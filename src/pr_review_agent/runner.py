"""Runs a project's install / tests / repro tests / static checks inside the sandbox."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from pathlib import Path

from .adapters import adapter_for
from .adapters.base import REPORT_DIR, CheckSpec, PathMap
from .config import ProjectConfig, Settings
from .models import Diagnostic, TestRun
from .sandbox import ExecResult, Sandbox, _run

log = logging.getLogger(__name__)

MAX_REPRO_CHARS = 20_000


class InstallError(RuntimeError):
    pass


class ProjectRunner:
    def __init__(self, sandbox: Sandbox, settings: Settings, max_parallel: int = 2):
        self.sandbox = sandbox
        self.settings = settings
        self._installs: dict[tuple[str, str], asyncio.Task[ExecResult]] = {}
        self._sem = asyncio.Semaphore(max_parallel)
        # (root, project, sha256(test_code)) -> result, so verification can reuse runs we made ourselves.
        self.repro_results: dict[tuple[str, str, str], TestRun] = {}

    def _image(self, p: ProjectConfig) -> str:
        return p.image or adapter_for(p).default_image

    def _paths(self, root: Path, p: ProjectConfig) -> PathMap:
        return PathMap(project=p, host_project_dir=root / p.norm_path if p.norm_path else root)

    async def _exec(self, root: Path, p: ProjectConfig, cmd: str, *, network: bool, timeout: int) -> ExecResult:
        workdir = p.norm_path or "."
        (root / workdir / REPORT_DIR).mkdir(parents=True, exist_ok=True)
        full = adapter_for(p).wrap(cmd) if not network else cmd
        log.debug("exec [%s] %s: %s", p.name, "net" if network else "no-net", full)
        return await self.sandbox.exec(root, workdir, full, network=network, timeout=timeout, image=self._image(p))

    # --- install ---------------------------------------------------------------------------------

    async def ensure_installed(self, root: Path, p: ProjectConfig) -> ExecResult:
        key = (str(root), p.name)
        if key not in self._installs:
            self._installs[key] = asyncio.create_task(self._install(root, p))
        res = await self._installs[key]
        if not res.ok:
            raise InstallError(
                f"install failed for {p.name} ({'timeout' if res.timed_out else res.exit_code}):\n" + res.tail(3000)
            )
        return res

    async def _install(self, root: Path, p: ProjectConfig) -> ExecResult:
        cmd = adapter_for(p).install_cmd(p)
        if not cmd:
            return ExecResult(0, "no install step")
        log.info("installing %s in %s", p.name, root.name)
        return await self._exec(root, p, cmd, network=True, timeout=self.settings.install_timeout_s)

    # --- tests -----------------------------------------------------------------------------------

    async def run_tests(self, root: Path, p: ProjectConfig, files: list[str] | None = None) -> TestRun:
        adapter = adapter_for(p)
        if not adapter.can_run_tests(p):
            raise ValueError(f"project {p.name} has no supported test runner configured")
        await self.ensure_installed(root, p)
        report_rel = f"{REPORT_DIR}/tests-{uuid.uuid4().hex[:8]}.out"
        report_path = self._paths(root, p).host_project_dir / report_rel
        async with self._sem:
            res = await self._exec(
                root, p, adapter.test_cmd(p, files, report_rel), network=False, timeout=self.settings.test_timeout_s
            )
        report = report_path.read_text(errors="replace") if report_path.exists() else None
        report_path.unlink(missing_ok=True)
        return adapter.parse_test_report(report, res, self._paths(root, p))

    async def run_repro(self, root: Path, p: ProjectConfig, test_code: str) -> tuple[TestRun, str]:
        """Write `test_code` into the project's repro dir, run only that file, remove it again.
        Returns the result and the repo-relative path the test was saved at."""
        if len(test_code) > MAX_REPRO_CHARS:
            raise ValueError(f"test is too long ({len(test_code)} chars, max {MAX_REPRO_CHARS})")
        digest = hashlib.sha256(test_code.encode()).hexdigest()
        adapter = adapter_for(p)
        rel = adapter.repro_file(p, digest[:10])
        repo_rel = f"{p.norm_path}/{rel}" if p.norm_path else rel
        cache_key = (str(root), p.name, digest)
        if cache_key in self.repro_results:
            return self.repro_results[cache_key], repo_rel
        target = self._paths(root, p).host_project_dir / rel
        created_dir = not target.parent.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(test_code)
        try:
            run = await self.run_tests(root, p, files=[rel])
        finally:
            target.unlink(missing_ok=True)
            if created_dir and target.parent.exists() and not any(target.parent.iterdir()):
                target.parent.rmdir()
        self.repro_results[cache_key] = run
        return run, repo_rel

    # --- static checks ---------------------------------------------------------------------------

    async def run_checks(self, root: Path, p: ProjectConfig) -> tuple[list[Diagnostic], list[str]]:
        """All configured static checks. Returns (diagnostics, human-readable errors)."""
        diags: list[Diagnostic] = []
        errors: list[str] = []
        for spec in adapter_for(p).checks(p):
            try:
                diags += await self._run_check(root, p, spec)
            except Exception as e:  # a broken linter shouldn't abort the review
                errors.append(f"{p.name}/{spec.name}: {e}")
        return diags, errors

    async def _run_check(self, root: Path, p: ProjectConfig, spec: CheckSpec) -> list[Diagnostic]:
        paths = self._paths(root, p)
        if spec.host_argv:
            env = {"PATH": os.environ.get("PATH", ""), "HOME": str(self.settings.cache_dir)}
            res = await _run(spec.host_argv, paths.host_project_dir, env, 300)
        else:
            await self.ensure_installed(root, p)
            async with self._sem:
                res = await self._exec(root, p, spec.cmd or "", network=False, timeout=self.settings.test_timeout_s)
        if res.timed_out:
            raise RuntimeError("timed out")
        if spec.report:
            report_file = paths.host_project_dir / spec.report
            if not report_file.exists():
                raise RuntimeError(f"no report written (exit {res.exit_code}): {res.tail(800)}")
            text = report_file.read_text(errors="replace")
            report_file.unlink(missing_ok=True)
        else:
            text = res.output
        diags = spec.parse(text, paths)
        if res.exit_code != 0 and not diags:
            # Non-zero exit with nothing parsed means the tool itself failed; don't report "clean".
            raise RuntimeError(f"exit {res.exit_code} with no parseable diagnostics: {res.tail(800)}")
        return diags
