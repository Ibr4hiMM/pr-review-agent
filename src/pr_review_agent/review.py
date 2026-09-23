"""`pr-review review`: the PR pipeline (fetch → analyze → agent → verify → render → post)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .adapters import adapter_for
from .agent.context import ReviewContext
from .agent.prompts import build_review_prompt
from .agent.reviewer import run_agent
from .analyzers import static
from .analyzers.tests import SuiteResult, compare_suites
from .config import RepoConfig, Settings, github_token, load_repo_config
from .diff import PrDiff, parse_diff
from .github_client import ExistingComment, GitHubClient, PullRequest, parse_target
from .models import Evidence, Finding, VerifiedFinding, fingerprint
from .render import Stats, dropped_notes, inline_comment, summary_comment
from .runner import ProjectRunner
from .sandbox import Sandbox
from .verify import verify_result
from .workspace import Workspace, git, prepare_local_pair, prepare_pr_workspace


@dataclass
class ReviewOutcome:
    pr: PullRequest
    kept: list[VerifiedFinding] = field(default_factory=list)
    inline: list[dict] = field(default_factory=list)
    summary: str = ""
    dropped: list[tuple[Finding, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    stats: Stats = field(default_factory=Stats)
    review_url: str | None = None
    summary_url: str | None = None


def plan_inline(
    kept: list[VerifiedFinding], diff: PrDiff, existing: list[ExistingComment], max_inline: int
) -> tuple[list[dict], set[str]]:
    """Inline comments to post (skipping ones already posted) and the fingerprints shown inline."""
    comments: list[dict] = []
    inline_fps: set[str] = set()
    for v in kept:
        f = v.finding
        fd = diff.get(f.file)
        if fd is None:
            continue
        in_range = range(f.line_start, f.line_end + 1)
        # Prefer a line the PR actually changed; fall back to any line GitHub will accept.
        anchors = [n for n in in_range if n in fd.added_lines] or [n for n in in_range if n in fd.commentable_lines]
        if not anchors:
            continue
        already = any(
            e.fingerprint == v.fingerprint
            or (
                e.path == f.file and e.category == f.category and e.line is not None and abs(e.line - f.line_start) <= 5
            )
            for e in existing
        )
        if already:
            inline_fps.add(v.fingerprint)
            continue
        if len(comments) >= max_inline:
            continue
        inline_fps.add(v.fingerprint)
        comments.append({"path": f.file, "line": anchors[0], "side": "RIGHT", "body": inline_comment(v)})
    return comments, inline_fps


def regression_findings(ctx: ReviewContext, kept: list[VerifiedFinding]) -> list[VerifiedFinding]:
    """Existing tests that pass on base and fail on head are bugs even if the agent didn't mention them."""
    cited = {e.test_id for v in kept for e in v.evidence if e.kind == "test_regression"}
    out = []
    for suite in ctx.suites.values():
        p = ctx.cfg.project(suite.project)
        for test_id in suite.regressed:
            if any(c and (c in test_id or test_id in c) for c in cited):
                continue
            rel_file = test_id.split("::", 1)[0]
            if p.language == "python" and not rel_file.endswith(".py"):
                rel_file = rel_file.replace(".", "/") + ".py"
            repo_file = f"{p.norm_path}/{rel_file}" if p.norm_path else rel_file
            line = _find_line(ctx, repo_file, test_id.rsplit("::", 1)[-1].split(" ")[-1])
            head_failed = suite.head.failed if suite.head else []
            msg = ctx.clean(next((c.message for c in head_failed if c.id == test_id), None) or "")
            finding = Finding(
                title=f"Existing test now fails: {test_id.rsplit('::', 1)[-1][:120]}",
                severity="high",
                category="regression",
                project=p.name,
                file=repo_file,
                line_start=line,
                line_end=line,
                confidence=1.0,
                explanation="This test passes on the base branch and fails with this PR, so the change "
                "breaks behaviour the test suite relies on.",
                evidence=[Evidence(kind="test_regression", test_id=test_id, test_output=msg[:2000])],
            )
            out.append(
                VerifiedFinding(
                    finding=finding,
                    tier="verified",
                    fingerprint=fingerprint(repo_file, "regression", test_id),
                    evidence=finding.evidence,
                )
            )
    return out


def _find_line(ctx: ReviewContext, repo_file: str, needle: str) -> int:
    try:
        for i, line in enumerate((ctx.ws.head / repo_file).read_text(errors="replace").splitlines(), 1):
            if needle and needle in line:
                return i
    except OSError:
        pass
    return 1


async def _analyze(ctx: ReviewContext, runner: ProjectRunner, affected, notes: list[str]) -> None:
    ws, cfg = ctx.ws, ctx.cfg
    assert ctx.diff is not None and ws.base is not None
    testable = [p for p in affected if adapter_for(p).can_run_tests(p)]
    ctx.progress(f"analyzing {', '.join(p.name for p in affected)} (static checks on base+head, tests)")
    (head_diags, head_errs), (base_diags, base_errs), *suites = await asyncio.gather(
        static.collect(runner, ws.head, affected, cfg),
        static.collect(runner, ws.base, affected, cfg),
        *(compare_suites(runner, ws, p) for p in testable),
    )
    ctx.diagnostics = static.new_on_changed_lines(base_diags, head_diags, ctx.diff.changed_lines())
    notes += [f"static check error (head): {e}" for e in head_errs][:5]
    notes += [f"static check error (base): {e}" for e in base_errs][:5]
    for s in suites:
        assert isinstance(s, SuiteResult)
        ctx.suites[s.project] = s
        notes.append("tests: " + s.describe())


async def _review_core(
    ws: Workspace,
    pr: PullRequest,
    settings: Settings,
    sandbox: Sandbox,
    progress: Callable[[str], None],
    out: ReviewOutcome,
) -> tuple[PrDiff, RepoConfig]:
    """Analyze → agent → verify, filling `out`. Shared by GitHub PR reviews and local branch reviews."""
    assert ws.base is not None
    cfg = load_repo_config(ws.base)  # base branch config: a PR can't change how it gets reviewed
    diff = parse_diff(ws.diff()).filtered(lambda p: not cfg.is_ignored(p))
    affected = sorted({p.name: p for f in diff.files if (p := cfg.project_for(f.path))}.values(), key=lambda p: p.name)
    runner = ProjectRunner(sandbox, settings)
    ctx = ReviewContext(mode="review", ws=ws, cfg=cfg, runner=runner, diff=diff, progress=progress)
    out.stats.model = settings.model
    if not affected:
        out.notes.append("No changed files belong to an enabled project; nothing to review.")
        return diff, cfg
    await _analyze(ctx, runner, affected, out.notes)
    progress("agent reviewing…")
    run = await run_agent(ctx, build_review_prompt(ctx, pr), settings, settings.review_budget_usd)
    out.stats.cost_usd, out.stats.turns, out.stats.duration_s = run.cost_usd, run.turns, run.duration_s
    if run.error:
        out.notes.append(run.error)
    out.notes += [f"blocked tool call: {b}" for b in run.blocked_calls]
    if run.result:
        out.stats.proposed = len(run.result.findings)
        progress(f"verifying {len(run.result.findings)} proposed finding(s)…")
        report = await verify_result(ctx, run.result)
        out.kept, out.dropped = report.kept, report.dropped
    out.kept += regression_findings(ctx, out.kept)
    out.stats.dropped = len(out.dropped)
    out.notes += dropped_notes(out.dropped)
    return diff, cfg


async def run_review(
    target: str, settings: Settings, sandbox: Sandbox, post: bool, progress: Callable[[str], None]
) -> ReviewOutcome:
    owner, repo, number = parse_target(target)
    token = github_token()
    gh = GitHubClient(token)
    pr = await gh.get_pr(owner, repo, number)
    out = ReviewOutcome(pr=pr)
    progress(f"{pr.label}: {pr.title!r} ({pr.head_sha[:7]} → {pr.base_ref})")
    ws = await asyncio.to_thread(
        prepare_pr_workspace, owner, repo, number, pr.head_sha, pr.base_sha, token, settings.cache_dir
    )
    try:
        diff, cfg = await _review_core(ws, pr, settings, sandbox, progress, out)
        existing = await gh.existing_comments(pr) if token else []
        out.inline, inline_fps = plan_inline(out.kept, diff, existing, cfg.thresholds.max_inline_comments)
        out.summary = summary_comment(out.kept, inline_fps, pr.head_sha, out.stats, out.notes)
        if post:
            if out.inline:
                body = (
                    f"pr-review-agent found {len(out.inline)} new issue(s) in `{pr.head_sha[:7]}`; "
                    "see the summary comment for details."
                )
                out.review_url = await gh.create_review(pr, body, out.inline)
            out.summary_url = await gh.upsert_summary(pr, out.summary)
        return out
    finally:
        ws.cleanup()


async def run_local_review(
    repo_path: Path,
    base_ref: str,
    head_ref: str,
    settings: Settings,
    sandbox: Sandbox,
    progress: Callable[[str], None],
) -> ReviewOutcome:
    """Review `base_ref...head_ref` of a local repo as if it were a PR (no GitHub involved)."""
    ws = await asyncio.to_thread(prepare_local_pair, repo_path, settings.cache_dir, base_ref, head_ref)
    try:
        subject = git(["log", "-1", "--format=%s", ws.head_sha], cwd=ws.head).strip()
        log = git(["log", "--format=- %s%n%b", f"{ws.base_sha}..{ws.head_sha}"], cwd=ws.head).strip()
        pr = PullRequest(
            owner="local",
            repo=repo_path.resolve().name,
            number=0,
            title=subject,
            body=log[:4000],
            author=git(["log", "-1", "--format=%an", ws.head_sha], cwd=ws.head).strip(),
            head_sha=ws.head_sha,
            base_sha=ws.base_sha or "",
            base_ref=base_ref,
            head_repo=None,
            draft=False,
            url="",
        )
        out = ReviewOutcome(pr=pr)
        progress(f"{pr.label}: {subject!r}")
        diff, cfg = await _review_core(ws, pr, settings, sandbox, progress, out)
        out.inline, inline_fps = plan_inline(out.kept, diff, [], cfg.thresholds.max_inline_comments)
        out.summary = summary_comment(out.kept, inline_fps, ws.head_sha, out.stats, out.notes)
        return out
    finally:
        ws.cleanup()
