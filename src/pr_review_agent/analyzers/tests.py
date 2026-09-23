"""Existing-test evidence: tests that pass on base but fail on head."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import ProjectConfig
from ..models import TestRun
from ..runner import ProjectRunner
from ..workspace import Workspace


@dataclass
class SuiteResult:
    project: str
    head: TestRun | None = None
    base: TestRun | None = None
    regressed: list[str] = field(default_factory=list)  # passed on base, fail on head
    new_failing: list[str] = field(default_factory=list)  # test doesn't exist on base and fails
    error: str | None = None

    def describe(self) -> str:
        if self.error:
            return f"{self.project}: could not run tests ({self.error[:300]})"
        if not self.head:
            return f"{self.project}: tests not run"
        parts = [f"{self.project}: head {self.head.summary()}"]
        if self.base:
            parts.append(f"base {self.base.summary()}")
        if self.regressed:
            parts.append("REGRESSED: " + ", ".join(self.regressed[:20]))
        if self.new_failing:
            parts.append("new failing tests: " + ", ".join(self.new_failing[:20]))
        return "; ".join(parts)


async def compare_suites(runner: ProjectRunner, ws: Workspace, p: ProjectConfig) -> SuiteResult:
    """Run the suite on head; only if something fails, run it on base to classify the failures."""
    out = SuiteResult(project=p.name)
    try:
        out.head = await runner.run_tests(ws.head, p)
        failed = {c.id for c in out.head.failed}
        if failed and ws.base is not None:
            out.base = await runner.run_tests(ws.base, p)
            base_status = {c.id: c.status for c in out.base.cases}
            out.regressed = sorted(t for t in failed if base_status.get(t) == "passed")
            out.new_failing = sorted(t for t in failed if t not in base_status)
    except Exception as e:  # install failures etc. are reported, not fatal
        out.error = str(e)
    return out
