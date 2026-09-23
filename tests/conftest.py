from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from pr_review_agent.config import ProjectConfig, RepoConfig


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def monorepo(tmp_path: Path) -> Path:
    """Minimal monorepo: TS backend (vitest), Next portal, Python service, Flutter root."""
    (tmp_path / "backend/src").mkdir(parents=True)
    (tmp_path / "backend/test").mkdir()
    (tmp_path / "backend/package.json").write_text(
        '{"devDependencies": {"typescript": "^5", "vitest": "^3"}, "dependencies": {"express": "^4"}}'
    )
    (tmp_path / "backend/package-lock.json").write_text("{}")
    (tmp_path / "backend/tsconfig.json").write_text("{}")
    (tmp_path / "backend/vitest.config.ts").write_text(
        "export default defineConfig({ test: { include: ['test/**/*.test.ts'] } });"
    )
    (tmp_path / "portal/src").mkdir(parents=True)
    (tmp_path / "portal/package.json").write_text('{"devDependencies": {"typescript": "^5", "eslint": "^9"}}')
    (tmp_path / "portal/tsconfig.json").write_text("{}")
    (tmp_path / "ai_service/tests").mkdir(parents=True)
    (tmp_path / "ai_service/requirements.txt").write_text("fastapi\n")
    (tmp_path / "ai_service/requirements-dev.txt").write_text("-r requirements.txt\npytest\n")
    (tmp_path / "ai_service/server.py").write_text("x = 1\n")
    (tmp_path / "pubspec.yaml").write_text("name: app\n")
    (tmp_path / "node_modules/foo").mkdir(parents=True)
    (tmp_path / "node_modules/foo/package.json").write_text('{"devDependencies": {"typescript": "1"}}')
    return tmp_path


@pytest.fixture
def repo_cfg() -> RepoConfig:
    return RepoConfig(
        projects=[
            ProjectConfig(name="app", path=".", language="dart", enabled=False),
            ProjectConfig(
                name="backend",
                path="backend",
                language="typescript",
                test="npx vitest run",
                repro_dir="test/__pr_review__",
                checks=["tsc"],
            ),
            ProjectConfig(
                name="ai",
                path="ai_service",
                language="python",
                test="python -m pytest tests -q",
                repro_dir="tests",
                checks=["ruff"],
            ),
        ]
    )
