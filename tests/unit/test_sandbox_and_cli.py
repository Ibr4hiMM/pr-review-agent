from pathlib import Path

from typer.testing import CliRunner

import pr_review_agent.sandbox as sandbox_mod
from pr_review_agent.cli import app
from pr_review_agent.sandbox import DockerSandbox, ExecResult


async def test_docker_run_is_offline_unprivileged_and_secret_free(monkeypatch, tmp_path):
    seen = {}

    async def fake_run(argv, cwd, env, timeout, on_timeout=None):
        seen["argv"] = argv
        return ExecResult(0, "")

    monkeypatch.setattr(sandbox_mod, "_run", fake_run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-secret")
    sb = DockerSandbox({})
    await sb.exec(tmp_path, "backend", "npx vitest run", network=False, timeout=10, image="node:20-bookworm-slim")
    argv = seen["argv"]
    joined = " ".join(argv)
    assert argv[argv.index("--network") + 1] == "none"
    assert "--read-only" in argv and "no-new-privileges" in argv and "--user" in argv
    assert argv[argv.index("-w") + 1] == "/work/backend"
    assert "sk-ant-secret" not in joined and "ANTHROPIC" not in joined
    assert argv[-3:] == ["sh", "-c", "npx vitest run"]

    await sb.exec(tmp_path, ".", "npm ci", network=True, timeout=10, image="node:20-bookworm-slim")
    assert seen["argv"][seen["argv"].index("--network") + 1] == "bridge"
    assert seen["argv"][seen["argv"].index("-w") + 1] == "/work"


def test_init_writes_config_and_workflow(monorepo: Path):
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=monorepo, check=True)
    subprocess.run(["git", "remote", "add", "origin", "git@github.com:acme/shop.git"], cwd=monorepo, check=True)
    result = CliRunner().invoke(app, ["init", str(monorepo)])
    assert result.exit_code == 0, result.output
    assert (monorepo / ".pr-review.toml").exists()
    wf = (monorepo / ".github/workflows/pr-review.yml").read_text()
    assert "git+https://github.com/acme/pr-review-agent" in wf
    assert "on:\n  pull_request:\n" in wf  # never pull_request_target
    assert "--sandbox docker" in wf
    again = CliRunner().invoke(app, ["init", str(monorepo)])
    assert "exists" in again.output  # never overwrites without --force
