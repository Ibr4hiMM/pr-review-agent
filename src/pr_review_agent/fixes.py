"""Applying the agent's proposed fixes: validation, patch rendering, and apply/restore on a checkout."""

from __future__ import annotations

import difflib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .adapters import adapter_for
from .config import RepoConfig
from .models import FixEdit

MAX_EDITS = 20


class EditError(ValueError):
    pass


@dataclass
class FileChange:
    file: str  # repo-relative
    original: str
    patched: str


def plan_edits(root: Path, cfg: RepoConfig, edits: list[FixEdit]) -> list[FileChange]:
    """Validate `edits` against the checkout at `root` and compute the patched file contents.

    Edits to the same file apply in order; each `old` must occur exactly once at the time it's applied.
    Test files, ignored files and files outside enabled projects can't be changed.
    """
    if not edits:
        raise EditError("no edits")
    if len(edits) > MAX_EDITS:
        raise EditError(f"too many edits ({len(edits)}, max {MAX_EDITS})")
    changes: dict[str, FileChange] = {}
    for i, e in enumerate(edits, 1):
        rel = e.file.strip().removeprefix("./")
        if not rel or rel.startswith("/") or ".." in Path(rel).parts:
            raise EditError(f"edit {i}: invalid path {e.file!r}")
        if cfg.is_ignored(rel):
            raise EditError(f"edit {i}: {rel} is ignored")
        project = cfg.project_for(rel)
        if project is None:
            raise EditError(f"edit {i}: {rel} is not in an enabled project")
        if adapter_for(project).is_test_file(rel):
            raise EditError(f"edit {i}: fixes may not modify test files ({rel})")
        path = root / rel
        if not path.is_file():
            raise EditError(f"edit {i}: {rel} does not exist")
        if not e.old:
            raise EditError(f"edit {i}: `old` is empty")
        change = changes.get(rel) or FileChange(rel, *(2 * [path.read_text()]))
        count = change.patched.count(e.old)
        if count != 1:
            where = "not found" if count == 0 else f"found {count} times"
            raise EditError(f"edit {i}: `old` text {where} in {rel}; copy it verbatim and make it unique")
        change.patched = change.patched.replace(e.old, e.new, 1)
        changes[rel] = change
    return [c for c in changes.values() if c.patched != c.original]


def unified_patch(changes: list[FileChange]) -> str:
    """A git-style unified diff (apply from the repo root with `git apply`)."""
    parts = []
    for c in changes:
        diff = difflib.unified_diff(
            c.original.splitlines(keepends=True),
            c.patched.splitlines(keepends=True),
            fromfile=f"a/{c.file}",
            tofile=f"b/{c.file}",
        )
        text = "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in diff)
        parts.append(f"diff --git a/{c.file} b/{c.file}\n{text}")
    return "".join(parts)


def changed_lines(changes: list[FileChange]) -> dict[str, set[int]]:
    """Patched-file line numbers touched by the changes (for filtering diagnostics)."""
    out: dict[str, set[int]] = {}
    for c in changes:
        sm = difflib.SequenceMatcher(a=c.original.splitlines(), b=c.patched.splitlines(), autojunk=False)
        lines = out.setdefault(c.file, set())
        for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
            if tag != "equal":
                lines.update(range(j1 + 1, max(j2, j1 + 1) + 1))
    return out


@contextmanager
def applied(root: Path, changes: list[FileChange]):
    """Write the patched contents, and always restore the originals afterwards."""
    written: list[FileChange] = []
    try:
        for c in changes:
            (root / c.file).write_text(c.patched)
            written.append(c)
        yield
    finally:
        for c in written:
            (root / c.file).write_text(c.original)
