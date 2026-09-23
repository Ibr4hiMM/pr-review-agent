import json
from pathlib import Path

from pr_review_agent.adapters.base import PathMap, broken_test_reason
from pr_review_agent.adapters.python import PythonAdapter, parse_mypy, parse_ruff
from pr_review_agent.adapters.typescript import TypeScriptAdapter, parse_eslint, parse_tsc
from pr_review_agent.config import ProjectConfig
from pr_review_agent.models import TestCase, TestRun
from pr_review_agent.sandbox import ExecResult

BACKEND = ProjectConfig(
    name="backend",
    path="backend",
    language="typescript",
    test="npx vitest run",
    repro_dir="test/__pr_review__",
    checks=["tsc", "eslint"],
)
AI = ProjectConfig(name="ai", path="ai_service", language="python", repro_dir="tests", checks=["ruff"])
HOST = Path("/home/me/ws/head")


def paths(p=BACKEND):
    return PathMap(project=p, host_project_dir=HOST / p.path)


def test_pathmap_handles_host_container_and_relative_paths():
    pm = paths()
    assert pm.to_repo("/home/me/ws/head/backend/src/a.ts") == "backend/src/a.ts"
    assert pm.to_repo("/work/backend/src/a.ts") == "backend/src/a.ts"
    assert pm.to_repo("src/a.ts") == "backend/src/a.ts"
    assert pm.to_project("/work/backend/test/x.test.ts") == "test/x.test.ts"


def vitest_report(*suites):
    return json.dumps({"testResults": list(suites)})


def test_vitest_report_assertion_failure():
    report = vitest_report(
        {
            "name": "/work/backend/test/__pr_review__/repro_1.test.ts",
            "status": "failed",
            "message": "",
            "assertionResults": [
                {
                    "fullName": "pagination returns last page",
                    "status": "failed",
                    "failureMessages": ["AssertionError: expected 9 to be 10"],
                },
                {"fullName": "pagination first page", "status": "passed", "failureMessages": []},
            ],
        }
    )
    run = TypeScriptAdapter().parse_test_report(report, ExecResult(1, ""), paths())
    assert run.load_error is None
    assert [c.id for c in run.failed] == ["test/__pr_review__/repro_1.test.ts::pagination returns last page"]
    assert broken_test_reason(run) is None


def test_vitest_suite_level_error_is_a_load_error():
    report = vitest_report(
        {
            "name": "/work/backend/test/x.test.ts",
            "status": "failed",
            "message": "Failed to load url ../../src/nope",
            "assertionResults": [],
        }
    )
    run = TypeScriptAdapter().parse_test_report(report, ExecResult(1, ""), paths())
    assert "Failed to load url" in run.load_error
    assert broken_test_reason(run).startswith("test file failed to load")


def test_vitest_without_report_or_matching_files():
    run = TypeScriptAdapter().parse_test_report(None, ExecResult(1, "No test files found, exiting"), paths())
    assert "No test files found" in run.load_error


def test_test_commands_and_repro_paths():
    ts = TypeScriptAdapter()
    assert ts.test_cmd(BACKEND, ["test/a.test.ts"], ".pr-review/r.json") == (
        "npx vitest run test/a.test.ts --reporter=json --outputFile=.pr-review/r.json"
    )
    assert ts.repro_file(BACKEND, "abc") == "test/__pr_review__/repro_abc.test.ts"
    assert "../../src" in ts.repro_instructions(BACKEND)
    py = PythonAdapter()
    assert py.repro_file(AI, "abc") == "tests/test_pr_review_abc.py"
    assert py.wrap("pytest").startswith(". .pr-review-venv/bin/activate && ")
    assert "requirements" not in py.install_cmd(AI)  # no install configured -> just venv + pytest
    assert not ts.can_run_tests(BACKEND.model_copy(update={"test": "npx jest"}))


JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest" errors="1" failures="1" tests="3">
  <testcase classname="tests.test_pr_review_x" name="test_rounding" time="0.01">
    <failure message="AssertionError: assert 0.30000000000000004 == 0.3">tests/x.py:5: AssertionError</failure>
  </testcase>
  <testcase classname="tests.test_db" name="test_ok" time="0.01"/>
  <testcase classname="" name="tests.test_broken" time="0">
    <error message="collection failure">ImportError: cannot import name 'nope'</error>
  </testcase>
</testsuite></testsuites>"""


def test_junit_report_parsing():
    run = PythonAdapter().parse_test_report(JUNIT, ExecResult(1, ""), paths(AI))
    assert [c.id for c in run.failed] == ["tests.test_pr_review_x::test_rounding"]
    assert [c.id for c in run.passed] == ["tests.test_db::test_ok"]
    assert "ImportError" in run.load_error


def test_broken_test_markers():
    run = TestRun(exit_code=1, cases=[TestCase(id="t", status="failed", message="ReferenceError: foo is not defined")])
    assert broken_test_reason(run) is not None
    run.cases.append(TestCase(id="u", status="failed", message="AssertionError: expected 1 to be 2"))
    assert broken_test_reason(run) is None
    assert broken_test_reason(TestRun(exit_code=0, timed_out=True)) == "timed out"
    assert broken_test_reason(TestRun(exit_code=0)) == "no tests were collected"


def test_tsc_eslint_ruff_mypy_parsers():
    tsc = parse_tsc("src/routes.ts(12,5): error TS2345: Argument of type 'string' is not assignable.\nnoise", paths())
    assert tsc[0].model_dump(include={"file", "line", "rule"}) == {
        "file": "backend/src/routes.ts",
        "line": 12,
        "rule": "TS2345",
    }
    eslint = parse_eslint(
        json.dumps(
            [
                {
                    "filePath": "/work/backend/src/a.ts",
                    "messages": [
                        {"ruleId": "no-undef", "line": 3, "column": 1, "message": "x is not defined", "severity": 2},
                        {"ruleId": None, "line": None, "message": "parse error"},
                    ],
                }
            ]
        ),
        paths(),
    )
    assert [(d.file, d.line, d.rule, d.severity) for d in eslint] == [("backend/src/a.ts", 3, "no-undef", "error")]
    ruff = parse_ruff(
        "warning: x\n"
        + json.dumps(
            [
                {
                    "code": "B006",
                    "message": "mutable default",
                    "filename": "/home/me/ws/head/ai_service/ai.py",
                    "location": {"row": 7, "column": 3},
                }
            ]
        ),
        paths(AI),
    )
    assert (ruff[0].file, ruff[0].line, ruff[0].rule) == ("ai_service/ai.py", 7, "B006")
    mypy = parse_mypy("ai.py:4:5: error: Incompatible return value  [return-value]", paths(AI))
    assert (mypy[0].file, mypy[0].rule, mypy[0].column) == ("ai_service/ai.py", "return-value", 5)
