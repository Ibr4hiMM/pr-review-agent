import subprocess

from pr_review_agent.config import ProjectConfig, RepoConfig
from pr_review_agent.scan.planner import plan_chunks, resolve_imports


def test_ts_import_resolution(tmp_path):
    known = {"backend/src/routes.ts", "backend/src/services/auth.ts", "backend/src/lib/index.ts"}
    text = (
        "import { a } from './services/auth.js';\nimport lib from './lib';\n"
        "import express from 'express';\nconst x = require('./missing');"
    )
    p = ProjectConfig(name="backend", path="backend", language="typescript")
    assert resolve_imports(tmp_path, "backend/src/routes.ts", text, p, known) == [
        "backend/src/services/auth.ts",
        "backend/src/lib/index.ts",
    ]


def test_python_import_resolution(tmp_path):
    known = {"ai_service/server.py", "ai_service/tools/db_tool.py", "ai_service/ai.py"}
    text = "from .tools.db_tool import run\nfrom . import ai\nimport os\n"
    p = ProjectConfig(name="ai", path="ai_service", language="python")
    assert resolve_imports(tmp_path, "ai_service/server.py", text, p, known) == [
        "ai_service/tools/db_tool.py",
        "ai_service/ai.py",
    ]


def test_chunks_are_risk_ranked_and_group_imports(tmp_path):
    src = tmp_path / "backend/src"
    (src / "services").mkdir(parents=True)
    (tmp_path / "backend/test").mkdir()
    (src / "routes.ts").write_text("import { check } from './services/auth';\n" + "x\n" * 50)
    (src / "services/auth.ts").write_text("export const check = 1;\n")
    (src / "format.ts").write_text("export const f = 1;\n")
    (tmp_path / "backend/test/format.test.ts").write_text("import { f } from '../src/format';\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    cfg = RepoConfig(projects=[ProjectConfig(name="backend", path="backend", language="typescript")])
    chunks = plan_chunks(tmp_path, cfg, cfg.projects, [])
    assert chunks[0].focus == ["backend/src/routes.ts", "backend/src/services/auth.ts"]
    assert chunks[-1].focus == ["backend/src/format.ts"]  # tested, not sensitive -> lowest risk
    assert all(not c.focus[0].endswith(".test.ts") for c in chunks)


def test_huge_files_are_split_into_overlapping_windows(tmp_path):
    src = tmp_path / "backend/src"
    src.mkdir(parents=True)
    (src / "routes.ts").write_text("import { a } from './a';\n" + "x\n" * 2999)
    (src / "a.ts").write_text("export const a = 1;\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    cfg = RepoConfig(projects=[ProjectConfig(name="backend", path="backend", language="typescript")])
    chunks = [c for c in plan_chunks(tmp_path, cfg, cfg.projects, [], max_lines=1200) if c.ranges]
    assert [c.ranges["backend/src/routes.ts"] for c in chunks] == [(1, 1200), (1141, 2340), (2281, 3001)]
    assert all(c.context == ["backend/src/a.ts"] for c in chunks)
    assert chunks[1].covers("backend/src/routes.ts", 1150) and not chunks[1].covers("backend/src/routes.ts", 2500)
    assert "lines 1141-2340 only" in chunks[1].describe_focus()[0]
