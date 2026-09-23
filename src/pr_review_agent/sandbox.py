"""Where untrusted repo code runs (installs, test suites, repro tests, linters with JS configs).

Two backends:
- `DockerSandbox` (default, and the only one used in CI): `--network none` for test runs, read-only
  root filesystem, CPU/memory/pid caps, no secrets in the environment.
- `LocalSandbox` (for scanning your own repos before Docker is installed): runs on the host with a
  scrubbed environment and a throwaway HOME. On macOS it also uses `sandbox-exec` to block network
  access and writes outside the workspace during test runs.

Both mount/run inside a *workspace root* (a clone or worktree we created), never your working copy.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import signal
import sys
import tempfile
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

OUTPUT_LIMIT = 200_000


@dataclass
class ExecResult:
    exit_code: int
    output: str  # stdout + stderr, interleaved
    timed_out: bool = False
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def tail(self, n: int = 4000) -> str:
        return self.output if len(self.output) <= n else "…(truncated)…\n" + self.output[-n:]


class Sandbox(ABC):
    name: str
    network_isolated: bool

    @abstractmethod
    async def exec(
        self,
        root: Path,
        workdir: str,
        cmd: str,
        *,
        network: bool,
        timeout: int,
        image: str | None = None,
    ) -> ExecResult:
        """Run `cmd` with cwd `root/workdir`. `network=False` must block outbound network."""

    @abstractmethod
    async def available(self) -> tuple[bool, str]: ...


async def _run(argv: list[str], cwd: Path, env: dict[str, str], timeout: int, on_timeout=None) -> ExecResult:
    loop = asyncio.get_running_loop()
    start = loop.time()
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
        start_new_session=True,  # own process group so a timeout kills the whole tree
    )
    timed_out = False
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.CancelledError:
        # The job was cancelled: don't leave test runs or installs behind.
        if on_timeout:
            await asyncio.shield(on_timeout())
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        raise
    except TimeoutError:
        timed_out = True
        if on_timeout:
            await on_timeout()
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, _ = await proc.communicate()
    text = (out or b"").decode(errors="replace")
    if len(text) > OUTPUT_LIMIT:
        text = text[:20_000] + "\n…(output truncated)…\n" + text[-(OUTPUT_LIMIT - 20_000) :]
    return ExecResult(
        exit_code=proc.returncode if proc.returncode is not None else -1,
        output=text,
        timed_out=timed_out,
        duration_s=loop.time() - start,
    )


class LocalSandbox(Sandbox):
    name = "local"

    def __init__(self, cache_dir: Path):
        self.cache_dir = cache_dir
        self.home = Path(tempfile.mkdtemp(prefix="pr-review-home-"))
        self._seatbelt = sys.platform == "darwin" and shutil.which("sandbox-exec") is not None
        self.network_isolated = self._seatbelt

    async def available(self) -> tuple[bool, str]:
        note = "network blocked via sandbox-exec" if self._seatbelt else "WARNING: no network isolation"
        return True, f"local sandbox ({note}); runs repo code on this machine"

    def _env(self) -> dict[str, str]:
        # Allowlist, so API keys and tokens in our own environment never reach repo code.
        env = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "SHELL") if k in os.environ}
        tmp = self.home / "tmp"
        tmp.mkdir(exist_ok=True)
        env.update(
            HOME=str(self.home),
            TMPDIR=str(tmp),
            CI="1",
            TERM="dumb",
            NO_COLOR="1",
            npm_config_cache=str(self.cache_dir / "npm"),
            npm_config_update_notifier="false",
            PIP_CACHE_DIR=str(self.cache_dir / "pip"),
            PIP_DISABLE_PIP_VERSION_CHECK="1",
        )
        return env

    def _seatbelt_profile(self, root: Path) -> str:
        home = str(Path.home().resolve())
        allowed_writes = [
            root.resolve(),
            self.home.resolve(),
            Path(tempfile.gettempdir()).resolve(),
            Path("/private/var/folders"),
        ]
        rules = [
            "(version 1)",
            "(allow default)",
            "(deny network-outbound (remote ip))",
            # Local servers (supertest, test databases) still need loopback.
            '(allow network-outbound (remote ip "localhost:*"))',
            # No reading file contents in, or writing to, the real home directory (ssh keys, tokens,
            # other repos). Metadata stays readable so path resolution (realpath/lstat) still works…
            f'(deny file-read-data file-write* (subpath "{home}"))',
            # …except the workspace, our throwaway HOME and the shared package caches.
            *[f'(allow file-read-data file-write* (subpath "{p}"))' for p in allowed_writes],
            f'(allow file-read-data (subpath "{self.cache_dir.resolve()}"))',
            # Toolchains installed under HOME (uv-managed Python, nvm, pyenv, …) stay readable.
            *[f'(allow file-read-data (subpath "{p}"))' for p in _toolchain_dirs(Path(home))],
        ]
        return "".join(rules)

    async def exec(self, root, workdir, cmd, *, network, timeout, image=None) -> ExecResult:
        cwd = (root / workdir).resolve()
        argv = ["/bin/sh", "-c", cmd]
        if not network and self._seatbelt:
            argv = ["sandbox-exec", "-p", self._seatbelt_profile(root), *argv]
        return await _run(argv, cwd, self._env(), timeout)


def _toolchain_dirs(home: Path) -> list[Path]:
    """Install prefixes of the interpreters on PATH that live under HOME (read-only access)."""
    dirs = {home / d for d in (".nvm", ".pyenv", ".volta", ".asdf", ".bun", ".local/share/uv", ".local/share/fnm")}
    for tool in ("node", "npm", "npx", "python3", "python", "pnpm", "yarn"):
        found = shutil.which(tool)
        if found:
            real = Path(found).resolve()
            if real.is_relative_to(home):
                dirs.add(real.parent.parent)  # <prefix>/bin/<tool> -> <prefix>
    return sorted(d for d in dirs if d.exists())


class DockerSandbox(Sandbox):
    name = "docker"
    network_isolated = True

    def __init__(self, default_images: dict[str, str]):
        self.default_images = default_images

    async def available(self) -> tuple[bool, str]:
        if not shutil.which("docker"):
            return False, "Docker isn't installed"
        res = await _run(["docker", "info", "--format", "{{.ServerVersion}}"], Path.cwd(), dict(os.environ), 30)
        if not res.ok:
            return False, "Docker is installed but not running (open Docker Desktop)"
        return True, f"docker {res.output.strip()}"

    async def exec(self, root, workdir, cmd, *, network, timeout, image=None) -> ExecResult:
        if image is None:
            raise ValueError("docker sandbox needs an image")
        name = f"pr-review-{uuid.uuid4().hex[:10]}"
        argv = [
            "docker",
            "run",
            "--rm",
            "--name",
            name,
            "--network",
            "bridge" if network else "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,exec,size=2g",
            "--memory",
            "4g",
            "--cpus",
            "2",
            "--pids-limit",
            "2048",
            "--security-opt",
            "no-new-privileges",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-e",
            "HOME=/tmp",
            "-e",
            "CI=1",
            "-e",
            "NO_COLOR=1",
            "-e",
            "npm_config_cache=/tmp/.npm",
            "-e",
            "PIP_CACHE_DIR=/tmp/.pip",
            "-v",
            f"{root.resolve()}:/work",
            "-w",
            f"/work/{workdir}".rstrip("/.") or "/work",
            image,
            "sh",
            "-c",
            cmd,
        ]

        async def kill():
            await _run(["docker", "kill", name], Path.cwd(), dict(os.environ), 30)

        # Only docker's own config needs our env; nothing is forwarded into the container.
        return await _run(argv, root, dict(os.environ), timeout, on_timeout=kill)


async def pick_sandbox(kind: str, cache_dir: Path) -> tuple[Sandbox, str | None]:
    """`auto` means Docker when it's usable, else the local sandbox (never in CI). Returns a warning, if any."""
    note = None
    if kind == "auto":
        ok, why = await DockerSandbox({}).available()
        if ok:
            kind = "docker"
        elif os.environ.get("GITHUB_ACTIONS") == "true":
            raise RuntimeError(f"docker is required in CI: {why}")
        else:
            note = f"{why}, so tests run in the local sandbox (network and home folder blocked)."
            kind = "local"
    sandbox = make_sandbox(kind, cache_dir)
    ok, why = await sandbox.available()
    if not ok:
        raise RuntimeError(why)
    return sandbox, note


def make_sandbox(kind: str, cache_dir: Path) -> Sandbox:
    if kind == "docker":
        return DockerSandbox({"typescript": "node:20-bookworm-slim", "python": "python:3.12-slim"})
    if kind == "local":
        return LocalSandbox(cache_dir)
    raise ValueError(f"unknown sandbox {kind!r}")


def q(s: str) -> str:
    return shlex.quote(s)
