"""Deterministic verification of the agent's findings. Nothing is reported unless it passes here.

- failing_test: re-run (or reuse a run *we* made via the tool) — must genuinely fail on head
- test_regression: must be in the regression set our analyzer computed
- static: must match a diagnostic our analyzer computed
- code_reference: the quoted snippet must exist at that file/line
Tiers: executable evidence -> "verified"; only static/code_reference + confidence >= threshold -> "possible".
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .adapters import adapter_for
from .adapters.base import broken_test_reason
from .agent.context import ReviewContext
from .agent.tools import failure_excerpt, repro_verdict
from .fixes import EditError, content_hash, display_text, plan_edits, real_bytes, real_text, unified_patch
from .models import (
    SEVERITY_ORDER,
    CodeExcerpt,
    Diagnostic,
    Evidence,
    Finding,
    FixResult,
    ReviewResult,
    VerifiedFinding,
    fingerprint,
)

log = logging.getLogger(__name__)

LINE_SLACK = 3


@dataclass
class VerifyReport:
    kept: list[VerifiedFinding] = field(default_factory=list)
    dropped: list[tuple[Finding, str]] = field(default_factory=list)


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _safe_rel(path: str) -> str | None:
    p = path.strip().removeprefix("./")
    if not p or p.startswith("/") or ".." in Path(p).parts:
        return None
    return p


def snippet_matches(root: Path, file: str, line: int, snippet: str, slack: int = LINE_SLACK) -> bool:
    try:
        lines = real_text(root / file).splitlines()
    except OSError:
        return False
    want = _norm(snippet)
    if not want:
        return False
    span = snippet.strip("\n").count("\n") + 1
    lo, hi = max(0, line - 1 - slack), min(len(lines), line - 1 + span + slack)
    window = _norm(" ".join(lines[lo:hi]))
    return want in window


def _test_id_matches(claimed: str, known: set[str]) -> bool:
    c = claimed.strip()
    return any(c == k or k.endswith(c) or c.endswith(k) for k in known) if c else False


async def _check_failing_test(ctx: ReviewContext, f: Finding, ev: Evidence) -> tuple[bool, bool, str]:
    """-> (valid, pre_existing, note)"""
    try:
        p = ctx.project(f.project)
    except ValueError as e:
        return False, False, str(e)
    if not adapter_for(p).can_run_tests(p):
        return False, False, f"{p.name} has no test runner"
    head, _ = await ctx.runner.run_repro(ctx.ws.head, p, ev.test_code or "")
    base = None
    if ctx.mode == "review" and ctx.ws.base is not None and broken_test_reason(head) is None and head.failed:
        base, _ = await ctx.runner.run_repro(ctx.ws.base, p, ev.test_code or "")
    ok, verdict = repro_verdict(head, base)
    if ok:
        # Replace whatever the agent wrote with output we produced ourselves.
        ev.test_output = failure_excerpt(ctx, head, cases=2)
    return ok, "pre-existing" in verdict, verdict


def code_excerpt(
    root: Path, file: str, start: int, end: int, context: int = 10, max_lines: int = 70
) -> CodeExcerpt | None:
    try:
        lines = real_text(root / file).splitlines()
    except OSError:
        return None
    lo = max(1, start - context)
    hi = min(len(lines), max(end + context, lo), lo + max_lines - 1)
    return CodeExcerpt(file=file, start=lo, lines=lines[lo - 1 : hi], highlight=(start, end))


def cited_diagnostics(ctx: ReviewContext, evidence: list[Evidence]) -> list[Diagnostic]:
    """The diagnostics behind verified `static` evidence (verification pins each to its diagnostic)."""
    return [
        d
        for ev in evidence
        if ev.kind == "static"
        for d in ctx.diagnostics
        if (d.file, d.line, d.tool, d.rule) == (ev.file, ev.line, ev.tool, ev.rule)
    ]


def shared_repro_tests(findings: Iterable[Finding | VerifiedFinding]) -> frozenset[str]:
    """Repro tests cited by more than one finding. A fix for one of them may leave the others' cases failing."""
    counts: Counter[str] = Counter()
    for f in findings:
        counts.update({ev.test_code for ev in f.evidence if ev.kind == "failing_test" and ev.test_code})
    return frozenset(code for code, n in counts.items() if n > 1)


async def check_proposed_fix(
    ctx: ReviewContext, f: Finding, valid: list[Evidence], shared: frozenset[str] = frozenset()
) -> FixResult | str:
    """Independently re-check the agent's fix. Returns a FixResult, or a reason the fix was discarded."""
    try:
        changes = plan_edits(ctx.ws.head, ctx.cfg, f.fix_edits)
    except EditError as e:
        return f"proposed fix discarded: {e}"
    if not changes:
        return "proposed fix discarded: it changes nothing"
    p = ctx.cfg.project(f.project)
    if any(ctx.cfg.project_for(c.file) is not p for c in changes):
        return f"proposed fix discarded: it edits files outside project {p.name}"
    repro = [ev.test_code for ev in valid if ev.kind == "failing_test" and ev.test_code]
    regressed = [ev.test_id for ev in valid if ev.kind == "test_regression" and ev.test_id]
    suite = ctx.suites.get(p.name)
    known = {c.id for c in suite.head.cases} if suite and suite.head else set()
    must_pass = [k for t in regressed for k in known if k == t or k.endswith(t) or t.endswith(k)]
    try:
        check = await ctx.runner.check_fix(
            ctx.ws.head,
            p,
            changes,
            repro,
            suite.head if suite else None,
            must_pass,
            must_clear=cited_diagnostics(ctx, valid),
            shared_repros=shared,
        )
    except Exception as e:
        return f"proposed fix could not be checked: {e}"
    if not check.ok:
        status = "failed"
    elif check.checked_tests:
        status = "verified"
    else:
        status = "unverified"
    for c in changes:
        ctx.keep_source(c.file)
    return FixResult(
        status=status,
        patch=unified_patch(changes),
        notes=check.notes,
        patched={c.file: display_text(c.patched) for c in changes},
        source_hashes={c.file: content_hash(c.original.encode()) for c in changes},
    )


async def recheck_stale_fixes(ctx: ReviewContext, kept: list[VerifiedFinding]) -> bool:
    """Cached findings' fixes were checked against the code as it was then, and a scan chunk's cache
    only covers its own files. Check a fix again when a file it changes is different now. Returns
    whether any finding changed."""
    shared = shared_repro_tests(kept)
    changed = False
    for v in kept:
        fix = v.fix
        if fix is None or not v.finding.fix_edits:
            continue
        try:
            now = {f: content_hash(real_bytes(ctx.ws.head / f)) for f in fix.patched}
        except OSError:
            now = {}
        if fix.source_hashes and now == {f: fix.source_hashes.get(f) for f in fix.patched}:
            continue
        checked = await check_proposed_fix(ctx, v.finding, v.evidence, shared)
        if isinstance(checked, str):
            v.fix = None
            v.notes.append(f"{checked} (checked again because the code changed since the cached run)")
        else:
            v.fix = checked
        changed = True
    return changed


async def verify_result(ctx: ReviewContext, result: ReviewResult) -> VerifyReport:
    report = VerifyReport()
    threshold = ctx.cfg.thresholds.possible_min_confidence
    regressed = ctx.regressed_tests()
    seen: set[str] = set()

    for f in result.findings:
        rel = _safe_rel(f.file)
        if rel is None or not (ctx.ws.head / rel).is_file():
            report.dropped.append((f, f"file {f.file!r} does not exist in head"))
            continue
        if ctx.cfg.is_ignored(rel):
            report.dropped.append((f, f"{rel} is ignored"))
            continue
        owner = ctx.cfg.project_for(rel)
        if owner is None:
            report.dropped.append((f, f"{rel} is not in any enabled project"))
            continue
        f.file, f.project = rel, owner.name
        n_lines = len(real_text(ctx.ws.head / rel).splitlines())
        f.line_start = min(max(1, f.line_start), max(1, n_lines))
        f.line_end = min(max(f.line_start, f.line_end), max(1, n_lines))

        valid: list[Evidence] = []
        notes: list[str] = []
        executable = False
        introduced = False
        pre_existing = False
        for ev in f.evidence:
            if ev.kind == "failing_test" and ev.test_code:
                ok, pre, note = await _check_failing_test(ctx, f, ev)
                notes.append(note)
                if ok:
                    valid.append(ev)
                    executable = True
                    pre_existing |= pre
                    introduced |= not pre
            elif ev.kind == "test_regression" and ev.test_id:
                if _test_id_matches(ev.test_id, regressed):
                    valid.append(ev)
                    executable = introduced = True
                    notes.append(f"existing test {ev.test_id} passes on base and fails on head")
                else:
                    notes.append(f"{ev.test_id} is not a regression our test run observed")
            elif ev.kind == "static" and ev.file and ev.line:
                ev_file = _safe_rel(ev.file) or ""
                match = next(
                    (
                        d
                        for d in ctx.diagnostics
                        if d.file == ev_file
                        and abs(d.line - ev.line) <= 2
                        and (not ev.tool or d.tool.lower() == ev.tool.lower())
                        and (not ev.rule or (d.rule or "").lower() == ev.rule.lower())
                    ),
                    None,
                )
                if match:
                    ev.file, ev.line, ev.rule, ev.tool = match.file, match.line, match.rule, match.tool
                    valid.append(ev)
                else:
                    notes.append(f"no matching {ev.tool or ''} diagnostic at {ev.file}:{ev.line}")
            elif ev.kind == "code_reference" and ev.file and ev.line and ev.snippet:
                ev_file = _safe_rel(ev.file)
                if (
                    ev_file
                    and not ctx.cfg.is_ignored(ev_file)
                    and snippet_matches(ctx.ws.head, ev_file, ev.line, ev.snippet)
                ):
                    ev.file = ev_file
                    valid.append(ev)
                else:
                    notes.append(f"quoted code not found at {ev.file}:{ev.line}")
            else:
                notes.append(f"incomplete {ev.kind} evidence")

        if ctx.mode == "review" and pre_existing and not introduced and ctx.diff is not None:
            if not ctx.diff.touches(rel, f.line_start, f.line_end, slack=2):
                report.dropped.append((f, "pre-existing bug in lines this PR does not touch"))
                continue

        if executable:
            tier = "verified"
        elif valid and f.confidence >= threshold:
            tier = "possible"
        else:
            why = "no evidence survived verification" if not valid else f"confidence {f.confidence:.2f} < {threshold}"
            report.dropped.append((f, why + (": " + "; ".join(notes) if notes else "")))
            continue

        fp = fingerprint(rel, f.category, f.title)
        overlaps = any(
            k.finding.file == rel
            and k.finding.category == f.category
            and k.finding.line_start <= f.line_end
            and f.line_start <= k.finding.line_end
            for k in report.kept
        )
        if fp in seen or overlaps:
            report.dropped.append((f, "duplicate"))
            continue
        seen.add(fp)
        ctx.keep_source(rel)
        for ev in valid:
            if ev.kind in ("code_reference", "static") and ev.file:
                ctx.keep_source(ev.file)
        report.kept.append(
            VerifiedFinding(
                finding=f,
                tier=tier,
                fingerprint=fp,
                notes=notes,
                pre_existing=pre_existing and not introduced,
                evidence=valid,
                code=code_excerpt(ctx.ws.head, rel, f.line_start, f.line_end),
            )
        )

    # Fixes are checked once every finding is settled, so "another finding cites this test" only counts
    # findings that survived verification (not, say, a dropped duplicate of the same bug).
    shared = shared_repro_tests(report.kept)
    for v in report.kept:
        if v.finding.fix_edits:
            checked = await check_proposed_fix(ctx, v.finding, v.evidence, shared)
            if isinstance(checked, str):
                v.notes.append(checked)
            else:
                v.fix = checked

    report.kept.sort(
        key=lambda v: (v.tier != "verified", SEVERITY_ORDER[v.finding.severity], v.finding.file, v.finding.line_start)
    )
    return report
