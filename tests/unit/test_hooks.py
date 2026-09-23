from pathlib import Path

import pytest

from pr_review_agent.agent.context import ReviewContext
from pr_review_agent.agent.hooks import check_path, path_guard
from pr_review_agent.config import DEFAULT_IGNORE
from pr_review_agent.workspace import Workspace


@pytest.fixture
def roots(tmp_path):
    head, base = tmp_path / "head", tmp_path / "base"
    (head / "backend/src").mkdir(parents=True)
    base.mkdir()
    return head.resolve(), base.resolve()


@pytest.mark.parametrize(
    ("raw", "allowed"),
    [
        ("backend/src/routes.ts", True),
        ("../base/backend/src/routes.ts", True),
        ("/etc/passwd", False),
        ("~/.ssh/id_ed25519", False),
        ("../../outside.txt", False),
        ("backend/.env", False),
        ("backend/node_modules/x/index.js", False),
        (".git/config", False),
        ("backend/firebase-service-account.json", False),
        (".", True),
    ],
)
def test_check_path(roots, raw, allowed):
    head, base = roots
    reason = check_path(raw, head, [head, base], DEFAULT_IGNORE)
    assert (reason is None) == allowed, reason


async def test_hook_denies_with_sdk_shape(roots, repo_cfg):
    head, base = roots
    ctx = ReviewContext(
        mode="review", ws=Workspace(root=head.parent, head=head, head_sha="x", base=base), cfg=repo_cfg, runner=None
    )
    guard = path_guard(ctx).hooks[0]
    deny = await guard(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Read",
            "tool_input": {"file_path": str(Path.home() / ".aws/credentials")},
        },
        "id",
        None,
    )
    assert deny["hookSpecificOutput"]["permissionDecision"] == "deny"
    glob = await guard(
        {"hook_event_name": "PreToolUse", "tool_name": "Glob", "tool_input": {"pattern": "/Users/**/*.pem"}}, "id", None
    )
    assert glob["hookSpecificOutput"]["permissionDecision"] == "deny"
    ok = await guard(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Grep",
            "tool_input": {"pattern": "requireAuth", "path": "backend/src"},
        },
        "id",
        None,
    )
    assert ok == {}
