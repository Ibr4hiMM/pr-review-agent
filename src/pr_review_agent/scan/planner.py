"""Split a codebase into risk-ranked chunks for the scan (highest risk reviewed first)."""

from __future__ import annotations

import hashlib
import math
import posixpath
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ..adapters import adapter_for
from ..config import ProjectConfig, RepoConfig
from ..files import iter_source_files
from ..models import Diagnostic
from ..workspace import git

SENSITIVE = re.compile(
    r"auth|login|session|token|passw|secret|permission|role|admin|payment|billing|checkout|upload|multer|"
    r"webhook|crypto|jwt|sql|query|supabase|firebase|route|middleware|handler|api",
    re.IGNORECASE,
)
_TS_IMPORT = re.compile(r"""(?:from\s+|import\s*\(\s*|require\s*\(\s*|import\s+)['"](\.{1,2}/[^'"]+)['"]""")
_PY_REL_IMPORT = re.compile(r"^\s*from\s+(\.+)([\w.]*)\s+import\s+([\w, ]+)", re.MULTILINE)
_PY_ABS_IMPORT = re.compile(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", re.MULTILINE)
WINDOW_OVERLAP = 60


@dataclass
class Chunk:
    project: str
    focus: list[str]
    context: list[str] = field(default_factory=list)
    risk: float = 0.0
    lines: int = 0
    # For files too big for one chunk: the 1-based inclusive line window this chunk covers.
    ranges: dict[str, tuple[int, int]] = field(default_factory=dict)

    def describe_focus(self) -> list[str]:
        return [
            f"{f} (lines {self.ranges[f][0]}-{self.ranges[f][1]} only)" if f in self.ranges else f for f in self.focus
        ]

    def covers(self, file: str, line: int, slack: int = 40) -> bool:
        if file not in self.focus:
            return False
        lo, hi = self.ranges.get(file, (1, 10**9))
        return lo - slack <= line <= hi + slack

    def cache_key(self, root: Path, extra: str) -> str:
        h = hashlib.sha256((extra + repr(sorted(self.ranges.items()))).encode())
        for rel in self.focus + self.context:
            h.update(rel.encode())
            try:
                h.update((root / rel).read_bytes())
            except OSError:
                pass
        return h.hexdigest()[:24]


def churn(root: Path, since: str = "6 months ago") -> Counter[str]:
    out = git(["log", f"--since={since}", "--name-only", "--format="], cwd=root, check=False)
    return Counter(line for line in out.splitlines() if line)


def resolve_imports(root: Path, rel: str, text: str, project: ProjectConfig, known: set[str]) -> list[str]:
    """Project-local files imported by `rel` (repo-relative), as far as we can tell statically."""
    found: list[str] = []
    here = posixpath.dirname(rel)
    if project.language == "typescript":
        for spec in _TS_IMPORT.findall(text):
            base = posixpath.normpath(posixpath.join(here, spec))
            stem = re.sub(r"\.(js|mjs|cjs|jsx)$", "", base)
            for cand in (
                base,
                stem + ".ts",
                stem + ".tsx",
                base + ".ts",
                base + ".tsx",
                base + ".js",
                base + "/index.ts",
                base + "/index.tsx",
                base + "/index.js",
            ):
                if cand in known:
                    found.append(cand)
                    break
    elif project.language == "python":
        for dots, mod, names in _PY_REL_IMPORT.findall(text):
            pkg = here
            for _ in range(len(dots) - 1):
                pkg = posixpath.dirname(pkg)
            targets = [mod] if mod else [n.strip() for n in names.split(",")]
            for t in targets:
                cand = posixpath.join(pkg, *t.split(".")) if t else pkg
                for c in (cand + ".py", cand + "/__init__.py"):
                    if c in known:
                        found.append(c)
                        break
        proj_root = project.norm_path
        for mod in _PY_ABS_IMPORT.findall(text):
            cand = posixpath.join(proj_root, *mod.split(".")) if proj_root else posixpath.join(*mod.split("."))
            for c in (cand + ".py", cand + "/__init__.py"):
                if c in known:
                    found.append(c)
                    break
    return list(dict.fromkeys(f for f in found if f != rel))


def _is_tested(rel: str, test_blob: str) -> bool:
    """Crude: does any test file mention this module's name?"""
    stem = posixpath.splitext(posixpath.basename(rel))[0]
    return stem != "index" and re.search(rf"\b{re.escape(stem)}\b", test_blob) is not None


def risk_score(rel: str, lines: int, commits: int, tested: bool, n_diags: int) -> float:
    score = 1.0
    score += 2.0 if SENSITIVE.search(rel) else 0.0
    score += math.log1p(commits)
    score += 0.0 if tested else 0.7
    score += 0.4 * min(n_diags, 5)
    score += min(lines / 300, 2.0)
    return round(score, 3)


def plan_chunks(
    root: Path,
    cfg: RepoConfig,
    projects: list[ProjectConfig],
    diagnostics: list[Diagnostic],
    max_lines: int = 1200,
    max_context: int = 8,
) -> list[Chunk]:
    commits = churn(root)
    diag_count = Counter(d.file for d in diagnostics)
    chunks: list[Chunk] = []
    for p in projects:
        adapter = adapter_for(p)
        files = list(iter_source_files(root, cfg, p))
        known = set(files)
        texts = {f: (root / f).read_text(errors="replace") for f in files}
        tests = [f for f in files if adapter.is_test_file(f)]
        test_blob = "\n".join(texts[t] for t in tests)
        sources = [f for f in files if not adapter.is_test_file(f)]
        imports = {f: resolve_imports(root, f, texts[f], p, known) for f in sources}
        line_count = {f: texts[f].count("\n") + 1 for f in sources}

        risk = {f: risk_score(f, line_count[f], commits[f], _is_tested(f, test_blob), diag_count[f]) for f in sources}
        assigned: set[str] = set()
        for f in sorted(sources, key=lambda s: -risk[s]):
            if f in assigned:
                continue
            if line_count[f] > max_lines:
                # Too big for one session: review it in overlapping windows, its imports as context.
                assigned.add(f)
                deps = imports.get(f, [])[:max_context]
                step = max_lines - WINDOW_OVERLAP
                for lo in range(1, line_count[f] + 1, step):
                    hi = min(lo + max_lines - 1, line_count[f])
                    chunks.append(
                        Chunk(
                            project=p.name,
                            focus=[f],
                            context=deps,
                            risk=risk[f],
                            lines=hi - lo + 1,
                            ranges={f: (lo, hi)},
                        )
                    )
                    if hi == line_count[f]:
                        break
                continue
            focus, total = [f], line_count[f]
            assigned.add(f)
            # Pull small, not-yet-reviewed local imports into the same chunk while it stays small.
            for dep in imports.get(f, []):
                if dep in assigned or dep not in line_count or total + line_count[dep] > max_lines:
                    continue
                focus.append(dep)
                assigned.add(dep)
                total += line_count[dep]
            ctx = [d for fo in focus for d in imports.get(fo, []) if d not in focus]
            chunks.append(
                Chunk(
                    project=p.name,
                    focus=focus,
                    context=list(dict.fromkeys(ctx))[:max_context],
                    risk=max(risk[x] for x in focus),
                    lines=total,
                )
            )
    return sorted(chunks, key=lambda c: -c.risk)
