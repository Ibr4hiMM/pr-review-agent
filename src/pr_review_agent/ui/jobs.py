"""Background jobs started from the dashboard (scans and reviews), run one at a time."""

from __future__ import annotations

import asyncio
import threading
import time
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from ..config import Settings, load_repo_config
from ..github_client import parse_target
from .repos import RepoError, ref_exists, repo_root

JobKind = Literal["scan", "review-local", "review"]
JobStatus = Literal["queued", "running", "done", "failed", "cancelled"]
MIN_BUDGET, MAX_BUDGET = 0.3, 100.0
MAX_LOG = 2000

Executor = Callable[["Job", Settings, Callable[[str], None]], Awaitable[str]]


@dataclass
class Job:
    id: str
    kind: JobKind
    params: dict[str, Any]
    title: str
    status: JobStatus = "queued"
    log: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat(timespec="seconds"))
    started: float | None = None
    finished: float | None = None
    run_id: str | None = None
    error: str | None = None
    _loop: asyncio.AbstractEventLoop | None = None
    _task: asyncio.Task | None = None

    def to_dict(self, since: int = 0) -> dict[str, Any]:
        elapsed = None
        if self.started:
            elapsed = round((self.finished or time.monotonic()) - self.started, 1)
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "params": self.params,
            "created_at": self.created_at,
            "elapsed_s": elapsed,
            "run_id": self.run_id,
            "error": self.error,
            "log": self.log[since:],
            "log_len": len(self.log),
        }


def _budget(params: dict[str, Any], default: float) -> float:
    try:
        value = float(params.get("budget_usd", default))
    except (TypeError, ValueError):
        raise RepoError("The spending limit must be a number.") from None
    if not MIN_BUDGET <= value <= MAX_BUDGET:
        raise RepoError(f"The spending limit must be between ${MIN_BUDGET:.2f} and ${MAX_BUDGET:.0f}.")
    return value


def validate(kind: str, params: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Check what the form sent and normalize it. Raises RepoError with a message for the person."""
    if kind == "scan":
        root = repo_root(str(params.get("repo_path", "")))
        cfg = load_repo_config(root)
        enabled = [p.name for p in cfg.enabled_projects()]
        projects = [str(p) for p in params.get("projects") or []]
        unknown = [p for p in projects if p not in enabled]
        if unknown:
            raise RepoError(f"These projects can't be scanned: {', '.join(unknown)}.")
        if not enabled:
            raise RepoError("No supported projects were found in this repository.")
        clean = {
            "repo_path": str(root),
            "projects": projects,
            "budget_usd": _budget(params, 5.0),
            "uncommitted": bool(params.get("uncommitted", True)),
        }
        scope = f" ({', '.join(projects)})" if projects else ""
        return clean, f"Scan {root.name}{scope}"
    if kind == "review-local":
        root = repo_root(str(params.get("repo_path", "")))
        base, head = str(params.get("base", "")), str(params.get("head", ""))
        for ref in (base, head):
            if not ref_exists(root, ref):
                raise RepoError(f"There's no branch or commit called {ref!r} in {root.name}.")
        if base == head:
            raise RepoError("Pick two different branches to compare.")
        clean = {"repo_path": str(root), "base": base, "head": head, "budget_usd": _budget(params, 2.0)}
        return clean, f"Review {head} against {base} in {root.name}"
    if kind == "review":
        target = str(params.get("target", "")).strip()
        try:
            owner, repo, number = parse_target(target)
        except ValueError:
            raise RepoError("Enter the pull request as owner/repo#123 or paste its GitHub link.") from None
        clean = {
            "target": f"{owner}/{repo}#{number}",
            "post": bool(params.get("post", False)),
            "budget_usd": _budget(params, 2.0),
        }
        return clean, f"Review pull request {owner}/{repo}#{number}"
    raise RepoError("Unknown job type.")


async def execute(job: Job, settings: Settings, progress: Callable[[str], None]) -> str:
    """The real work: pick a sandbox, run the scan/review, save it, return the run id."""
    from .. import actions
    from ..sandbox import pick_sandbox

    s = settings.model_copy()
    sandbox, note = await pick_sandbox("auto", s.cache_dir)
    if note:
        progress(note)
    p = job.params
    if job.kind == "scan":
        _res, record = await actions.scan(
            Path(p["repo_path"]), s, sandbox, progress, p["projects"] or None, p["budget_usd"], None, p["uncommitted"]
        )
    elif job.kind == "review-local":
        s.review_budget_usd = p["budget_usd"]
        _res, record = await actions.review_local(Path(p["repo_path"]), p["base"], p["head"], s, sandbox, progress)
    else:
        s.review_budget_usd = p["budget_usd"]
        _res, record = await actions.review_pr(p["target"], s, sandbox, progress, p["post"])
    summary = record.summary()
    progress(
        f"Done: {summary['verified']} proven, {summary['possible']} possible, {summary['fixes']} verified "
        f"fix(es), ${summary['cost_usd']:.2f}."
    )
    return record.id


def friendly_error(e: Exception, job: Job) -> str:
    """Say what went wrong in plain words; raw library errors aren't useful in the dashboard."""
    from githubkit.exception import RequestFailed

    from ..runner import InstallError
    from ..workspace import GitError

    if isinstance(e, RequestFailed):
        code = e.response.status_code
        what = job.params.get("target", "the repository")
        if code == 404:
            return f"GitHub couldn't find {what}. Check the number, and that your GitHub login can see the repository."
        if code in (401, 403):
            return "GitHub refused the request. Sign in again with gh auth login, then retry."
        return f"GitHub returned an error ({code}). Try again in a minute."
    if isinstance(e, InstallError):
        first = str(e).splitlines()[0]
        return f"{first}. Check that the project installs on its own (pr-review doctor shows the details)."
    if isinstance(e, GitError):
        return f"Git couldn't prepare the checkout: {str(e).split('failed:', 1)[-1].strip()[:300]}"
    return str(e) or type(e).__name__


class JobManager:
    def __init__(self, settings: Settings, executor: Executor = execute, on_done: Callable[[Job], None] | None = None):
        self.settings = settings
        self.executor = executor
        self.on_done = on_done
        self.jobs: dict[str, Job] = {}
        self._queue: deque[str] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._worker: threading.Thread | None = None

    def submit(self, kind: str, params: dict[str, Any]) -> Job:
        clean, title = validate(kind, params)
        job = Job(id=uuid.uuid4().hex[:10], kind=kind, params=clean, title=title)  # type: ignore[arg-type]
        with self._lock:
            self.jobs[job.id] = job
            self._queue.append(job.id)
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work, name="pr-review-jobs", daemon=True)
                self._worker.start()
        self._wake.set()
        return job

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            jobs = sorted(self.jobs.values(), key=lambda j: j.created_at, reverse=True)
        return [
            {k: v for k, v in j.to_dict().items() if k != "log"} | {"last": j.log[-1] if j.log else ""} for j in jobs
        ]

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def cancel(self, job_id: str) -> Job | None:
        job = self.jobs.get(job_id)
        if job is None:
            return None
        with self._lock:
            if job.status == "queued":
                self._queue.remove(job.id)
                job.status = "cancelled"
                job.log.append("Cancelled before it started.")
            elif job.status == "running" and job._loop and job._task:
                job.log.append("Cancelling…")
                job._loop.call_soon_threadsafe(job._task.cancel)
        return job

    def _work(self) -> None:
        while True:
            with self._lock:
                job_id = self._queue.popleft() if self._queue else None
            if job_id is None:
                self._wake.clear()
                if not self._wake.wait(timeout=30):
                    with self._lock:
                        if not self._queue:
                            self._worker = None
                            return
                continue
            self._run(self.jobs[job_id])

    def _run(self, job: Job) -> None:
        job.status = "running"
        job.started = time.monotonic()

        def progress(msg: str) -> None:
            if len(job.log) < MAX_LOG:
                job.log.append(f"[{time.monotonic() - (job.started or 0):6.1f}s] {msg}")

        loop = asyncio.new_event_loop()
        job._loop = loop
        try:
            job._task = loop.create_task(self.executor(job, self.settings, progress))
            job.run_id = loop.run_until_complete(job._task)
            job.status = "done"
        except asyncio.CancelledError:
            job.status = "cancelled"
            progress("Cancelled. Nothing was saved.")
        except Exception as e:  # shown in the dashboard
            job.status = "failed"
            job.error = friendly_error(e, job)
            progress(f"Failed: {job.error}")
        finally:
            job.finished = time.monotonic()
            job._task = None
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            finally:
                loop.close()
            job._loop = None
            if self.on_done:
                self.on_done(job)
