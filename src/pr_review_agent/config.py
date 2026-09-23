"""Configuration: environment settings plus the per-repo `.pr-review.toml`.

A repo is split into *projects* (e.g. a monorepo with a TypeScript `backend`, a Next.js `portal`, a Python
service and a Flutter app at the root). Every file is routed to the enabled project with the longest matching path.
"""

from __future__ import annotations

import functools
import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

CONFIG_FILE = ".pr-review.toml"

Language = Literal["typescript", "python", "dart"]

DEFAULT_IGNORE = [
    "**/node_modules/**",
    "**/build/**",
    "**/dist/**",
    "**/.next/**",
    "**/out/**",
    "**/.dart_tool/**",
    "**/.venv/**",
    "**/.pr-review-venv/**",
    "ios/**",
    "android/**",
    "**/*.lock",
    "**/package-lock.json",
    "**/*.min.js",
    "**/*.g.dart",
    "**/*.freezed.dart",
    "**/.env*",
    "**/*service-account*.json",
    "**/*.pem",
    "**/*.key",
]


class Settings(BaseSettings):
    """Environment-driven settings (prefix PR_REVIEW_, except the well-known token names)."""

    model_config = SettingsConfigDict(env_prefix="PR_REVIEW_", extra="ignore")

    model: str = "claude-opus-5"
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    review_budget_usd: float = 2.0
    max_turns: int = 40
    sandbox: Literal["docker", "local"] = "docker"
    cache_dir: Path = Path.home() / ".cache" / "pr-review-agent"
    data_dir: Path = Path.home() / ".local" / "share" / "pr-review-agent"
    test_timeout_s: int = 300
    install_timeout_s: int = 900

    @property
    def runs_dir(self) -> Path:
        return self.data_dir / "runs"

    @property
    def anthropic_api_key(self) -> str | None:
        return os.environ.get("ANTHROPIC_API_KEY")


@functools.cache
def github_token() -> str | None:
    """GITHUB_TOKEN / GH_TOKEN, falling back to the `gh` CLI's login."""
    for var in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    if shutil.which("gh"):
        proc = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    return None


class ProjectConfig(BaseModel):
    name: str
    path: str = "."
    language: Language
    enabled: bool = True
    install: str | None = None
    test: str | None = None
    # Where repro tests are written, relative to the project (must be picked up by the test runner).
    repro_dir: str | None = None
    checks: list[str] = Field(default_factory=list)
    image: str | None = None  # docker image override

    @property
    def norm_path(self) -> str:
        return "" if self.path in (".", "", "./") else self.path.strip("/").removeprefix("./")

    def contains(self, repo_path: str) -> bool:
        return not self.norm_path or repo_path == self.norm_path or repo_path.startswith(self.norm_path + "/")

    def rel(self, repo_path: str) -> str:
        """Repo-relative path -> project-relative path."""
        return repo_path[len(self.norm_path) + 1 :] if self.norm_path else repo_path


class Thresholds(BaseModel):
    possible_min_confidence: float = 0.8
    max_inline_comments: int = 10


class RepoConfig(BaseModel):
    ignore: list[str] = Field(default_factory=lambda: list(DEFAULT_IGNORE))
    thresholds: Thresholds = Field(default_factory=Thresholds)
    projects: list[ProjectConfig] = Field(default_factory=list)

    def enabled_projects(self) -> list[ProjectConfig]:
        return [p for p in self.projects if p.enabled]

    def project(self, name: str) -> ProjectConfig:
        for p in self.projects:
            if p.name == name:
                return p
        raise KeyError(f"no project named {name!r} in {CONFIG_FILE}")

    def project_for(self, repo_path: str) -> ProjectConfig | None:
        candidates = [p for p in self.enabled_projects() if p.contains(repo_path)]
        return max(candidates, key=lambda p: len(p.norm_path), default=None)

    def is_ignored(self, repo_path: str) -> bool:
        return matches_any(repo_path, self.ignore)


@functools.cache
def _glob_regex(pattern: str) -> re.Pattern[str]:
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def matches_any(path: str, patterns: list[str]) -> bool:
    path = path.removeprefix("./")
    return any(_glob_regex(p).match(path) for p in patterns)


def load_repo_config(repo_root: Path) -> RepoConfig:
    """Read `.pr-review.toml`; fall back to auto-detection when the repo has none."""
    cfg_path = repo_root / CONFIG_FILE
    if not cfg_path.exists():
        return RepoConfig(projects=detect_projects(repo_root))
    data = tomllib.loads(cfg_path.read_text())
    data["projects"] = data.pop("project", [])
    return RepoConfig.model_validate(data)


# --- Detection (used by `pr-review init` and as the fallback) ------------------------------------

_SKIP_DIRS = {
    "node_modules",
    ".git",
    "build",
    "dist",
    ".next",
    "out",
    "ios",
    "android",
    ".dart_tool",
    ".venv",
    "venv",
    ".pr-review-venv",
    "__pycache__",
    "assets",
}


def _candidate_dirs(root: Path, max_depth: int = 2) -> list[Path]:
    found = [root]
    frontier = [root]
    for _ in range(max_depth):
        nxt = []
        for d in frontier:
            for child in sorted(d.iterdir()):
                if child.is_dir() and child.name not in _SKIP_DIRS and not child.name.startswith("."):
                    nxt.append(child)
        found += nxt
        frontier = nxt
    return found


def _detect_ts(d: Path, rel: str) -> ProjectConfig | None:
    pkg_file = d / "package.json"
    if not pkg_file.exists():
        return None
    try:
        pkg = json.loads(pkg_file.read_text())
    except json.JSONDecodeError:
        return None
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    if "typescript" not in deps and not (d / "tsconfig.json").exists():
        return None
    if (d / "pnpm-lock.yaml").exists():
        install = "corepack enable && pnpm install --frozen-lockfile"
    elif (d / "yarn.lock").exists():
        install = "corepack enable && yarn install --frozen-lockfile"
    elif (d / "package-lock.json").exists():
        install = "npm ci --no-audit --no-fund"
    else:
        install = "npm install --no-audit --no-fund"
    test = repro_dir = None
    if "vitest" in deps:
        test = "npx vitest run"
        repro_dir = f"{_vitest_test_root(d)}/__pr_review__".lstrip("/")
    checks = []
    if (d / "tsconfig.json").exists():
        checks.append("tsc")
    if "eslint" in deps or any(d.glob("eslint.config.*")) or any(d.glob(".eslintrc*")):
        checks.append("eslint")
    return ProjectConfig(
        name=_project_name(rel, d),
        path=rel,
        language="typescript",
        install=install,
        test=test,
        repro_dir=repro_dir,
        checks=checks,
    )


def _vitest_test_root(d: Path) -> str:
    """First directory of the vitest `include` glob (e.g. `test/**/*.test.ts` -> `test`)."""
    for cfg in d.glob("vitest.config.*"):
        m = re.search(r"include\s*:\s*\[\s*['\"]([^'\"*]+)/", cfg.read_text())
        if m:
            return m.group(1)
    for name in ("test", "tests", "__tests__", "src"):
        if (d / name).is_dir():
            return name
    return "test"


def _detect_python(d: Path, rel: str) -> ProjectConfig | None:
    reqs = [n for n in ("requirements-dev.txt", "requirements.txt") if (d / n).exists()]
    has_pyproject = (d / "pyproject.toml").exists()
    if not reqs and not has_pyproject:
        return None
    if not any(d.glob("*.py")) and not any(d.glob("*/*.py")):
        return None
    install = f"pip install -r {reqs[0]}" if reqs else "pip install -e ."
    tests_dir = next((n for n in ("tests", "test") if (d / n).is_dir()), None)
    test = f"python -m pytest {tests_dir} -q" if tests_dir else "python -m pytest -q"
    return ProjectConfig(
        name=_project_name(rel, d),
        path=rel,
        language="python",
        install=install,
        test=test,
        repro_dir=tests_dir or ".",
        checks=["ruff"],
    )


def _detect_dart(d: Path, rel: str) -> ProjectConfig | None:
    if not (d / "pubspec.yaml").exists():
        return None
    return ProjectConfig(
        name=_project_name(rel, d, fallback="app"),
        path=rel,
        language="dart",
        enabled=False,
        install="flutter pub get",
        test="flutter test",
        repro_dir="test/__pr_review__",
        checks=["dart-analyze"],
    )


def _project_name(rel: str, d: Path, fallback: str | None = None) -> str:
    if rel in (".", ""):
        return fallback or d.name.lower()
    return re.sub(r"[^a-z0-9_-]+", "-", rel.lower()).strip("-")


def detect_projects(root: Path) -> list[ProjectConfig]:
    projects: list[ProjectConfig] = []
    for d in _candidate_dirs(root):
        rel = "." if d == root else d.relative_to(root).as_posix()
        for detect in (_detect_ts, _detect_python, _detect_dart):
            p = detect(d, rel)
            if p:
                projects.append(p)
                break
    names: set[str] = set()
    for p in projects:  # keep names unique
        base, n = p.name, 2
        while p.name in names:
            p.name = f"{base}-{n}"
            n += 1
        names.add(p.name)
    return projects


def render_toml(cfg: RepoConfig) -> str:
    """Human-friendly TOML for `pr-review init` (tomllib can read but not write)."""

    def q(v: object) -> str:
        return json.dumps(v)

    lines = [
        "# pr-review-agent configuration. Read from the PR's *base* branch during reviews.",
        "",
        "ignore = [",
        *[f"  {q(p)}," for p in cfg.ignore],
        "]",
        "",
        "[thresholds]",
        f"possible_min_confidence = {cfg.thresholds.possible_min_confidence}",
        f"max_inline_comments = {cfg.thresholds.max_inline_comments}",
    ]
    for p in cfg.projects:
        lines += ["", "[[project]]", f"name = {q(p.name)}", f"path = {q(p.path)}", f"language = {q(p.language)}"]
        if not p.enabled:
            lines.append("enabled = false  # language adapter not implemented yet")
        for key in ("install", "test", "repro_dir", "image"):
            val = getattr(p, key)
            if val is not None:
                lines.append(f"{key} = {q(val)}")
        lines.append(f"checks = {q(p.checks)}")
        if p.language == "typescript" and not p.test:
            lines.append("# no vitest found: findings here can only carry static/code-reference evidence")
    return "\n".join(lines) + "\n"
