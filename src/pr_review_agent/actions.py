"""Run a scan or review and save it to the run history. Shared by the CLI and the dashboard."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .config import Settings
from .review import ReviewOutcome, run_local_review, run_review
from .runs import RunRecord, new_run, save_run
from .sandbox import Sandbox
from .scan.runner import ScanOutcome, run_scan

Progress = Callable[[str], None]
Emit = Callable[[dict], None]  # structured progress for the dashboard's live view


def _no_events(_event: dict) -> None:
    pass


async def scan(
    repo_path: Path,
    settings: Settings,
    sandbox: Sandbox,
    progress: Progress,
    projects: list[str] | None = None,
    budget_usd: float = 10.0,
    max_chunks: int | None = None,
    uncommitted: bool = False,
    concurrency: int = 3,
    emit: Emit = _no_events,
    continues: str | None = None,
) -> tuple[ScanOutcome, RunRecord]:
    res = await run_scan(
        repo_path, settings, sandbox, progress, projects, budget_usd, max_chunks, uncommitted, concurrency, emit
    )
    target = str(repo_path.resolve()) + (f" ({', '.join(projects)})" if projects else "")
    record = new_run(
        "scan",
        res.repo,
        target,
        res.sha,
        settings.model,
        res.stats,
        res.kept,
        res.dropped,
        res.notes,
        chunks_done=res.chunks_done,
        chunks_total=res.chunks_total,
        repo_path=str(repo_path.resolve()),
        sources=res.sources,
        budget_usd=budget_usd,
        projects=projects or [],
        uncommitted=uncommitted,
        continues=continues,
    )
    save_run(record, settings.runs_dir)
    return res, record


async def review_local(
    repo_path: Path,
    base: str,
    head: str,
    settings: Settings,
    sandbox: Sandbox,
    progress: Progress,
    emit: Emit = _no_events,
) -> tuple[ReviewOutcome, RunRecord]:
    res = await run_local_review(repo_path, base, head, settings, sandbox, progress, emit)
    record = new_run(
        "review-local",
        res.pr.repo,
        f"{base}...{head}",
        res.pr.head_sha,
        settings.model,
        res.stats,
        res.kept,
        res.dropped,
        res.notes,
        repo_path=str(repo_path.resolve()),
        sources=res.sources,
    )
    save_run(record, settings.runs_dir)
    return res, record


async def review_pr(
    target: str,
    settings: Settings,
    sandbox: Sandbox,
    progress: Progress,
    post: bool = False,
    emit: Emit = _no_events,
) -> tuple[ReviewOutcome, RunRecord]:
    res = await run_review(target, settings, sandbox, post, progress, emit)
    pr = res.pr
    record = new_run(
        "review",
        f"{pr.owner}/{pr.repo}",
        pr.label,
        pr.head_sha,
        settings.model,
        res.stats,
        res.kept,
        res.dropped,
        res.notes,
        url=pr.url,
        sources=res.sources,
    )
    save_run(record, settings.runs_dir)
    return res, record
