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


def test_large_reads_are_capped_and_small_ones_untouched(roots):
    from pr_review_agent.agent.hooks import MAX_READ_LINES, _cap_read

    head, _ = roots
    (head / "big.ts").write_text("x\n" * 3800)
    (head / "small.ts").write_text("x\n" * 50)
    capped = _cap_read({"file_path": str(head / "big.ts")}, head)["hookSpecificOutput"]
    assert capped["updatedInput"]["limit"] == MAX_READ_LINES and capped["updatedInput"]["offset"] == 1
    assert "3800 lines" in capped["additionalContext"]
    assert _cap_read({"file_path": "small.ts"}, head) == {}
    assert _cap_read({"file_path": "big.ts", "offset": 1200, "limit": 300}, head) == {}
    assert _cap_read({"file_path": "big.ts", "offset": 3600}, head) == {}  # fewer than the cap remain


def test_agent_sessions_are_lean(roots, repo_cfg):
    from pr_review_agent.agent.reviewer import build_options
    from pr_review_agent.config import Settings

    head, base = roots
    ctx = ReviewContext(mode="scan", ws=Workspace(root=head.parent, head=head, head_sha="x"), cfg=repo_cfg, runner=None)
    opts = build_options(ctx, Settings(), 2.0)
    assert opts.strict_mcp_config and opts.skills == [] and opts.setting_sources == []
    assert opts.env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false" and opts.env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"


async def test_reads_wait_while_another_chunk_has_the_file_patched(roots, repo_cfg):
    import asyncio

    from pr_review_agent.agent.hooks import wait_for_real_code
    from pr_review_agent.fixes import FileChange, applied

    head, base = roots
    src = head / "backend/src/page.ts"
    src.write_text("real\n")
    ctx = ReviewContext(mode="scan", ws=Workspace(root=head.parent, head=head, head_sha="x"), cfg=repo_cfg, runner=None)
    guard = path_guard(ctx).hooks[0]
    seen = []

    async def fix_check():
        with applied(head, [FileChange("backend/src/page.ts", "real\n", "trial fix\n")]):
            await asyncio.sleep(0.5)

    async def agent_read():
        await asyncio.sleep(0.1)  # starts while the fix is applied
        await guard(
            {"hook_event_name": "PreToolUse", "tool_name": "Read", "tool_input": {"file_path": str(src)}}, "id", None
        )
        seen.append(src.read_text())

    await asyncio.gather(fix_check(), agent_read())
    assert seen == ["real\n"]
    with applied(head, [FileChange("backend/src/page.ts", "real\n", "trial fix\n")]):
        assert not await wait_for_real_code([head / "backend"], wait_s=0.3)  # a directory under Grep
    assert await wait_for_real_code([head / "backend"], wait_s=0.3)
