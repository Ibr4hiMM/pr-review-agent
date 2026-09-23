"""Runs one Claude Agent SDK session and returns the structured ReviewResult."""

from __future__ import annotations

import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    query,
)
from pydantic import ValidationError

from ..config import Settings
from ..models import ReviewResult, review_result_schema
from .context import ReviewContext
from .hooks import path_guard
from .prompts import SYSTEM_PROMPT
from .tools import SERVER_NAME, allowed_tool_names, build_server

log = logging.getLogger(__name__)

BUILTIN_TOOLS = ["Read", "Grep", "Glob"]

# Everything the Claude Code harness would otherwise load into every session, none of which a reviewer
# uses. Each of these is re-billed on every turn: connectors from the Claude account alone were
# thousands of tokens of tool names and instructions. Also a safety measure: the agent reads untrusted
# code and must never see tools that reach your email, files or databases.
LEAN_ENV = {
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",  # claude.ai connectors (Gmail, Drive, Supabase, ...)
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",  # we pass the repo's notes ourselves, from the base branch
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
    "CLAUDE_CODE_DISABLE_BUNDLED_SKILLS": "1",
    "CLAUDE_CODE_DISABLE_ADVISOR_TOOL": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",  # e.g. background session-title generation
}
# Belt and braces: `tools=` already limits the built-ins, `dontAsk` denies anything not allowed.
DENIED_TOOLS = ["Bash", "Write", "Edit", "NotebookEdit", "WebFetch", "WebSearch", "Agent", "Task", "Skill"]


@dataclass
class AgentRun:
    result: ReviewResult | None = None
    cost_usd: float = 0.0
    turns: int = 0
    duration_s: float = 0.0
    error: str | None = None
    tool_calls: Counter[str] = field(default_factory=Counter)
    blocked_calls: list[str] = field(default_factory=list)


def task_budget_tokens(budget_usd: float) -> int:
    """Advisory token budget so the model paces itself and returns findings before the hard USD cap."""
    return max(20_000, min(int(budget_usd * 60_000), 400_000))


def build_options(ctx: ReviewContext, settings: Settings, budget_usd: float) -> ClaudeAgentOptions:
    return ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        model=settings.model,
        effort=settings.effort,
        tools=BUILTIN_TOOLS,
        allowed_tools=BUILTIN_TOOLS + allowed_tool_names(),
        disallowed_tools=DENIED_TOOLS,
        permission_mode="dontAsk",
        mcp_servers={SERVER_NAME: build_server(ctx)},
        cwd=str(ctx.ws.head),
        add_dirs=[str(ctx.ws.base)] if ctx.ws.base else [],
        # Never load settings/hooks/CLAUDE.md from disk: the repo under review is untrusted, and the
        # user's own ~/.claude config shouldn't change how reviews behave.
        setting_sources=[],
        strict_mcp_config=True,  # only our own tool server
        skills=[],  # no skill listing in the prompt
        env=LEAN_ENV,
        max_turns=settings.max_turns,
        max_budget_usd=budget_usd,
        task_budget={"total": task_budget_tokens(budget_usd)},
        output_format={"type": "json_schema", "schema": review_result_schema()},
        hooks={"PreToolUse": [path_guard(ctx)]},
        stderr=lambda line: log.debug("claude: %s", line),
    )


def _describe_call(ctx: ReviewContext, block: ToolUseBlock) -> str | None:
    name = block.name.removeprefix(f"mcp__{SERVER_NAME}__")
    if name == "StructuredOutput":
        return None
    inp = block.input or {}
    arg = str(inp.get("file_path") or inp.get("pattern") or inp.get("symbol") or inp.get("project") or "")
    for root in (ctx.ws.head, ctx.ws.base):
        if root:
            arg = arg.replace(f"{root}/", "").replace(f"{root.resolve()}/", "")
    return f"{name} {arg[:80]}".strip()


async def run_agent(
    ctx: ReviewContext,
    prompt: str,
    settings: Settings,
    budget_usd: float,
    on_first_reply: Callable[[], None] | None = None,
) -> AgentRun:
    run = AgentRun()
    start = time.monotonic()
    final: ResultMessage | None = None
    allowed = set(BUILTIN_TOOLS + allowed_tool_names() + ["StructuredOutput"])
    pending: dict[str, str] = {}  # tool_use_id -> description, for calls to tools we never offered
    try:
        async for message in query(prompt=prompt, options=build_options(ctx, settings, budget_usd)):
            if isinstance(message, AssistantMessage):
                if on_first_reply:
                    on_first_reply()  # the shared prompt prefix is now in the cache
                    on_first_reply = None
                for block in message.content:
                    if isinstance(block, ToolUseBlock):
                        run.tool_calls[block.name.removeprefix(f"mcp__{SERVER_NAME}__")] += 1
                        if block.name not in allowed:
                            pending[block.id] = block.name
                        elif desc := _describe_call(ctx, block):
                            ctx.progress(desc)
            elif isinstance(message, UserMessage) and isinstance(message.content, list):
                for block in message.content:
                    if isinstance(block, ToolResultBlock) and block.tool_use_id in pending:
                        name = pending.pop(block.tool_use_id)
                        state = "blocked" if block.is_error else "UNEXPECTEDLY SUCCEEDED"
                        run.blocked_calls.append(f"{name}: {state}")
                        ctx.progress(f"{name} {state} (not an available tool)")
            elif isinstance(message, ResultMessage):
                final = message
    except Exception as e:  # query() raises after yielding an error result; keep what we have
        run.error = f"{type(e).__name__}: {e}"
    run.duration_s = time.monotonic() - start
    if final is None:
        run.error = run.error or "agent ended without a result"
        return run
    run.cost_usd = final.total_cost_usd or 0.0
    run.turns = final.num_turns
    for denial in final.permission_denials or []:
        tool = denial.get("tool_name") if isinstance(denial, dict) else getattr(denial, "tool_name", "?")
        run.blocked_calls.append(f"{tool}: denied")
    if final.subtype != "success" or final.structured_output is None:
        detail = "; ".join(final.errors or []) or (final.result or "")[:300]
        run.error = f"agent run failed ({final.subtype}{', ' + detail if detail else ''})"
        return run
    try:
        run.result = ReviewResult.model_validate(final.structured_output)
    except ValidationError as e:
        run.error = f"structured output did not match the schema: {e}"
    return run
