"""`pr-review scan`: review a whole repo (or some projects) chunk by chunk under a total budget."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ..adapters import adapter_for
from ..agent.context import ReviewContext
from ..agent.prompts import PROMPT_VERSION, build_scan_prompt
from ..agent.reviewer import run_agent
from ..analyzers import static
from ..analyzers.tests import SuiteResult
from ..config import Settings, load_repo_config
from ..models import SEVERITY_ORDER, Finding, ReviewResult, VerifiedFinding
from ..render import Stats, dropped_notes
from ..runner import ProjectRunner
from ..sandbox import Sandbox
from ..verify import verify_result
from ..workspace import prepare_local_workspace, repo_slug
from .planner import Chunk, plan_chunks

# Below this a chunk can't finish (it hits the cap before returning anything), so the scan stops instead.
MIN_CHUNK_BUDGET = 1.0


@dataclass
class ScanOutcome:
    repo: str
    sha: str
    kept: list[VerifiedFinding] = field(default_factory=list)
    dropped: list[tuple[Finding, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    stats: Stats = field(default_factory=Stats)
    chunks_total: int = 0
    chunks_done: int = 0
    cached_chunks: int = 0
    sources: dict[str, str] = field(default_factory=dict)


def chunk_label(chunk: Chunk) -> str:
    """Short name for the live view: the main file, its line window, and how many files come with it."""
    first = chunk.focus[0].rsplit("/", 1)[-1]
    if first in chunk.ranges or chunk.focus[0] in chunk.ranges:
        lo, hi = chunk.ranges[chunk.focus[0]]
        return f"{first} {lo}–{hi}"
    extra = len(chunk.focus) - 1
    return f"{first} +{extra}" if extra else first


class _Budget:
    """Total spend cap shared by concurrent chunks. A chunk reserves up to its own cap up front; if the
    rest is reserved by chunks still running, it waits for them to settle their real (usually lower) cost."""

    def __init__(self, total: float):
        self.total, self.spent, self.reserved = total, 0.0, 0.0
        self.cond = asyncio.Condition()

    async def reserve(self, want: float) -> float:
        async with self.cond:
            while True:
                avail = self.total - self.spent - self.reserved
                if avail >= MIN_CHUNK_BUDGET:
                    grant = min(want, avail)
                    self.reserved += grant
                    return grant
                if self.reserved <= 0:
                    return 0.0  # truly exhausted
                await self.cond.wait()

    async def settle(self, granted: float, cost: float) -> None:
        async with self.cond:
            self.reserved -= granted
            self.spent += cost
            self.cond.notify_all()


async def run_scan(
    repo_path: Path,
    settings: Settings,
    sandbox: Sandbox,
    progress: Callable[[str], None],
    project_names: list[str] | None = None,
    budget_usd: float = 10.0,
    max_chunks: int | None = None,
    include_uncommitted: bool = False,
    concurrency: int = 3,
    emit: Callable[[dict], None] = lambda _event: None,
) -> ScanOutcome:
    emit({"type": "phase", "phase": "prepare"})
    ws = await asyncio.to_thread(prepare_local_workspace, repo_path, settings.cache_dir, "HEAD", include_uncommitted)
    out = ScanOutcome(repo=repo_slug(repo_path) or repo_path.name, sha=ws.head_sha)
    out.stats.model = settings.model
    try:
        cfg = load_repo_config(ws.head)
        projects = cfg.enabled_projects()
        if project_names:
            projects = [cfg.project(n) for n in project_names]
        if not projects:
            out.notes.append("No enabled projects to scan.")
            return out
        runner = ProjectRunner(sandbox, settings)
        ctx = ReviewContext(mode="scan", ws=ws, cfg=cfg, runner=runner, progress=progress, emit=emit)
        out.sources = ctx.sources

        progress(f"preparing {', '.join(p.name for p in projects)} (install, tests, static checks)…")
        diags, errs = await static.collect(runner, ws.head, projects, cfg)
        ctx.diagnostics = [d for d in diags if static.is_bug_relevant(d)]
        out.notes += [f"static check error: {e}" for e in errs][:5]
        for p in projects:
            if adapter_for(p).can_run_tests(p):
                try:
                    run = await runner.run_tests(ws.head, p)
                    ctx.suites[p.name] = SuiteResult(project=p.name, head=run)
                    out.notes.append(f"tests {p.name}: {run.summary()}")
                except Exception as e:
                    out.notes.append(f"tests {p.name}: could not run ({str(e)[:300]}); repro tests will fail too")

        chunks = plan_chunks(ws.head, cfg, projects, ctx.diagnostics)
        if max_chunks:
            chunks = chunks[:max_chunks]
        out.chunks_total = len(chunks)
        progress(f"{len(chunks)} chunk(s) planned; budget ${budget_usd:.2f}")
        emit(
            {
                "type": "plan",
                "budget": budget_usd,
                "chunks": [{"label": chunk_label(c), "files": c.describe_focus(), "lines": c.lines} for c in chunks],
            }
        )
        emit({"type": "phase", "phase": "review"})

        cache_dir = settings.cache_dir / "scan-cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        budget = _Budget(budget_usd)
        sem = asyncio.Semaphore(concurrency)
        seen_titles: list[str] = []
        stop = asyncio.Event()
        # Chunks that start together all pay to write the same ~30K-token prompt prefix to the cache.
        # Let one chunk go first; the rest start once its first reply shows the prefix is cached.
        warm = asyncio.Event()
        leader = asyncio.Lock()

        def found(i: int, kept: list[VerifiedFinding]) -> None:
            for v in kept:
                emit(
                    {
                        "type": "finding",
                        "i": i,
                        "title": v.finding.title,
                        "severity": v.finding.severity,
                        "file": v.finding.file,
                        "line": v.finding.line_start,
                        "tier": v.tier,
                        "fix": v.fix.status if v.fix else None,
                    }
                )

        async def do_chunk(i: int, chunk: Chunk) -> None:
            # Cached chunks cost nothing, so they count even once the budget has run out. This is what lets a
            # re-run continue an unfinished scan: it only pays for the chunks that weren't reviewed.
            key = chunk.cache_key(ws.head, f"{PROMPT_VERSION}|{settings.model}|{settings.effort}")
            cached = cache_dir / f"{key}.json"
            if cached.exists():
                kept = [VerifiedFinding.model_validate(v) for v in json.loads(cached.read_text())]
                out.kept.extend(kept)
                out.chunks_done += 1
                out.cached_chunks += 1
                emit({"type": "chunk", "i": i, "state": "cached", "proven": len(kept)})
                found(i, kept)
                return
            if stop.is_set():
                emit({"type": "chunk", "i": i, "state": "skipped"})
                return
            async with sem:
                granted = await budget.reserve(settings.review_budget_usd)
                if not granted:
                    stop.set()
                    emit({"type": "chunk", "i": i, "state": "skipped"})
                    return
                emit({"type": "chunk", "i": i, "state": "reviewing"})
                emit({"type": "spent", "usd": budget.spent, "reserved": budget.reserved})
                is_leader = not warm.is_set() and not leader.locked()
                if is_leader:
                    await leader.acquire()
                elif not warm.is_set():
                    try:
                        await asyncio.wait_for(warm.wait(), timeout=120)
                    except TimeoutError:
                        pass
                progress(f"[{i + 1}/{len(chunks)}] {chunk.project}: {', '.join(chunk.describe_focus())}")
                prompt = build_scan_prompt(ctx, chunk.describe_focus(), chunk.context, seen_titles)
                run = await run_agent(ctx, prompt, settings, granted, on_first_reply=warm.set if is_leader else None)
                warm.set()  # even if the leader failed before replying, don't hold the others back
                await budget.settle(granted, run.cost_usd)
                emit({"type": "spent", "usd": budget.spent, "reserved": budget.reserved})
                out.stats.turns += run.turns
                out.stats.duration_s += run.duration_s
                if run.error:
                    out.notes.append(f"chunk {', '.join(chunk.describe_focus())}: {run.error}")
                out.notes += [f"blocked tool call: {b}" for b in run.blocked_calls]
                if run.result is None:
                    emit({"type": "chunk", "i": i, "state": "failed", "error": (run.error or "")[:200]})
                    return
                in_focus = [f for f in run.result.findings if chunk.covers(f.file.removeprefix("./"), f.line_start)]
                out.stats.proposed += len(run.result.findings)
                report = await verify_result(ctx, ReviewResult(summary=run.result.summary, findings=in_focus))
                out.kept.extend(report.kept)
                out.dropped.extend(report.dropped)
                out.dropped.extend(
                    (f, "outside the chunk's focus")
                    for f in run.result.findings
                    if not chunk.covers(f.file.removeprefix("./"), f.line_start)
                )
                seen_titles.extend(f"{v.finding.file}: {v.finding.title}" for v in report.kept)
                found(i, report.kept)
                emit({"type": "chunk", "i": i, "state": "done", "proven": len(report.kept)})
                if not run.error:  # only cache complete runs
                    cached.write_text(json.dumps([v.model_dump(mode="json") for v in report.kept]))
                out.chunks_done += 1

        await asyncio.gather(*(do_chunk(i, c) for i, c in enumerate(chunks)))
        emit({"type": "phase", "phase": "done"})
        out.stats.cost_usd = budget.spent
        out.stats.dropped = len(out.dropped)
        out.notes += dropped_notes(out.dropped)
        # De-duplicate across chunks and order by tier/severity.
        uniq: dict[str, VerifiedFinding] = {}
        for v in out.kept:
            uniq.setdefault(v.fingerprint, v)
        out.kept = sorted(
            uniq.values(),
            key=lambda v: (
                v.tier != "verified",
                SEVERITY_ORDER[v.finding.severity],
                v.finding.file,
                v.finding.line_start,
            ),
        )
        if out.cached_chunks:
            out.stats.extra = f"{out.cached_chunks} chunk(s) from cache"
        return out
    finally:
        ws.cleanup()
