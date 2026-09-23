"""Real installs and test runs on the seeded fixture (needs network for npm; no API calls)."""

import pytest

from pr_review_agent.agent.context import ReviewContext
from pr_review_agent.agent.tools import repro_verdict
from pr_review_agent.analyzers.tests import compare_suites
from pr_review_agent.config import Settings, load_repo_config
from pr_review_agent.models import Evidence, Finding, ReviewResult
from pr_review_agent.runner import ProjectRunner
from pr_review_agent.sandbox import make_sandbox
from pr_review_agent.verify import verify_result
from pr_review_agent.workspace import prepare_local_pair
from tests.fixtures.seeded import make_ts_shop

pytestmark = pytest.mark.integration

REPRO = """import { expect, it } from 'vitest';
import { pageCount } from '../../src/pagination';

it('counts the last partial page', () => {
  expect(pageCount(41, 20)).toBe(3);
});
"""
BROKEN = "import { nope } from '../../src/nope';\nimport { it } from 'vitest';\nit('x', () => nope());\n"


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    repo = tmp_path_factory.mktemp("fixture") / "shop-repo"
    make_ts_shop(repo)
    settings = Settings(sandbox="local", cache_dir=tmp_path_factory.mktemp("cache"))
    ws = prepare_local_pair(repo, settings.cache_dir, "main", "feature")
    yield settings, ws
    ws.cleanup()


async def test_repro_fails_on_head_and_passes_on_base(env):
    settings, ws = env
    cfg = load_repo_config(ws.base)
    shop = cfg.project("shop")
    runner = ProjectRunner(make_sandbox("local", settings.cache_dir), settings)
    head, path = await runner.run_repro(ws.head, shop, REPRO)
    base, _ = await runner.run_repro(ws.base, shop, REPRO)
    assert path.startswith("shop/test/__pr_review__/repro_")
    ok, verdict = repro_verdict(head, base)
    assert ok and "introduced" in verdict
    assert not (ws.head / path).exists()  # cleaned up

    broken, _ = await runner.run_repro(ws.head, shop, BROKEN)
    assert repro_verdict(broken, None)[0] is False

    suite = await compare_suites(runner, ws, shop)
    assert suite.head and not suite.head.failed and not suite.regressed  # seeded bugs evade existing tests


async def test_verifier_accepts_real_repro_and_rejects_fake_quote(env):
    settings, ws = env
    cfg = load_repo_config(ws.base)
    runner = ProjectRunner(make_sandbox("local", settings.cache_dir), settings)
    ctx = ReviewContext(mode="review", ws=ws, cfg=cfg, runner=runner)
    common = dict(
        severity="high",
        category="logic",
        project="shop",
        file="shop/src/pagination.ts",
        line_start=3,
        line_end=3,
        explanation="x",
        confidence=0.9,
    )
    result = ReviewResult(
        summary="",
        findings=[
            Finding(title="floor drops last page", evidence=[Evidence(kind="failing_test", test_code=REPRO)], **common),
            Finding(
                title="made up",
                evidence=[
                    Evidence(
                        kind="code_reference",
                        file="shop/src/pagination.ts",
                        line=3,
                        snippet="return total / pageSize | 0;",
                    )
                ],
                **common,
            ),
        ],
    )
    report = await verify_result(ctx, result)
    assert [v.finding.title for v in report.kept] == ["floor drops last page"]
    assert report.kept[0].tier == "verified"


async def test_sandbox_blocks_network_and_secrets(env):
    settings, ws = env
    sb = make_sandbox("local", settings.cache_dir)
    if not sb.network_isolated:
        pytest.skip("no network isolation available on this platform")
    probe = "fetch('https://example.com').then(() => console.log('NET-OK')).catch(() => console.log('NET-BLOCKED'))"
    res = await sb.exec(ws.head, ".", f'node -e "{probe}"', network=False, timeout=60)
    assert "NET-BLOCKED" in res.output
    res = await sb.exec(ws.head, ".", "env", network=False, timeout=30)
    assert "ANTHROPIC_API_KEY" not in res.output and "GITHUB_TOKEN" not in res.output
    assert not (ws.head / "shop/.env").exists()  # git-ignored secrets never reach the checkout


async def test_check_fix_on_real_tests(env):
    from pr_review_agent.fixes import plan_edits
    from pr_review_agent.models import FixEdit

    settings, ws = env
    cfg = load_repo_config(ws.base)
    shop = cfg.project("shop")
    runner = ProjectRunner(make_sandbox("local", settings.cache_dir), settings)
    before = (ws.head / "shop/src/pagination.ts").read_text()

    def edits(new):
        return plan_edits(
            ws.head, cfg, [FixEdit(file="shop/src/pagination.ts", old="return Math.floor(total / pageSize);", new=new)]
        )

    good = await runner.check_fix(ws.head, shop, edits("return Math.ceil(total / pageSize);"), [REPRO])
    assert good.ok and good.checked_tests, good.notes

    useless = await runner.check_fix(ws.head, shop, edits("return Math.floor(total / pageSize) || 0;"), [REPRO])
    assert not useless.ok and "still fails" in " ".join(useless.notes)

    # Passes the repro (41 -> 3 pages) but breaks the existing test (40 items -> 2 pages).
    breaking = await runner.check_fix(ws.head, shop, edits("return Math.ceil((total + 1) / pageSize);"), [REPRO])
    assert not breaking.ok and "breaks existing tests" in " ".join(breaking.notes)

    assert (ws.head / "shop/src/pagination.ts").read_text() == before  # always restored
