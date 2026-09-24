"""PreToolUse guard: the agent may only look inside the checkouts, and never at ignored/secret files."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from claude_agent_sdk import HookMatcher

from ..config import matches_any
from ..fixes import is_patched
from .context import ReviewContext

GUARDED_TOOLS = "Read|Grep|Glob"
MAX_READ_LINES = 400  # a whole 3,800-line file per Read was the biggest single token cost
PATCH_WAIT_S = 45.0  # how long a Read/Grep waits for another chunk's fix check to restore the file
HOOK_TIMEOUT_S = 90  # the SDK's default (60 s) would cut the wait short


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def check_path(raw: str, cwd: Path, roots: list[Path], ignore: list[str]) -> str | None:
    """Reason to deny access to `raw`, or None if it's fine."""
    p = Path(raw).expanduser()
    resolved = (p if p.is_absolute() else cwd / p).resolve()
    root = next((r for r in roots if resolved == r or resolved.is_relative_to(r)), None)
    if root is None:
        return f"{raw} is outside the repository checkouts"
    rel = resolved.relative_to(root).as_posix()
    if rel == ".git" or rel.startswith(".git/") or "/.git/" in f"/{rel}/":
        return "git internals are off limits"
    if rel != "." and matches_any(rel, ignore):
        return f"{rel} matches an ignore pattern (generated, vendored or secret files)"
    return None


def _cap_read(tool_input: dict[str, Any], cwd: Path) -> dict[str, Any]:
    """Limit a Read to MAX_READ_LINES at a time when the file is longer than that."""
    limit = tool_input.get("limit")
    if isinstance(limit, int) and 0 < limit <= MAX_READ_LINES:
        return {}
    raw = tool_input.get("file_path")
    if not isinstance(raw, str):
        return {}
    path = Path(raw) if Path(raw).is_absolute() else cwd / raw
    try:
        with path.open("rb") as fh:
            total = sum(1 for _ in fh)
    except OSError:
        return {}
    offset = tool_input.get("offset") if isinstance(tool_input.get("offset"), int) else 1
    if total - offset + 1 <= MAX_READ_LINES:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {**tool_input, "offset": offset, "limit": MAX_READ_LINES},
            "additionalContext": f"{raw} has {total} lines; reads return at most {MAX_READ_LINES} lines. "
            "Use offset to read the part you need.",
        }
    }


async def wait_for_real_code(targets: list[Path], wait_s: float = PATCH_WAIT_S) -> bool:
    """Scan chunks share one checkout, and a fix check patches files in it for a while. Wait until none
    of `targets` (files or directories) is patched, so the agent reads the real code rather than another
    chunk's trial fix. False if something is still patched when the wait runs out."""
    deadline = time.monotonic() + wait_s
    while any(is_patched(t) for t in targets):
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.2)
    return True


def path_guard(ctx: ReviewContext) -> HookMatcher:
    roots = ctx.allowed_roots
    cwd = ctx.ws.head.resolve()

    async def guard(input_data: dict[str, Any], tool_use_id: str | None, context: Any) -> dict[str, Any]:
        if input_data.get("hook_event_name") != "PreToolUse":
            return {}
        tool_input = input_data.get("tool_input") or {}
        candidates = [tool_input.get(k) for k in ("file_path", "path", "notebook_path")]
        pattern = tool_input.get("pattern")
        if input_data.get("tool_name") == "Glob" and isinstance(pattern, str) and pattern.startswith(("/", "~")):
            candidates.append(pattern.split("*", 1)[0] or "/")
        paths = [raw for raw in candidates if isinstance(raw, str) and raw]
        for raw in paths:
            reason = check_path(raw, cwd, roots, ctx.cfg.ignore)
            if reason:
                return _deny(reason)
        tool = input_data.get("tool_name")
        if tool in ("Read", "Grep"):
            targets = [p if (p := Path(raw).expanduser()).is_absolute() else cwd / p for raw in paths] or [cwd]
            if not await wait_for_real_code(targets):
                return {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "additionalContext": "A fix check had this code temporarily patched while you read it, so "
                        "what you see may include a trial fix. Read it again before quoting it.",
                    }
                }
        if tool == "Read":
            return _cap_read(tool_input, cwd)
        return {}

    return HookMatcher(matcher=GUARDED_TOOLS, hooks=[guard], timeout=HOOK_TIMEOUT_S)
