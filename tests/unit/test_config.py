import tomllib

from pr_review_agent.config import (
    RepoConfig,
    detect_projects,
    load_repo_config,
    matches_any,
    render_toml,
)


def test_glob_matching():
    assert matches_any("node_modules/x/y.js", ["**/node_modules/**"])
    assert matches_any("backend/node_modules/x.js", ["**/node_modules/**"])
    assert matches_any("backend/.env", ["**/.env*"])
    assert matches_any(".env.production", ["**/.env*"])
    assert matches_any("backend/firebase-service-account.json", ["**/*service-account*.json"])
    assert matches_any("ios/Runner/x.swift", ["ios/**"])
    assert not matches_any("backend/src/ios/x.ts", ["ios/**"])
    assert not matches_any("backend/src/routes.ts", ["**/node_modules/**", "**/.env*"])


def test_routing_prefers_most_specific_enabled_project(repo_cfg):
    assert repo_cfg.project_for("backend/src/routes.ts").name == "backend"
    assert repo_cfg.project_for("ai_service/server.py").name == "ai"
    # the root Dart app is disabled, so root files belong to nobody
    assert repo_cfg.project_for("lib/main.dart") is None
    assert repo_cfg.project_for("backendish/file.ts") is None


def test_detects_monorepo_layout(monorepo):
    projects = {p.name: p for p in detect_projects(monorepo)}
    assert set(projects) == {"app", "backend", "portal", "ai_service"}
    be = projects["backend"]
    assert (be.language, be.test, be.repro_dir) == ("typescript", "npx vitest run", "test/__pr_review__")
    assert be.install.startswith("npm ci") and be.checks == ["tsc"]
    assert projects["portal"].test is None and projects["portal"].checks == ["tsc", "eslint"]
    ai = projects["ai_service"]
    assert ai.install == "pip install -r requirements-dev.txt"
    assert ai.test == "python -m pytest tests -q" and ai.repro_dir == "tests"
    assert projects["app"].language == "dart" and not projects["app"].enabled


def test_render_toml_roundtrips(monorepo):
    cfg = RepoConfig(projects=detect_projects(monorepo))
    text = render_toml(cfg)
    data = tomllib.loads(text)
    assert [p["name"] for p in data["project"]] == [p.name for p in cfg.projects]
    (monorepo / ".pr-review.toml").write_text(text)
    loaded = load_repo_config(monorepo)
    assert loaded.model_dump() == cfg.model_dump()
