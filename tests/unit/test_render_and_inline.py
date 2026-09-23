import json

from pr_review_agent.diff import parse_diff
from pr_review_agent.github_client import ExistingComment
from pr_review_agent.models import Evidence, Finding, VerifiedFinding, fingerprint, review_result_schema
from pr_review_agent.render import Stats, inline_comment, parse_marker, scan_report, summary_comment
from pr_review_agent.review import plan_inline

DIFF = """\
diff --git a/backend/src/page.ts b/backend/src/page.ts
--- a/backend/src/page.ts
+++ b/backend/src/page.ts
@@ -1,3 +1,4 @@
 export function lastPage(total: number, size: number) {
-  return Math.ceil(total / size);
+  const pages = Math.floor(total / size);
+  return pages;
 }
"""


def vf(line=2, title="lastPage drops the final partial page", tier="verified", file="backend/src/page.ts"):
    f = Finding(
        title=title,
        severity="high",
        category="logic",
        project="backend",
        file=file,
        line_start=line,
        line_end=line,
        explanation="floor instead of ceil",
        confidence=0.9,
        evidence=[Evidence(kind="failing_test", test_code="it('x', () => {})", test_output="expected 9 to be 10")],
    )
    return VerifiedFinding(finding=f, tier=tier, fingerprint=fingerprint(file, "logic", title), evidence=f.evidence)


def test_inline_comment_has_evidence_and_parseable_marker():
    v = vf()
    body = inline_comment(v)
    assert "Verified" in body and "expected 9 to be 10" in body and "```ts" in body
    assert parse_marker(body) == (v.fingerprint, "logic")


def test_plan_inline_anchors_skips_duplicates_and_respects_cap():
    diff = parse_diff(DIFF)
    on_diff, off_diff = vf(line=2), vf(line=40, title="other")
    comments, inline = plan_inline([on_diff, off_diff], diff, existing=[], max_inline=10)
    assert [(c["path"], c["line"], c["side"]) for c in comments] == [("backend/src/page.ts", 2, "RIGHT")]
    assert inline == {on_diff.fingerprint}

    posted = [ExistingComment(path="backend/src/page.ts", line=3, fingerprint="zzz", category="logic")]
    comments, inline = plan_inline([on_diff], diff, existing=posted, max_inline=10)
    assert comments == [] and inline == {on_diff.fingerprint}  # already on the PR: don't repeat

    comments, _ = plan_inline([vf(2), vf(3, title="second bug")], diff, existing=[], max_inline=1)
    assert len(comments) == 1


def test_summary_lists_outside_diff_findings_in_full():
    inside, outside = vf(2), vf(40, title="caller passes null", file="backend/src/routes.ts")
    md = summary_comment([inside, outside], {inside.fingerprint}, "a" * 40, Stats(cost_usd=1.234, proposed=3), [])
    assert "<!-- pr-review-agent:summary -->" in md
    assert "(inline)" in md and "Findings outside the diff" in md and "caller passes null" in md
    assert "$1.23" in md
    assert "No evidence-backed bugs" in summary_comment([], set(), "a" * 40, Stats(), [])


def test_scan_report_mentions_budget_cutoff():
    md = scan_report("me/repo", "b" * 40, [vf()], Stats(), ["tests backend: 28 passed"], 3, 10)
    assert "Budget ran out after 3 of 10" in md and "lastPage" in md


def test_agent_schema_is_flat_and_serializable():
    schema = review_result_schema()
    text = json.dumps(schema)
    assert "discriminator" not in text and "oneOf" not in text
    assert schema["required"] == ["summary", "findings"]


def test_inline_anchor_prefers_changed_line():
    comments, _ = plan_inline([vf(line=1)], parse_diff(DIFF), existing=[], max_inline=10)
    assert comments[0]["line"] == 1  # only line 1 in range: context line is acceptable
    wide = vf(line=1)
    wide.finding.line_end = 4
    comments, _ = plan_inline([wide], parse_diff(DIFF), existing=[], max_inline=10)
    assert comments[0]["line"] == 2  # first *added* line in the range


def test_clean_output_strips_local_paths_and_runner_frames(tmp_path):
    from pr_review_agent.adapters.base import clean_output

    raw = (
        f"AssertionError: expected 2 to be 3\n    at {tmp_path}/head/shop/test/x.test.ts:7:31\n"
        f"    at file://{tmp_path}/head/shop/node_modules/@vitest/runner/dist/chunk.js:155:11\n"
        "    at processTicksAndRejections (node:internal/process/task_queues:105:5)\n"
    )
    out = clean_output(raw, [tmp_path / "head", tmp_path])
    assert out == "AssertionError: expected 2 to be 3\n    at shop/test/x.test.ts:7:31"
