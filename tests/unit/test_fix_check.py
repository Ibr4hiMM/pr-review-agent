"""The fix check's rules, with the sandbox replaced by scripted test and static-check results."""

import pytest

from pr_review_agent.config import Settings
from pr_review_agent.fixes import plan_edits
from pr_review_agent.models import Diagnostic, FixEdit, TestCase, TestRun
from pr_review_agent.runner import REPRO_RUNS_WITH_FIX, ProjectRunner, _compare_repro

SRC = "export function pageCount(total: number, size: number) {\n  return Math.floor(total / size);\n}\n"


def run(**statuses):
    """run(a="passed", b="failed") -> a TestRun with those cases."""
    msgs = {"failed": "AssertionError: expected 2 to be 3", "broken": "fixture 'client' not found"}
    return TestRun(
        exit_code=int(any(s != "passed" for s in statuses.values())),
        cases=[
            TestCase(id=f"t::{name}", status="failed" if s == "broken" else s, message=msgs.get(s))
            for name, s in statuses.items()
        ],
    )


def test_every_genuinely_failing_case_must_pass():
    ok, note = _compare_repro(run(a="failed", b="failed"), run(a="passed", b="failed"))
    assert not ok and "1 of 2 failing cases" in note and "own test file" in note
    ok, note = _compare_repro(run(a="failed", b="failed"), run(a="passed", b="passed"))
    assert ok and note == "passes with the fix"


def test_cases_broken_by_the_test_itself_are_ignored():
    ok, note = _compare_repro(run(a="failed", b="broken"), run(a="passed", b="broken"))
    assert ok and "1 case(s) that only failed because the test is broken were ignored" in note
    ok, note = _compare_repro(run(b="broken"), run(b="passed"))
    assert not ok and "does not fail without the fix" in note


def test_partial_pass_only_for_a_test_other_findings_cite():
    ok, note = _compare_repro(run(a="failed", b="failed"), run(a="passed", b="failed"), partial_ok=True)
    assert ok and "for 1 of 2 failing cases" in note and "another finding" in note
    ok, _ = _compare_repro(run(a="failed", b="failed"), run(a="failed", b="failed"), partial_ok=True)
    assert not ok


def test_a_passing_case_that_stops_passing_fails_the_fix():
    ok, note = _compare_repro(run(a="failed", ok="passed"), run(a="passed", ok="failed"))
    assert not ok and "fails in new places" in note
    ok, _ = _compare_repro(run(a="failed", ok="passed"), run(a="passed", ok="skipped"))
    assert not ok


class ScriptedRunner(ProjectRunner):
    """Test and check results come from scripts instead of the sandbox. `repro_with_fix` is consumed
    one result per run; `checks_with_fix` is what the static checks report while the fix is applied."""

    def __init__(self, root, before, repro_with_fix, suite, checks=((), ()), checks_with_fix=None):
        super().__init__(sandbox=None, settings=Settings())
        self.root, self.before, self.repro_with_fix = root, before, list(repro_with_fix)
        self.suite, self.checks = suite, checks
        self.checks_with_fix = checks_with_fix if checks_with_fix is not None else checks
        self.repro_runs = 0

    def _patched(self):
        return "Math.floor" not in (self.root / "backend/src/page.ts").read_text()

    async def ensure_installed(self, root, p):
        return None

    async def _run_repro(self, root, p, code):
        if not self._patched():
            return self.before
        self.repro_runs += 1
        return self.repro_with_fix.pop(0)

    async def _run_tests(self, root, p, files=None):
        return self.suite

    async def _run_checks(self, root, p):
        diags, errors = self.checks_with_fix if self._patched() else self.checks
        return list(diags), list(errors)


@pytest.fixture
def root(tmp_path):
    (tmp_path / "backend/src").mkdir(parents=True)
    (tmp_path / "backend/src/page.ts").write_text(SRC)
    (tmp_path / "backend/src/caller.ts").write_text("import { pageCount } from './page';\n")
    return tmp_path


def changes(root, repo_cfg):
    return plan_edits(root, repo_cfg, [FixEdit(file="backend/src/page.ts", old="Math.floor", new="Math.ceil")])


SUITE = run(existing="passed")


async def check(runner, root, repo_cfg, **kw):
    result = await runner.check_fix(root, repo_cfg.project("backend"), changes(root, repo_cfg), ["T"], SUITE, **kw)
    assert (root / "backend/src/page.ts").read_text() == SRC  # always restored
    return result


async def test_repro_must_pass_several_runs_in_a_row(root, repo_cfg):
    runner = ScriptedRunner(root, run(a="failed"), [run(a="passed")] * REPRO_RUNS_WITH_FIX, SUITE)
    result = await check(runner, root, repo_cfg)
    assert result.ok and result.checked_tests and runner.repro_runs == REPRO_RUNS_WITH_FIX
    assert f"passes with the fix ({REPRO_RUNS_WITH_FIX} runs in a row)" in result.notes[0]

    flaky = ScriptedRunner(root, run(a="failed"), [run(a="passed"), run(a="failed"), run(a="passed")], SUITE)
    result = await check(flaky, root, repo_cfg)
    assert not result.ok and "on run 2 of" in result.notes[0] and "flaky" in result.notes[0]


TSC = dict(tool="tsc", rule="TS2345", message="Argument of type 'number | null' is not assignable")


async def test_new_diagnostics_anywhere_in_the_project_fail_the_fix(root, repo_cfg):
    # The fix changes page.ts, but the new type error is in a caller.
    broke_caller = ([Diagnostic(file="backend/src/caller.ts", line=1, **TSC)], [])
    runner = ScriptedRunner(root, run(a="failed"), [run(a="passed")] * 3, SUITE, checks_with_fix=broke_caller)
    result = await check(runner, root, repo_cfg)
    assert not result.ok and any("introduces diagnostics: backend/src/caller.ts:1" in n for n in result.notes)


async def test_existing_diagnostics_that_only_move_are_not_new(root, repo_cfg):
    moved = Diagnostic(
        file="backend/src/page.ts", line=9, tool="mypy", rule="no-redef", message='Name "x" already defined on line 3'
    )
    before = moved.model_copy(update={"line": 8, "message": 'Name "x" already defined on line 2'})
    runner = ScriptedRunner(root, run(a="failed"), [run(a="passed")] * 3, SUITE, ([before], []), ([moved], []))
    assert (await check(runner, root, repo_cfg)).ok


async def test_a_check_that_breaks_with_the_fix_fails_it(root, repo_cfg):
    runner = ScriptedRunner(
        root, run(a="failed"), [run(a="passed")] * 3, SUITE, checks_with_fix=([], ["backend/tsc: timed out"])
    )
    result = await check(runner, root, repo_cfg)
    assert not result.ok and any("could not run with the fix" in n for n in result.notes)


async def test_cited_diagnostic_must_go_away(root, repo_cfg):
    cited = Diagnostic(file="backend/src/page.ts", line=2, **TSC)
    kept = ScriptedRunner(root, run(a="failed"), [run(a="passed")] * 3, SUITE, ([cited], []))
    result = await check(kept, root, repo_cfg, must_clear=[cited])
    assert not result.ok and any("still reported with the fix" in n for n in result.notes)

    gone = ScriptedRunner(root, run(a="failed"), [run(a="passed")] * 3, SUITE, ([cited], []), ([], []))
    result = await check(gone, root, repo_cfg, must_clear=[cited])
    assert result.ok and any("cited as evidence is gone" in n for n in result.notes)
