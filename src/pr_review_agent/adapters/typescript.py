"""TypeScript / Node projects: vitest for tests, tsc and eslint for static checks."""

from __future__ import annotations

import json
import re

from ..config import ProjectConfig
from ..models import Diagnostic, TestCase, TestRun
from ..sandbox import ExecResult, q
from .base import CheckSpec, LanguageAdapter, PathMap

_TSC_LINE = re.compile(
    r"^(?P<file>[^\s(][^(]*)\((?P<line>\d+),(?P<col>\d+)\): (?P<sev>error|warning) (?P<code>TS\d+): (?P<msg>.*)$"
)
_STATUS = {"passed": "passed", "failed": "failed", "skipped": "skipped", "pending": "skipped", "todo": "skipped"}
# vitest.config.ts, eslint.config.js, vitest.workspace.ts, ... at the project root. (Deeper *.config.ts
# files are usually app code, e.g. Angular's app.config.ts.)
_ROOT_CONFIG = re.compile(r"\.(config|workspace)\.[cm]?[jt]sx?$")
# vitest.setup.ts, setupTests.ts, test-utils.tsx, ... anywhere.
_TEST_SETUP = re.compile(r"(\.setup|^setup-?tests?|^tests?-?setup|^test-?utils?)\.[cm]?[jt]sx?$", re.IGNORECASE)


class TypeScriptAdapter(LanguageAdapter):
    language = "typescript"
    default_image = "node:20-bookworm-slim"
    source_exts = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")

    def can_run_tests(self, p: ProjectConfig) -> bool:
        return bool(p.test) and "vitest" in p.test

    def test_cmd(self, p: ProjectConfig, files: list[str] | None, report: str) -> str:
        base = p.test or "npx vitest run"
        targets = " ".join(q(f) for f in files) if files else ""
        return f"{base} {targets} --reporter=json --outputFile={q(report)}".replace("  ", " ")

    def parse_test_report(self, report: str | None, res: ExecResult, paths: PathMap) -> TestRun:
        run = TestRun(exit_code=res.exit_code, timed_out=res.timed_out, output=res.tail(6000))
        if not report:
            if "No test files found" in res.output:
                run.load_error = "No test files found (is the file inside the vitest `include` globs?)"
            elif not res.timed_out:
                run.load_error = "vitest produced no report:\n" + res.tail(1500)
            return run
        data = json.loads(report)
        for suite in data.get("testResults", []):
            file = paths.to_project(suite.get("name", "?"))
            assertions = suite.get("assertionResults", [])
            if suite.get("status") == "failed" and not assertions:
                run.load_error = (run.load_error or "") + f"{file}: {suite.get('message', '').strip()[:1500]}\n"
                continue
            for a in assertions:
                run.cases.append(
                    TestCase(
                        id=f"{file}::{a.get('fullName') or a.get('title')}",
                        status=_STATUS.get(a.get("status", ""), "error"),
                        message="\n".join(a.get("failureMessages") or [])[:3000] or None,
                    )
                )
        return run

    def repro_file(self, p: ProjectConfig, uid: str) -> str:
        return f"{(p.repro_dir or 'test/__pr_review__').rstrip('/')}/repro_{uid}.test.ts"

    def repro_instructions(self, p: ProjectConfig) -> str:
        where = self.repro_file(p, "<id>")
        depth = where.count("/")
        return (
            f"Project `{p.name}` (path `{p.path}`) runs tests with vitest. Your test file is saved as "
            f"`{p.norm_path + '/' if p.norm_path else ''}{where}`, so import code under test with a relative path "
            f"starting `{'../' * depth}` (e.g. `{'../' * depth}src/...`). Use "
            "`import { describe, it, expect, vi } from 'vitest'`. There is NO network: mock external "
            "services (Supabase, HTTP, Firebase) with vi.mock, following the style of the existing tests. "
            "Write the smallest test asserting the CORRECT behaviour, so that it fails because of the bug."
        )

    def checks(self, p: ProjectConfig) -> list[CheckSpec]:
        specs = []
        if "tsc" in p.checks:
            specs.append(
                CheckSpec(
                    name="tsc",
                    cmd="npx --no-install tsc --noEmit -p tsconfig.json --pretty false",
                    parse=parse_tsc,
                )
            )
        if "eslint" in p.checks:
            specs.append(
                CheckSpec(
                    name="eslint",
                    cmd="npx --no-install eslint . -f json -o .pr-review/eslint.json",
                    report=".pr-review/eslint.json",
                    parse=parse_eslint,
                )
            )
        return specs

    def is_test_file(self, path: str) -> bool:
        return bool(re.search(r"(\.|/)(test|spec)\.[cm]?[jt]sx?$", path)) or "/__tests__/" in path

    def is_tooling_file(self, rel: str) -> bool:
        name = rel.rsplit("/", 1)[-1]
        return bool(_TEST_SETUP.search(name)) or ("/" not in rel and bool(_ROOT_CONFIG.search(name)))


def parse_tsc(output: str, paths: PathMap) -> list[Diagnostic]:
    diags = []
    for line in output.splitlines():
        m = _TSC_LINE.match(line.strip())
        if m:
            diags.append(
                Diagnostic(
                    tool="tsc",
                    rule=m["code"],
                    file=paths.to_repo(m["file"]),
                    line=int(m["line"]),
                    column=int(m["col"]),
                    message=m["msg"],
                    severity=m["sev"],
                )
            )
    return diags


def parse_eslint(report: str, paths: PathMap) -> list[Diagnostic]:
    diags = []
    for f in json.loads(report or "[]"):
        for m in f.get("messages", []):
            if not m.get("line"):
                continue
            diags.append(
                Diagnostic(
                    tool="eslint",
                    rule=m.get("ruleId"),
                    file=paths.to_repo(f["filePath"]),
                    line=m["line"],
                    column=m.get("column"),
                    message=m.get("message", ""),
                    severity="error" if m.get("severity") == 2 else "warning",
                )
            )
    return diags
