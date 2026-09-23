"""Unified-diff parsing: which head lines changed, and which lines GitHub will accept comments on."""

from __future__ import annotations

from dataclasses import dataclass, field

from unidiff import PatchSet


@dataclass
class FileDiff:
    path: str  # head path (repo-relative)
    source_path: str | None  # base path; differs on renames
    status: str  # added | modified | removed | renamed
    added_lines: set[int] = field(default_factory=set)
    # Head-side lines inside a hunk (added + context). GitHub rejects review comments elsewhere (422).
    commentable_lines: set[int] = field(default_factory=set)
    patch: str = ""


@dataclass
class PrDiff:
    files: list[FileDiff]

    def get(self, path: str) -> FileDiff | None:
        return next((f for f in self.files if f.path == path), None)

    def changed_lines(self) -> dict[str, set[int]]:
        return {f.path: f.added_lines for f in self.files if f.status != "removed"}

    def touches(self, path: str, start: int, end: int, slack: int = 0) -> bool:
        f = self.get(path)
        return bool(f) and any(start - slack <= n <= end + slack for n in f.added_lines)

    def filtered(self, keep) -> PrDiff:
        return PrDiff([f for f in self.files if keep(f.path)])

    def render(self, max_chars: int = 60_000) -> str:
        """Diff text for the prompt; files that don't fit are listed so the agent can Read them."""
        parts, used, skipped = [], 0, []
        for f in self.files:
            if used + len(f.patch) > max_chars:
                skipped.append(f.path)
                continue
            parts.append(f.patch)
            used += len(f.patch)
        if skipped:
            parts.append(
                "\n# Diff truncated. These changed files were omitted; Read them directly:\n"
                + "\n".join(f"#   {p}" for p in skipped)
            )
        return "\n".join(parts)


def parse_diff(text: str) -> PrDiff:
    files = []
    for pf in PatchSet(text):
        path = _strip(pf.target_file) if not pf.is_removed_file else _strip(pf.source_file)
        source = None if pf.is_added_file else _strip(pf.source_file)
        if pf.is_added_file:
            status = "added"
        elif pf.is_removed_file:
            status = "removed"
        elif pf.is_rename:
            status = "renamed"
        else:
            status = "modified"
        fd = FileDiff(path=path, source_path=source, status=status, patch=str(pf))
        for hunk in pf:
            for line in hunk:
                if line.target_line_no is None:
                    continue
                fd.commentable_lines.add(line.target_line_no)
                if line.is_added:
                    fd.added_lines.add(line.target_line_no)
        files.append(fd)
    return PrDiff(files)


def _strip(p: str) -> str:
    return p[2:] if p.startswith(("a/", "b/")) else p
