"""Markdown for GitHub review comments, the sticky PR summary, and scan reports."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .models import Evidence, VerifiedFinding

MARKER_PREFIX = "<!-- pr-review-agent"
SUMMARY_MARKER = "<!-- pr-review-agent:summary -->"
_MARKER_RE = re.compile(r"<!-- pr-review-agent fp=(?P<fp>[0-9a-f]+) cat=(?P<cat>[\w-]+) -->")

_SEV_ICON = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "⚪"}
_FENCE_LANG = {".ts": "ts", ".tsx": "tsx", ".js": "js", ".mjs": "js", ".py": "python", ".dart": "dart"}


@dataclass
class Stats:
    cost_usd: float = 0.0
    turns: int = 0
    duration_s: float = 0.0
    proposed: int = 0
    dropped: int = 0
    model: str = ""
    extra: str = ""

    def line(self) -> str:
        parts = [
            f"{self.proposed} proposed",
            f"{self.dropped} dropped by verification",
            f"${self.cost_usd:.2f}",
            f"{self.turns} turns",
            f"{self.duration_s:.0f}s",
        ]
        if self.model:
            parts.append(self.model)
        return " · ".join(parts) + (f" · {self.extra}" if self.extra else "")


def dropped_notes(dropped: list, limit: int = 12) -> list[str]:
    """One line per finding the verifier rejected, so nothing disappears silently."""
    out = [f"dropped: {f.file}:{f.line_start} {f.title[:90]} ({why[:160]})" for f, why in dropped[:limit]]
    if len(dropped) > limit:
        out.append(f"…and {len(dropped) - limit} more dropped")
    return out


def marker(v: VerifiedFinding) -> str:
    return f"<!-- pr-review-agent fp={v.fingerprint} cat={v.finding.category} -->"


def parse_marker(body: str) -> tuple[str, str] | None:
    m = _MARKER_RE.search(body or "")
    return (m["fp"], m["cat"]) if m else None


def _lang(path: str | None) -> str:
    for ext, lang in _FENCE_LANG.items():
        if path and path.endswith(ext):
            return lang
    return ""


def _fence(code: str, lang: str = "") -> str:
    ticks = "````" if "```" in code else "```"
    return f"{ticks}{lang}\n{code.rstrip()}\n{ticks}"


def _evidence_md(ev: Evidence, file: str) -> str:
    if ev.kind == "failing_test":
        return (
            "<details><summary>🧪 Failing test (re-run and confirmed by pr-review-agent)</summary>\n\n"
            f"{_fence(ev.test_code or '', _lang(file))}\n\nOutput:\n{_fence((ev.test_output or '')[:3000])}\n</details>"
        )
    if ev.kind == "test_regression":
        return f"🧪 Existing test `{ev.test_id}` passes on the base branch and fails with this change."
    if ev.kind == "static":
        return f"🔎 `{ev.tool}` {f'`{ev.rule}`' if ev.rule else ''} at `{ev.file}:{ev.line}`"
    if ev.kind == "code_reference":
        why = f" — {ev.why_relevant}" if ev.why_relevant else ""
        return f"📎 `{ev.file}:{ev.line}`{why}\n{_fence(ev.snippet or '', _lang(ev.file))}"
    return ""


def finding_body(v: VerifiedFinding, heading_level: int = 0) -> str:
    f = v.finding
    tier = "**Verified**" if v.tier == "verified" else "**Possible**"
    title = f"{_SEV_ICON[f.severity]} {tier} · {f.severity} · {f.category}: {f.title}"
    head = f"{'#' * heading_level} {title}" if heading_level else title
    parts = [head, "", f.explanation]
    if v.pre_existing:
        parts += ["", "_This bug predates the PR, but the PR touches this code._"]
    parts += ["", *[_evidence_md(ev, f.file) for ev in v.evidence]]
    if f.suggested_fix:
        parts += ["", f"**Suggested fix:** {f.suggested_fix}"]
    if v.fix and v.fix.status != "failed":
        label = (
            "✅ Verified fix: the failing test passes with this patch and existing tests still pass"
            if v.fix.status == "verified"
            else "🩹 Proposed fix (applies cleanly; not checked by a test)"
        )
        parts += ["", f"<details><summary>{label}</summary>\n\n{_fence(v.fix.patch, 'diff')}\n</details>"]
    return "\n".join(parts).strip() + "\n"


def inline_comment(v: VerifiedFinding) -> str:
    return finding_body(v) + "\n" + marker(v)


def summary_comment(
    kept: list[VerifiedFinding], inline_fps: set[str], head_sha: str, stats: Stats, notes: list[str]
) -> str:
    verified = [v for v in kept if v.tier == "verified"]
    possible = [v for v in kept if v.tier == "possible"]
    lines = [SUMMARY_MARKER, "## 🔍 pr-review-agent", ""]
    if not kept:
        lines.append(f"No evidence-backed bugs found in `{head_sha[:7]}`.")
    else:
        lines.append(f"`{head_sha[:7]}`: **{len(verified)} verified**, {len(possible)} possible finding(s).")
        lines += ["", "| | Finding | Location | Evidence |", "|---|---|---|---|"]
        for v in kept:
            f = v.finding
            where = f"`{f.file}:{f.line_start}`" + (" (inline)" if v.fingerprint in inline_fps else "")
            kinds = ", ".join(sorted({e.kind.replace("_", " ") for e in v.evidence}))
            lines.append(f"| {_SEV_ICON[f.severity]} | {f.title} | {where} | {kinds} |")
        outside = [v for v in kept if v.fingerprint not in inline_fps]
        if outside:
            lines += ["", "### Findings outside the diff", ""]
            lines += [finding_body(v, heading_level=4) for v in outside]
    if notes:
        lines += ["", "<details><summary>Run notes</summary>", "", *[f"- {n}" for n in notes], "</details>"]
    lines += ["", f"<sub>{stats.line()}</sub>"]
    return "\n".join(lines) + "\n"


def scan_report(
    repo: str,
    sha: str,
    kept: list[VerifiedFinding],
    stats: Stats,
    notes: list[str],
    chunks_done: int,
    chunks_total: int,
) -> str:
    verified = [v for v in kept if v.tier == "verified"]
    lines = [
        f"# Scan report: {repo} @ {sha[:7]}",
        "",
        f"**{len(verified)} verified**, {len(kept) - len(verified)} possible finding(s) · "
        f"{chunks_done}/{chunks_total} chunks reviewed",
        "",
        f"_{stats.line()}_",
        "",
    ]
    if chunks_done < chunks_total:
        lines += [
            f"> Budget ran out after {chunks_done} of {chunks_total} chunks (highest-risk first). "
            "Re-run with a larger `--max-budget-usd`; reviewed chunks are cached.",
            "",
        ]
    if kept:
        lines += ["| | Finding | Location | Tier |", "|---|---|---|---|"]
        for v in kept:
            f = v.finding
            lines.append(f"| {_SEV_ICON[f.severity]} | {f.title} | `{f.file}:{f.line_start}` | {v.tier} |")
        lines.append("")
        for v in kept:
            lines += [
                finding_body(v, heading_level=2),
                f"`{v.finding.file}:{v.finding.line_start}-{v.finding.line_end}`",
                "",
            ]
    else:
        lines.append("No evidence-backed bugs found.")
    if notes:
        lines += ["", "## Run notes", "", *[f"- {n}" for n in notes]]
    return "\n".join(lines) + "\n"


def findings_json(kept: list[VerifiedFinding]) -> str:
    return json.dumps([v.model_dump(mode="json") for v in kept], indent=2)
