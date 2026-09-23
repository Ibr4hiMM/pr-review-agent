"""Dart / Flutter projects — planned (milestone 8). Detected by `init` but disabled until implemented."""

from __future__ import annotations

from ..config import ProjectConfig
from ..models import TestRun
from ..sandbox import ExecResult
from .base import LanguageAdapter, PathMap


class DartAdapter(LanguageAdapter):
    language = "dart"
    default_image = "ghcr.io/cirruslabs/flutter:stable"
    source_exts = (".dart",)

    def can_run_tests(self, p: ProjectConfig) -> bool:
        return False

    def test_cmd(self, p: ProjectConfig, files: list[str] | None, report: str) -> str:
        raise NotImplementedError("Dart support is not implemented yet")

    def parse_test_report(self, report: str | None, res: ExecResult, paths: PathMap) -> TestRun:
        raise NotImplementedError("Dart support is not implemented yet")

    def repro_file(self, p: ProjectConfig, uid: str) -> str:
        raise NotImplementedError("Dart support is not implemented yet")

    def repro_instructions(self, p: ProjectConfig) -> str:
        return f"Project `{p.name}` is Dart; repro tests are not supported yet, use code_reference evidence."
