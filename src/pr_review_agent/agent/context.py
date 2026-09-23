"""Everything the agent's tools and the verifier need about one review/scan run."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from ..adapters import adapter_for
from ..analyzers.tests import SuiteResult
from ..config import ProjectConfig, RepoConfig
from ..diff import PrDiff
from ..models import Diagnostic
from ..runner import ProjectRunner
from ..workspace import Workspace

MAX_SOURCE_BYTES = 300_000
MAX_SOURCES = 60


@dataclass
class ReviewContext:
    mode: Literal["review", "scan"]
    ws: Workspace
    cfg: RepoConfig
    runner: ProjectRunner
    diagnostics: list[Diagnostic] = field(default_factory=list)
    suites: dict[str, SuiteResult] = field(default_factory=dict)
    diff: PrDiff | None = None
    progress: Callable[[str], None] = lambda _msg: None
    # Structured progress for the dashboard's live view (see ui/jobs.py for the event shapes).
    emit: Callable[[dict], None] = lambda _event: None
    # Head contents of files the findings point at, kept for the dashboard's code viewer (the
    # checkout is deleted when the run ends).
    sources: dict[str, str] = field(default_factory=dict)

    def keep_source(self, rel: str) -> None:
        if rel in self.sources or len(self.sources) >= MAX_SOURCES:
            return
        path = self.ws.head / rel
        try:
            if path.is_file() and path.stat().st_size <= MAX_SOURCE_BYTES:
                self.sources[rel] = path.read_text(errors="replace")
        except OSError:
            pass

    def clean(self, text: str, max_lines: int = 20) -> str:
        from ..adapters.base import clean_output

        return clean_output(text, [r for r in (self.ws.head, self.ws.base, self.ws.root) if r], max_lines)

    @property
    def allowed_roots(self) -> list[Path]:
        return [r.resolve() for r in (self.ws.head, self.ws.base) if r is not None]

    def project(self, name: str) -> ProjectConfig:
        for p in self.cfg.enabled_projects():
            if p.name == name:
                return p
        names = ", ".join(p.name for p in self.cfg.enabled_projects())
        raise ValueError(f"unknown or disabled project {name!r}; enabled projects: {names}")

    def testable_projects(self) -> list[ProjectConfig]:
        return [p for p in self.cfg.enabled_projects() if adapter_for(p).can_run_tests(p)]

    def regressed_tests(self) -> set[str]:
        return {t for s in self.suites.values() for t in s.regressed + s.new_failing}
