"""Walking a workspace's source files (respecting the ignore globs)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

from .adapters import adapter_for
from .config import ProjectConfig, RepoConfig

_PRUNE = {
    ".git",
    "node_modules",
    ".pr-review",
    ".pr-review-venv",
    ".venv",
    "venv",
    "__pycache__",
    ".next",
    "dist",
    "build",
    ".dart_tool",
    "coverage",
}


def iter_source_files(root: Path, cfg: RepoConfig, project: ProjectConfig) -> Iterator[str]:
    """Repo-relative paths of the project's source files (tests included), sorted."""
    exts = adapter_for(project).source_exts
    start = root / project.norm_path if project.norm_path else root
    out = []
    for dirpath, dirnames, filenames in os.walk(start):
        dirnames[:] = [d for d in dirnames if d not in _PRUNE and not d.startswith(".")]
        for name in filenames:
            if not name.endswith(exts) or name.endswith(".d.ts"):
                continue
            rel = Path(dirpath, name).relative_to(root).as_posix()
            # Files belong to the most specific project (e.g. backend/ isn't part of the root app).
            if cfg.is_ignored(rel) or cfg.project_for(rel) is not project:
                continue
            out.append(rel)
    yield from sorted(out)
