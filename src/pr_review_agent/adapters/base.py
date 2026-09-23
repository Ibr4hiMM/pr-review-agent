"""Language adapters: how to install, test, lint and write repro tests for one kind of project."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..config import ProjectConfig
from ..models import Diagnostic, TestRun
from ..sandbox import ExecResult

REPORT_DIR = ".pr-review"  # project-relative scratch dir for report files


@dataclass
class PathMap:
    """Turns paths printed by tools (absolute, container or host) into repo-relative paths."""

    project: ProjectConfig
    host_project_dir: Path

    @property
    def prefixes(self) -> list[str]:
        container = f"/work/{self.project.norm_path}".rstrip("/")
        return [str(self.host_project_dir.resolve()) + "/", str(self.host_project_dir) + "/", container + "/"]

    def to_repo(self, tool_path: str) -> str:
        """Tool path (absolute, or relative to the project dir) -> repo-relative."""
        rel = tool_path
        for pre in self.prefixes:
            if tool_path.startswith(pre):
                rel = tool_path[len(pre) :]
                break
        rel = rel.removeprefix("./")
        return f"{self.project.norm_path}/{rel}" if self.project.norm_path else rel

    def to_project(self, tool_path: str) -> str:
        return self.project.rel(self.to_repo(tool_path))


@dataclass
class CheckSpec:
    name: str
    parse: Callable[[str, PathMap], list[Diagnostic]]
    cmd: str | None = None  # run inside the sandbox (tools whose configs can execute repo code)
    host_argv: list[str] | None = None  # run on the host (pure static tools, e.g. ruff)
    report: str | None = None  # project-relative report file the command writes; else parse output


class LanguageAdapter(ABC):
    language: str
    default_image: str
    source_exts: tuple[str, ...]

    def wrap(self, cmd: str) -> str:
        """Prefix needed to run a command in the project's environment (e.g. venv activation)."""
        return cmd

    def install_cmd(self, p: ProjectConfig) -> str | None:
        return p.install

    def can_run_tests(self, p: ProjectConfig) -> bool:
        return bool(p.test)

    @abstractmethod
    def test_cmd(self, p: ProjectConfig, files: list[str] | None, report: str) -> str:
        """Command running the whole suite (files=None) or specific project-relative files."""

    @abstractmethod
    def parse_test_report(self, report: str | None, res: ExecResult, paths: PathMap) -> TestRun: ...

    @abstractmethod
    def repro_file(self, p: ProjectConfig, uid: str) -> str:
        """Project-relative path for a repro test."""

    @abstractmethod
    def repro_instructions(self, p: ProjectConfig) -> str:
        """Tells the agent how to write a repro test for this project."""

    def checks(self, p: ProjectConfig) -> list[CheckSpec]:
        return []

    def is_test_file(self, path: str) -> bool:
        return False


# Failures that mean the *test* is broken rather than the code under test.
BROKEN_TEST_MARKERS = (
    "SyntaxError",
    "ImportError",
    "ModuleNotFoundError",
    "NameError",
    "ReferenceError",
    "Cannot find module",
    "Failed to load url",
    "Failed to resolve import",
    "Transform failed",
    "is not defined",
    "fixture '",  # pytest: fixture 'x' not found
)


def broken_test_reason(run: TestRun) -> str | None:
    """Why a failing repro doesn't count as evidence, or None if the failure looks genuine."""
    if run.timed_out:
        return "timed out"
    if run.load_error:
        return "test file failed to load: " + run.load_error[:300]
    if not run.cases:
        return "no tests were collected"
    failed = run.failed
    if not failed:
        return None
    for case in failed:
        msg = case.message or ""
        if not any(m in msg for m in BROKEN_TEST_MARKERS):
            return None  # at least one genuine assertion/runtime failure
    return "every failure is an import/name/syntax error in the test itself"


_NOISE_FRAME = re.compile(
    r"^\s*at .*(node_modules|node:internal|processTicksAndRejections|new Promise \(<anonymous>\))"
)


def clean_output(text: str, roots: list[Path], max_lines: int = 20) -> str:
    """Make test output fit for a PR comment: workspace paths become repo-relative, runner-internal
    stack frames go, and the result is capped."""
    prefixes = sorted({f"{r}/" for root in roots for r in (root, root.resolve())} | {"/work/"}, key=len, reverse=True)
    lines = []
    for line in text.splitlines():
        if _NOISE_FRAME.match(line):
            continue
        for pre in prefixes:
            line = line.replace(pre, "")
        lines.append(line.rstrip())
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"… ({len(lines) - max_lines} more lines)"]
    return "\n".join(lines).strip()
