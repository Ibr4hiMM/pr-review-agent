"""Docker sandbox against a real daemon. Run with: uv run pytest -m docker"""

import asyncio
import shutil
import subprocess

import pytest

from pr_review_agent.sandbox import DockerSandbox

pytestmark = pytest.mark.docker


@pytest.fixture(scope="module")
def sandbox():
    if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        pytest.skip("docker daemon not available")
    return DockerSandbox({})


def test_offline_run_has_no_network_and_no_secrets(sandbox, tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    probe = "fetch('https://example.com').then(() => console.log('NET-OK')).catch(() => console.log('NET-BLOCKED'))"
    res = asyncio.run(
        sandbox.exec(
            tmp_path, ".", f'node -e "{probe}"; env', network=False, timeout=120, image="node:20-bookworm-slim"
        )
    )
    assert "NET-BLOCKED" in res.output, res.output
    assert "sk-ant-should-not-leak" not in res.output


def test_workspace_is_writable_but_root_fs_is_not(sandbox, tmp_path):
    res = asyncio.run(
        sandbox.exec(
            tmp_path,
            ".",
            "echo ok > /work/out.txt && (touch /etc/x 2>/dev/null || echo RO)",
            network=False,
            timeout=120,
            image="node:20-bookworm-slim",
        )
    )
    assert (tmp_path / "out.txt").read_text().strip() == "ok"
    assert "RO" in res.output
