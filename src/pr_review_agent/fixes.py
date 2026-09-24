"""Applying the agent's proposed fixes: validation, patch rendering, and apply/restore on a checkout."""

from __future__ import annotations

import difflib
import hashlib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .adapters import adapter_for
from .config import RepoConfig
from .models import FixEdit

MAX_EDITS = 20

# Files a fix check has patched right now, with their real contents. Scan chunks share one checkout, so
# everything that reads the code under review goes through `real_bytes` and never mistakes another
# chunk's trial fix for the code itself.
_PATCHED: dict[str, bytes] = {}


class EditError(ValueError):
    pass


@dataclass
class FileChange:
    file: str  # repo-relative
    original: str
    patched: str


def _key(path: Path) -> str:
    return str(path.resolve())


def real_bytes(path: Path) -> bytes:
    """`path`'s contents, or its real contents while a fix check has it patched."""
    real = _PATCHED.get(_key(path))
    return real if real is not None else path.read_bytes()


def display_text(text: str) -> str:
    """`\\n` line endings, as the dashboard and snippet matching expect."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def real_text(path: Path) -> str:
    """`real_bytes` as text for display and snippet matching (see display_text)."""
    return display_text(real_bytes(path).decode(errors="replace"))


def is_patched(path: Path) -> bool:
    """Is `path`, or a file under it, patched by a fix check right now?"""
    key = _key(path)
    return any(k == key or k.startswith(key + "/") for k in _PATCHED)


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_source(path: Path, where: str) -> str:
    try:
        return real_bytes(path).decode()
    except UnicodeDecodeError:
        raise EditError(f"{where} is not UTF-8 text, so it can't be edited safely") from None
    except OSError as e:
        raise EditError(f"could not read {where}: {e.strerror or e}") from None


def _crlf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


def plan_edits(root: Path, cfg: RepoConfig, edits: list[FixEdit]) -> list[FileChange]:
    """Validate `edits` against the checkout at `root` and compute the patched file contents.

    Edits to the same file apply in order; each `old` must occur exactly once at the time it's applied.
    Only source files in enabled projects can be changed: never tests, test helpers, config or ignored files.
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
        refusal = adapter_for(project).fix_refusal(project, rel)
        if refusal:
            raise EditError(f"edit {i}: {refusal} ({rel})")
        path = root / rel
        if not path.is_file():
            raise EditError(f"edit {i}: {rel} does not exist")
        if not e.old:
            raise EditError(f"edit {i}: `old` is empty")
        change = changes.get(rel) or FileChange(rel, *(2 * [_read_source(path, f"edit {i}: {rel}")]))
        old, new = e.old, e.new
        if change.patched.count("\r\n") * 2 > change.patched.count("\n"):
            # A CRLF file: the agent copies code with \n line endings, the file keeps its own.
            new = _crlf(new)
            if change.patched.count(old) != 1:
                old = _crlf(old)
        count = change.patched.count(old)
        if count != 1:
            where = "not found" if count == 0 else f"found {count} times"
            raise EditError(f"edit {i}: `old` text {where} in {rel}; copy it verbatim and make it unique")
        change.patched = change.patched.replace(old, new, 1)
        changes[rel] = change
    return [c for c in changes.values() if c.patched != c.original]


def _lines(text: str) -> list[str]:
    """Lines as git sees them. str.splitlines() also splits on \\r, \\f, \\u2028 and others, which
    would produce hunks that don't match the file."""
    parts = text.split("\n")
    return [line + "\n" for line in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def unified_patch(changes: list[FileChange]) -> str:
    """A git-style unified diff (apply from the repo root with `git apply`)."""
    parts = []
    for c in changes:
        diff = difflib.unified_diff(
            _lines(c.original),
            _lines(c.patched),
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
        sm = difflib.SequenceMatcher(a=_lines(c.original), b=_lines(c.patched), autojunk=False)
        lines = out.setdefault(c.file, set())
        for tag, _i1, _i2, j1, j2 in sm.get_opcodes():
            if tag != "equal":
                lines.update(range(j1 + 1, max(j2, j1 + 1) + 1))
    return out


@contextmanager
def applied(root: Path, changes: list[FileChange]):
    """Write the patched contents, and always restore the originals afterwards. Meanwhile `real_bytes`
    keeps returning the originals, so nothing else mistakes the trial fix for the code under review."""
    written: list[tuple[Path, bytes]] = []
    try:
        for c in changes:
            path = root / c.file
            original = c.original.encode()
            if path.read_bytes() != original:
                raise EditError(f"{c.file} changed after the fix was planned")
            _PATCHED[_key(path)] = original
            written.append((path, original))
            path.write_bytes(c.patched.encode())
        yield
    finally:
        for path, original in reversed(written):
            path.write_bytes(original)
            _PATCHED.pop(_key(path), None)
