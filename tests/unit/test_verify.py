from pathlib import Path

import pytest

from pr_review_agent.agent.context import ReviewContext
from pr_review_agent.analyzers.tests import SuiteResult
from pr_review_agent.diff import parse_diff
from pr_review_agent.models import Diagnostic, Evidence, Finding, ReviewResult, TestCase, TestRun
from pr_review_agent.verify import snippet_matches, verify_result
from pr_review_agent.workspace import Workspace

FAIL = TestRun(exit_code=1, cases=[TestCase(id="t", status="failed", message="AssertionError: expected 9 to be 10")])
PASS = TestRun(exit_code=0, cases=[TestCase(id="t", status="passed")])
BROKEN = TestRun(exit_code=1, load_error="Cannot find module '../../src/nope'")

SRC = """export function lastPage(total: number, size: number) {
  return Math.floor(total / size);
}
"""


class FakeRunner:
    """Maps (checkout name, test_code) -> TestRun."""

    def __init__(self, results):
        self.results = results
        self.calls = []

    async def run_repro(self, root, p, code):
        self.calls.append((root.name, code))
        return self.results[(root.name, code)], "backend/test/__pr_review__/repro_x.test.ts"


def make_ctx(tmp_path: Path, repo_cfg, results, mode="review", diagnostics=(), suites=None, diff_text=None):
    for side in ("head", "base"):
        (tmp_path / side / "backend/src").mkdir(parents=True, exist_ok=True)
        (tmp_path / side / "backend/src/page.ts").write_text(SRC)
    ws = Workspace(
        root=tmp_path,
        head=tmp_path / "head",
        head_sha="h" * 40,
        base=tmp_path / "base" if mode == "review" else None,
        base_sha="b" * 40,
    )
    diff = parse_diff(diff_text) if diff_text else None
    return ReviewContext(
        mode=mode,
        ws=ws,
        cfg=repo_cfg,
        runner=FakeRunner(results),
        diagnostics=list(diagnostics),
        suites=suites or {},
        diff=diff,
    )


def finding(*evidence, confidence=0.9, file="backend/src/page.ts", **kw):
    base = dict(
        title="lastPage drops the final partial page",
        severity="high",
        category="logic",
        project="backend",
        file=file,
        line_start=2,
        line_end=2,
        explanation="floor instead of ceil",
        confidence=confidence,
        evidence=list(evidence),
    )
    base.update(kw)
    return Finding(**base)


async def test_failing_test_that_passes_on_base_is_verified(tmp_path, repo_cfg):
    ctx = make_ctx(tmp_path, repo_cfg, {("head", "T"): FAIL, ("base", "T"): PASS})
    report = await verify_result(
        ctx,
        ReviewResult(
            summary="",
            findings=[finding(Evidence(kind="failing_test", test_code="T", test_output="made up by the model"))],
        ),
    )
    [v] = report.kept
    assert v.tier == "verified" and not v.pre_existing
    assert "expected 9 to be 10" in v.evidence[0].test_output  # replaced with our own output


async def test_repro_that_does_not_load_is_rejected(tmp_path, repo_cfg):
    ctx = make_ctx(tmp_path, repo_cfg, {("head", "T"): BROKEN})
    report = await verify_result(
        ctx, ReviewResult(summary="", findings=[finding(Evidence(kind="failing_test", test_code="T"))])
    )
    assert not report.kept
    assert "failed to load" in report.dropped[0][1]


async def test_repro_that_passes_on_head_is_rejected(tmp_path, repo_cfg):
    ctx = make_ctx(tmp_path, repo_cfg, {("head", "T"): PASS})
    report = await verify_result(
        ctx, ReviewResult(summary="", findings=[finding(Evidence(kind="failing_test", test_code="T"))])
    )
    assert not report.kept


DIFF_TOUCHING_LINE_2 = """\
diff --git a/backend/src/page.ts b/backend/src/page.ts
--- a/backend/src/page.ts
+++ b/backend/src/page.ts
@@ -1,3 +1,3 @@
 export function lastPage(total: number, size: number) {
-  return Math.floor(total / size) ;
+  return Math.floor(total / size);
 }
"""


async def test_pre_existing_bug_kept_only_when_pr_touches_it(tmp_path, repo_cfg):
    results = {("head", "T"): FAIL, ("base", "T"): FAIL}
    ev = Evidence(kind="failing_test", test_code="T")
    ctx = make_ctx(tmp_path, repo_cfg, results, diff_text=DIFF_TOUCHING_LINE_2)
    [v] = (await verify_result(ctx, ReviewResult(summary="", findings=[finding(ev)]))).kept
    assert v.pre_existing
    untouched = DIFF_TOUCHING_LINE_2.replace("@@ -1,3 +1,3 @@", "@@ -40,3 +40,3 @@")
    ctx = make_ctx(tmp_path, repo_cfg, results, diff_text=untouched)
    report = await verify_result(ctx, ReviewResult(summary="", findings=[finding(ev)]))
    assert not report.kept and "does not touch" in report.dropped[0][1]


async def test_code_reference_must_quote_real_code_and_meet_threshold(tmp_path, repo_cfg):
    ctx = make_ctx(tmp_path, repo_cfg, {}, mode="scan")
    real = Evidence(
        kind="code_reference", file="backend/src/page.ts", line=2, snippet="return   Math.floor(total / size);"
    )
    fake = Evidence(kind="code_reference", file="backend/src/page.ts", line=2, snippet="return Math.ceil(total);")
    report = await verify_result(
        ctx,
        ReviewResult(
            summary="",
            findings=[
                finding(real, confidence=0.9),
                finding(fake, confidence=0.95, title="another"),
                finding(real, confidence=0.5, title="low confidence"),
            ],
        ),
    )
    assert [v.tier for v in report.kept] == ["possible"]
    reasons = " | ".join(r for _, r in report.dropped)
    assert "quoted code not found" in reasons and "confidence 0.50" in reasons


async def test_static_evidence_must_match_a_computed_diagnostic(tmp_path, repo_cfg):
    diag = Diagnostic(tool="tsc", rule="TS2532", file="backend/src/page.ts", line=2, message="possibly undefined")
    ctx = make_ctx(tmp_path, repo_cfg, {}, mode="scan", diagnostics=[diag])
    ok = Evidence(kind="static", tool="tsc", rule="TS2532", file="backend/src/page.ts", line=3)
    bad = Evidence(kind="static", tool="tsc", rule="TS9999", file="backend/src/page.ts", line=2)
    report = await verify_result(
        ctx, ReviewResult(summary="", findings=[finding(ok), finding(bad, title="bogus rule")])
    )
    assert len(report.kept) == 1 and report.kept[0].evidence[0].line == 2


async def test_regression_evidence_must_be_observed(tmp_path, repo_cfg):
    suites = {"backend": SuiteResult(project="backend", regressed=["test/page.test.ts::last page"])}
    ctx = make_ctx(tmp_path, repo_cfg, {}, suites=suites)
    report = await verify_result(
        ctx,
        ReviewResult(
            summary="",
            findings=[
                finding(Evidence(kind="test_regression", test_id="last page")),
                finding(Evidence(kind="test_regression", test_id="imaginary test"), title="imaginary"),
            ],
        ),
    )
    assert [v.tier for v in report.kept] == ["verified"]


@pytest.mark.parametrize("path", ["/etc/passwd", "../secrets.ts", "backend/src/missing.ts", "backend/.env"])
async def test_bad_paths_are_dropped(tmp_path, repo_cfg, path):
    ctx = make_ctx(tmp_path, repo_cfg, {("head", "T"): FAIL})
    (tmp_path / "head/backend/.env").write_text("SECRET=1")
    report = await verify_result(
        ctx, ReviewResult(summary="", findings=[finding(Evidence(kind="failing_test", test_code="T"), file=path)])
    )
    assert not report.kept


async def test_duplicates_are_collapsed(tmp_path, repo_cfg):
    ctx = make_ctx(tmp_path, repo_cfg, {("head", "T"): FAIL}, mode="scan")
    ev = Evidence(kind="failing_test", test_code="T")
    report = await verify_result(
        ctx, ReviewResult(summary="", findings=[finding(ev), finding(ev, title="lastPage drops final partial page!")])
    )
    assert len(report.kept) == 1


def test_snippet_matching_tolerates_whitespace_and_small_offsets(tmp_path):
    (tmp_path / "f.ts").write_text("a\nb\n  const x =   1;\nd\n")
    assert snippet_matches(tmp_path, "f.ts", 1, "const x = 1;")
    assert snippet_matches(tmp_path, "f.ts", 3, "b\n  const x = 1;")
    assert not snippet_matches(tmp_path, "f.ts", 3, "const x = 2;")
