"""Python projects: pytest (JUnit XML) for tests, ruff on the host and optional mypy in the sandbox."""

from __future__ import annotations

import json
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from ..config import ProjectConfig
from ..models import Diagnostic, TestCase, TestRun
from ..sandbox import ExecResult, q
from .base import CheckSpec, LanguageAdapter, PathMap

VENV = ".pr-review-venv"
_MYPY_LINE = re.compile(
    r"^(?P<file>[^:]+):(?P<line>\d+):(?:(?P<col>\d+):)? (?P<sev>error|warning): (?P<msg>.*?)(?:  \[(?P<code>[\w-]+)\])?$"  # noqa: E501
)


class PythonAdapter(LanguageAdapter):
    language = "python"
    default_image = "python:3.12-slim"
    source_exts = (".py",)

    def wrap(self, cmd: str) -> str:
        return f". {VENV}/bin/activate && {cmd}"

    def install_cmd(self, p: ProjectConfig) -> str | None:
        extra = " mypy" if "mypy" in p.checks else ""
        steps = [
            f"python3 -m venv {VENV}",
            f". {VENV}/bin/activate",
            "python -m pip install -q --upgrade pip",
        ]
        if p.install:
            steps.append(p.install)
        steps.append(f"python -m pip install -q pytest{extra}")
        return " && ".join(steps)

    def test_cmd(self, p: ProjectConfig, files: list[str] | None, report: str) -> str:
        tail = f"-p no:cacheprovider --junitxml={q(report)}"
        if files:
            return f"python -m pytest {' '.join(q(f) for f in files)} -q {tail}"
        return f"{p.test or 'python -m pytest -q'} {tail}"

    def parse_test_report(self, report: str | None, res: ExecResult, paths: PathMap) -> TestRun:
        run = TestRun(exit_code=res.exit_code, timed_out=res.timed_out, output=res.tail(6000))
        if not report:
            if not res.timed_out:
                run.load_error = "pytest produced no report:\n" + res.tail(1500)
            return run
        root = ET.fromstring(report)
        for tc in root.iter("testcase"):
            cls, name = tc.get("classname", ""), tc.get("name", "")
            case_id = f"{cls}::{name}" if cls else name
            failure, error, skipped = tc.find("failure"), tc.find("error"), tc.find("skipped")
            if error is not None and "collection failure" in (error.get("message") or ""):
                run.load_error = (run.load_error or "") + f"{name}: {(error.text or '')[-1500:]}\n"
                continue
            if failure is not None:
                status, node = "failed", failure
            elif error is not None:
                status, node = "error", error
            elif skipped is not None:
                status, node = "skipped", None
            else:
                status, node = "passed", None
            message = None
            if node is not None:
                message = ((node.get("message") or "") + "\n" + (node.text or ""))[-3000:]
            run.cases.append(TestCase(id=case_id, status=status, message=message))
        return run

    def repro_file(self, p: ProjectConfig, uid: str) -> str:
        return f"{(p.repro_dir or 'tests').rstrip('/')}/test_pr_review_{uid}.py".removeprefix("./")

    def repro_instructions(self, p: ProjectConfig) -> str:
        where = self.repro_file(p, "<id>")
        return (
            f"Project `{p.name}` (path `{p.path}`) runs tests with pytest from the project directory. Your "
            f"test is saved as `{p.norm_path + '/' if p.norm_path else ''}{where}`; import code under test the "
            "same way the existing tests in that directory do (existing conftest.py fixtures apply). There is "
            "NO network: monkeypatch/mock external clients. Write the smallest test asserting the CORRECT "
            "behaviour, so that it fails because of the bug."
        )

    def checks(self, p: ProjectConfig) -> list[CheckSpec]:
        specs = []
        if "ruff" in p.checks:
            # ruff never executes project code, so it runs on the host with our own binary.
            ruff = shutil.which("ruff", path=str(Path(sys.executable).parent)) or shutil.which("ruff") or "ruff"
            specs.append(
                CheckSpec(
                    name="ruff",
                    host_argv=[
                        ruff,
                        "check",
                        ".",
                        "--output-format",
                        "json",
                        "--no-cache",
                        "--exit-zero",
                        "--extend-select",
                        "B",
                    ],
                    parse=parse_ruff,
                )
            )
        if "mypy" in p.checks:
            specs.append(
                CheckSpec(
                    name="mypy",
                    cmd="python -m mypy . --no-error-summary --show-column-numbers --no-pretty",
                    parse=parse_mypy,
                )
            )
        return specs

    def is_test_file(self, path: str) -> bool:
        name = path.rsplit("/", 1)[-1]
        return name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"


def parse_ruff(output: str, paths: PathMap) -> list[Diagnostic]:
    start = output.find("[")
    if start < 0:
        return []
    return [
        Diagnostic(
            tool="ruff",
            rule=d.get("code"),
            file=paths.to_repo(d["filename"]),
            line=d["location"]["row"],
            column=d["location"]["column"],
            message=d.get("message", ""),
        )
        for d in json.loads(output[start:])
    ]


def parse_mypy(output: str, paths: PathMap) -> list[Diagnostic]:
    diags = []
    for line in output.splitlines():
        m = _MYPY_LINE.match(line.strip())
        if m:
            diags.append(
                Diagnostic(
                    tool="mypy",
                    rule=m["code"],
                    file=paths.to_repo(m["file"]),
                    line=int(m["line"]),
                    column=int(m["col"]) if m["col"] else None,
                    message=m["msg"],
                    severity=m["sev"],
                )
            )
    return diags
