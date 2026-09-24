"""Runs a project's install / tests / repro tests / static checks inside the sandbox."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .adapters import adapter_for
from .adapters.base import REPORT_DIR, CheckSpec, PathMap, broken_test_reason, is_broken_case
from .config import ProjectConfig, Settings
from .fixes import FileChange, applied
from .models import Diagnostic, TestRun
from .sandbox import ExecResult, Sandbox, _run

log = logging.getLogger(__name__)

MAX_REPRO_CHARS = 20_000
# A fix must make the repro pass this many runs in a row: a flaky test that passes once by chance
# (typical of the race conditions the agent hunts for) must not verify it.
REPRO_RUNS_WITH_FIX = 3


class InstallError(RuntimeError):
    pass


class _RWLock:
    """Many test runs may share a checkout; a fix check needs it to itself while files are patched."""

    def __init__(self) -> None:
        self._readers = 0
        self._writer = False
        self._cond = asyncio.Condition()

    @asynccontextmanager
    async def shared(self) -> AsyncIterator[None]:
        async with self._cond:
            await self._cond.wait_for(lambda: not self._writer)
            self._readers += 1
        try:
            yield
        finally:
            async with self._cond:
                self._readers -= 1
                self._cond.notify_all()

    @asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        async with self._cond:
            await self._cond.wait_for(lambda: not self._writer and self._readers == 0)
            self._writer = True
        try:
            yield
        finally:
            async with self._cond:
                self._writer = False
                self._cond.notify_all()


def _short(test_id: str) -> str:
    return test_id.split("::")[-1]


def _first_line(message: str | None) -> str:
    lines = (message or "").strip().splitlines()
    return lines[0][:160] if lines else "failed"


def _compare_repro(before: TestRun, after: TestRun, partial_ok: bool = False) -> tuple[bool, str]:
    """Judge a fix by the repro test's cases: every case that genuinely failed without the fix must pass
    with it, and every case that passed must still pass. Cases that failed only because the test itself
    is broken (a bad import, a missing fixture) prove nothing either way and are left out.

    `partial_ok` is for a test file that other findings cite too: their cases may keep failing, as long
    as at least one case passes with this fix."""
    broken = broken_test_reason(after)
    if broken and not after.cases:
        return False, f"could not run with the fix ({broken})"
    now = {c.id: c for c in after.cases}
    was_failing = [c.id for c in before.failed if not is_broken_case(c)]
    if not was_failing:
        return False, "does not fail without the fix, so it can't show that the fix works"
    stopped = [c.id for c in before.passed if c.id not in now or now[c.id].status != "passed"]
    if stopped:
        return False, "fails in new places with the fix: " + ", ".join(_short(t) for t in stopped[:3])
    still = [t for t in was_failing if t not in now or now[t].status != "passed"]
    ignored = len(before.failed) - len(was_failing)
    aside = f" ({ignored} case(s) that only failed because the test is broken were ignored)" if ignored else ""
    if not still:
        return True, "passes with the fix" + aside
    why = "; ".join(f"{_short(t)}: {_first_line(now[t].message) if t in now else 'did not run'}" for t in still[:2])
    fixed = len(was_failing) - len(still)
    if fixed and partial_ok:
        return True, (
            f"passes with the fix for {fixed} of {len(was_failing)} failing cases; the others belong to another "
            "finding that cites the same test" + aside
        )
    if fixed:
        return False, (
            f"still fails with the fix in {len(still)} of {len(was_failing)} failing cases ({why}). If those "
            "cases test a different bug, put them in their own test file"
        )
    return False, f"still fails with the fix ({why})"


def _tool(error: str) -> str:
    """The "project/tool" an error from _run_checks is about."""
    return error.split(": ", 1)[0]


@dataclass
class FixCheck:
    ok: bool
    notes: list[str] = field(default_factory=list)
    checked_tests: bool = False  # False when there was nothing executable to check against


def _compare_checks(
    check: FixCheck,
    before_checks: tuple[list[Diagnostic], list[str]],
    after_checks: tuple[list[Diagnostic], list[str]],
    has_checks: bool,
    changes: list[FileChange],
    must_clear: list[Diagnostic],
) -> None:
    """Static checks with the fix vs without it, across the whole project: a fix that changes a
    signature breaks callers in files it never touched."""
    base_diags, base_errors = before_checks
    diags, errors = after_checks
    failed_tools = {_tool(e) for e in errors} - {_tool(e) for e in base_errors}
    if failed_tools:
        check.ok = False
        check.notes.append(
            "static checks could not run with the fix: "
            + "; ".join(e[:200] for e in errors if _tool(e) in failed_tools)
        )
    before = Counter(d.identity() for d in base_diags)
    remaining = before.copy()
    new = []
    for d in diags:
        if remaining[d.identity()] > 0:
            remaining[d.identity()] -= 1
        else:
            new.append(d)
    if new:
        changed = {c.file for c in changes}
        new.sort(key=lambda d: (d.file not in changed, d.file, d.line))
        check.ok = False
        check.notes.append(
            "fix introduces diagnostics: "
            + "; ".join(f"{d.file}:{d.line} {d.tool} {d.rule or ''} {d.message[:120]}" for d in new[:3])
        )
    elif has_checks and not failed_tools:
        check.notes.append("no new static-check diagnostics")
    now = Counter(d.identity() for d in diags)
    cleared = [d for d in must_clear if before[d.identity()]]
    still = [d for d in cleared if now[d.identity()] >= before[d.identity()]]
    if still:
        check.ok = False
        check.notes.append(
            "the diagnostic cited as evidence is still reported with the fix: "
            + "; ".join(f"{d.file}:{d.line} {d.tool} {d.rule or ''}" for d in still[:3])
        )
    elif cleared and not failed_tools:
        check.notes.append("the diagnostic cited as evidence is gone with the fix")


class ProjectRunner:
    def __init__(self, sandbox: Sandbox, settings: Settings, max_parallel: int = 2):
        self.sandbox = sandbox
        self.settings = settings
        self._installs: dict[tuple[str, str], asyncio.Task[ExecResult]] = {}
        self._sem = asyncio.Semaphore(max_parallel)
        # (root, project, sha256(test_code)) -> result, so verification can reuse runs we made ourselves.
        self.repro_results: dict[tuple[str, str, str], TestRun] = {}
        self.fix_results: dict[str, FixCheck] = {}
        self._locks: dict[str, _RWLock] = {}
        self._baseline_suite: dict[tuple[str, str], TestRun] = {}
        self._baseline_checks: dict[tuple[str, str], tuple[list[Diagnostic], list[str]]] = {}

    def _lock(self, root: Path) -> _RWLock:
        return self._locks.setdefault(str(root), _RWLock())

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
        if not adapter_for(p).can_run_tests(p):
            raise ValueError(f"project {p.name} has no supported test runner configured")
        await self.ensure_installed(root, p)
        async with self._lock(root).shared():
            return await self._run_tests(root, p, files)

    async def _run_tests(self, root: Path, p: ProjectConfig, files: list[str] | None = None) -> TestRun:
        adapter = adapter_for(p)
        report_rel = f"{REPORT_DIR}/tests-{uuid.uuid4().hex[:8]}.out"
        report_path = self._paths(root, p).host_project_dir / report_rel
        async with self._sem:
            res = await self._exec(
                root, p, adapter.test_cmd(p, files, report_rel), network=False, timeout=self.settings.test_timeout_s
            )
        report = report_path.read_text(errors="replace") if report_path.exists() else None
        report_path.unlink(missing_ok=True)
        return adapter.parse_test_report(report, res, self._paths(root, p))

    def _repro_path(self, p: ProjectConfig, test_code: str) -> tuple[str, str, str]:
        if len(test_code) > MAX_REPRO_CHARS:
            raise ValueError(f"test is too long ({len(test_code)} chars, max {MAX_REPRO_CHARS})")
        digest = hashlib.sha256(test_code.encode()).hexdigest()
        rel = adapter_for(p).repro_file(p, digest[:10])
        return digest, rel, (f"{p.norm_path}/{rel}" if p.norm_path else rel)

    async def run_repro(self, root: Path, p: ProjectConfig, test_code: str) -> tuple[TestRun, str]:
        """Write `test_code` into the project's repro dir, run only that file, remove it again.
        Returns the result and the repo-relative path the test was saved at."""
        digest, _rel, repo_rel = self._repro_path(p, test_code)
        cache_key = (str(root), p.name, digest)
        if cache_key not in self.repro_results:
            await self.ensure_installed(root, p)
            async with self._lock(root).shared():
                self.repro_results[cache_key] = await self._run_repro(root, p, test_code)
        return self.repro_results[cache_key], repo_rel

    async def _run_repro(self, root: Path, p: ProjectConfig, test_code: str) -> TestRun:
        _digest, rel, _repo_rel = self._repro_path(p, test_code)
        target = self._paths(root, p).host_project_dir / rel
        created_dir = not target.parent.exists()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(test_code)
        try:
            return await self._run_tests(root, p, files=[rel])
        finally:
            target.unlink(missing_ok=True)
            if created_dir and target.parent.exists() and not any(target.parent.iterdir()):
                target.parent.rmdir()

    # --- fixes -----------------------------------------------------------------------------------

    async def check_fix(
        self,
        root: Path,
        p: ProjectConfig,
        changes: list[FileChange],
        repro_tests: list[str],
        baseline_suite: TestRun | None = None,
        must_pass: list[str] | None = None,
        must_clear: list[Diagnostic] | None = None,
        shared_repros: frozenset[str] = frozenset(),
    ) -> FixCheck:
        """Apply `changes`, then require: every repro test passes (see _compare_repro; `shared_repros` are
        tests other findings cite too), the regressed tests in `must_pass` pass again, no existing test
        starts failing, no static check anywhere in the project reports something new, and the
        diagnostics in `must_clear` (the finding's static evidence) go away. Files are always restored."""
        h = hashlib.sha256(str(root).encode())
        for c in changes:
            h.update(c.file.encode() + b"\0" + c.patched.encode() + b"\0")
        for t in [*repro_tests, *(must_pass or [])]:
            h.update(t.encode() + (b"\1" if t in shared_repros else b"\0"))
        for d in must_clear or []:
            h.update(repr(d.identity()).encode() + b"\0")
        key = h.hexdigest()
        if key in self.fix_results:
            return self.fix_results[key]
        adapter = adapter_for(p)
        can_test = adapter.can_run_tests(p)
        await self.ensure_installed(root, p)
        base_key = (str(root), p.name)
        check = FixCheck(ok=True, checked_tests=can_test and bool(repro_tests or must_pass))
        async with self._lock(root).exclusive():
            # Baselines on the unpatched code (computed once per checkout and project).
            if can_test and base_key not in self._baseline_suite:
                self._baseline_suite[base_key] = baseline_suite or await self._run_tests(root, p)
            if base_key not in self._baseline_checks:
                self._baseline_checks[base_key] = await self._run_checks(root, p)
            # Each repro test's result without the fix, to compare case by case.
            before_runs = []
            for code in repro_tests:
                repro_key = (str(root), p.name, hashlib.sha256(code.encode()).hexdigest())
                if repro_key not in self.repro_results:
                    self.repro_results[repro_key] = await self._run_repro(root, p, code)
                before_runs.append(self.repro_results[repro_key])
            with applied(root, changes):
                for i, (code, before_run) in enumerate(zip(repro_tests, before_runs, strict=True), 1):
                    ok, note = await self._repro_with_fix(root, p, code, before_run, code in shared_repros)
                    check.ok &= ok
                    check.notes.append(f"repro test {i} {note}")
                if can_test:
                    suite = await self._run_tests(root, p)
                    before = {c.id for c in self._baseline_suite[base_key].failed}
                    broke = sorted(c.id for c in suite.failed if c.id not in before)
                    if suite.load_error or suite.timed_out:
                        check.ok = False
                        check.notes.append(f"test suite did not run with the fix: {suite.summary()}")
                    elif broke:
                        check.ok = False
                        check.notes.append("fix breaks existing tests: " + ", ".join(broke[:5]))
                    else:
                        check.notes.append(f"existing tests still pass ({len(suite.passed)} passed)")
                    status = {c.id: c.status for c in suite.cases}
                    still = [t for t in must_pass or [] if status.get(t) != "passed"]
                    if still:
                        check.ok = False
                        check.notes.append("regressed tests still fail with the fix: " + ", ".join(still[:5]))
                    elif must_pass:
                        check.notes.append("the regressed test(s) pass again with the fix")
                _compare_checks(
                    check,
                    self._baseline_checks[base_key],
                    await self._run_checks(root, p),
                    bool(adapter.checks(p)),
                    changes,
                    must_clear or [],
                )
        self.fix_results[key] = check
        return check

    async def _repro_with_fix(
        self, root: Path, p: ProjectConfig, code: str, before: TestRun, partial_ok: bool
    ) -> tuple[bool, str]:
        """Run a repro test REPRO_RUNS_WITH_FIX times with the fix applied; it must pass every time."""
        note = ""
        for n in range(1, REPRO_RUNS_WITH_FIX + 1):
            ok, note = _compare_repro(before, await self._run_repro(root, p, code), partial_ok)
            if not ok:
                if n == 1:
                    return False, note
                return False, (
                    f"passed with the fix, but on run {n} of {REPRO_RUNS_WITH_FIX} it {note.removeprefix('still ')}; "
                    "the test is flaky, so it can't confirm the fix"
                )
        return True, f"{note} ({REPRO_RUNS_WITH_FIX} runs in a row)"

    # --- static checks ---------------------------------------------------------------------------

    async def run_checks(self, root: Path, p: ProjectConfig) -> tuple[list[Diagnostic], list[str]]:
        """All configured static checks. Returns (diagnostics, human-readable errors)."""
        if any(spec.cmd for spec in adapter_for(p).checks(p)):
            await self.ensure_installed(root, p)
        async with self._lock(root).shared():
            return await self._run_checks(root, p)

    async def _run_checks(self, root: Path, p: ProjectConfig) -> tuple[list[Diagnostic], list[str]]:
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
