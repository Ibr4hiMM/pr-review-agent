"""Static-analysis evidence: diagnostics that are new in head (review) or bug-relevant (scan)."""

from __future__ import annotations

import asyncio
from collections import Counter
from pathlib import Path

from ..config import ProjectConfig, RepoConfig
from ..models import Diagnostic
from ..runner import ProjectRunner

# Rule families that point at bugs rather than style. tsc errors always count.
_BUG_RULE_PREFIXES = {
    "ruff": ("F", "B", "E9", "PLE", "S", "ASYNC", "RUF006"),
    "mypy": ("",),
    "tsc": ("",),
}
_BUG_ESLINT_RULES = (
    "no-undef",
    "no-unreachable",
    "no-dupe",
    "no-unsafe",
    "no-fallthrough",
    "no-constant-condition",
    "no-self-assign",
    "no-cond-assign",
    "use-isnan",
    "valid-typeof",
    "no-async-promise-executor",
    "require-atomic-updates",
    "@typescript-eslint/no-floating-promises",
    "@typescript-eslint/no-misused-promises",
    "@typescript-eslint/await-thenable",
    "@typescript-eslint/no-unsafe",
    "react-hooks/rules-of-hooks",
    "react-hooks/exhaustive-deps",
)


def is_bug_relevant(d: Diagnostic) -> bool:
    if d.tool == "eslint":
        return d.severity == "error" and any((d.rule or "").startswith(r) for r in _BUG_ESLINT_RULES)
    prefixes = _BUG_RULE_PREFIXES.get(d.tool)
    return prefixes is not None and any((d.rule or "").startswith(p) for p in prefixes)


async def collect(
    runner: ProjectRunner, root: Path, projects: list[ProjectConfig], cfg: RepoConfig
) -> tuple[list[Diagnostic], list[str]]:
    results = await asyncio.gather(*(runner.run_checks(root, p) for p in projects), return_exceptions=True)
    diags: list[Diagnostic] = []
    errors: list[str] = []
    for p, res in zip(projects, results, strict=True):
        if isinstance(res, BaseException):
            errors.append(f"{p.name}: {res}")
            continue
        found, errs = res
        diags += [d for d in found if not cfg.is_ignored(d.file)]
        errors += errs
    return diags, errors


def new_on_changed_lines(
    base: list[Diagnostic], head: list[Diagnostic], changed: dict[str, set[int]]
) -> list[Diagnostic]:
    """Head diagnostics that didn't exist on base and sit on a changed line.

    Base/head are matched by line-independent identity (lines shift between versions). Where head
    has more copies of an identical diagnostic than base, the ones on changed lines are the new ones.
    """
    remaining = Counter(d.identity() for d in base)

    def on_changed(d: Diagnostic) -> bool:
        return d.line in changed.get(d.file, ())

    new = []
    for d in sorted(head, key=on_changed):  # unchanged-line copies consume base matches first
        if remaining[d.identity()] > 0:
            remaining[d.identity()] -= 1
        elif on_changed(d):
            new.append(d)
    return sorted(new, key=lambda d: (d.file, d.line))
