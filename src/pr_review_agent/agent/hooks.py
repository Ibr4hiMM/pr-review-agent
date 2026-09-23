"""PreToolUse guard: the agent may only look inside the checkouts, and never at ignored/secret files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from claude_agent_sdk import HookMatcher

from ..config import matches_any
from .context import ReviewContext

GUARDED_TOOLS = "Read|Grep|Glob"
MAX_READ_LINES = 400  # a whole 3,800-line file per Read was the biggest single token cost


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
        for raw in candidates:
            if isinstance(raw, str) and raw:
                reason = check_path(raw, cwd, roots, ctx.cfg.ignore)
                if reason:
                    return _deny(reason)
        if input_data.get("tool_name") == "Read":
            return _cap_read(tool_input, cwd)
        return {}

    return HookMatcher(matcher=GUARDED_TOOLS, hooks=[guard])
