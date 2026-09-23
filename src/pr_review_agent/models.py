"""Data shapes shared by every stage of the pipeline.

`ReviewResult` is what the agent must return (its JSON schema is passed to the Agent SDK as
`output_format`). Everything else is produced by our own deterministic code.
"""

from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["critical", "high", "medium", "low"]
Category = Literal["logic", "error-handling", "security", "async", "api-contract", "resource-leak", "regression"]
EvidenceKind = Literal["failing_test", "test_regression", "static", "code_reference"]
Tier = Literal["verified", "possible"]

SEVERITY_ORDER: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3}


# --- Agent output -------------------------------------------------------------------------------
# Evidence is deliberately one flat object with a `kind` discriminator rather than a union: the
# SDK validates against JSON Schema draft-07, and flat schemas cause far fewer validation retries.


class Evidence(BaseModel):
    kind: EvidenceKind = Field(
        description=(
            "failing_test: a test you ran with run_repro_test that demonstrates the bug. "
            "test_regression: an existing test that passes on base and fails on head. "
            "static: a diagnostic returned by static_findings. "
            "code_reference: a quote of related code that proves the bug (e.g. a caller passing null)."
        )
    )
    test_code: str | None = Field(None, description="failing_test: the exact test file content you ran.")
    test_output: str | None = Field(None, description="failing_test: the relevant failure output.")
    test_id: str | None = Field(None, description="test_regression: the test id as reported by the tools.")
    tool: str | None = Field(None, description="static: tool name, e.g. tsc, eslint, ruff.")
    rule: str | None = Field(None, description="static: rule/code, e.g. TS2345, no-floating-promises.")
    file: str | None = Field(None, description="static/code_reference: repo-relative path.")
    line: int | None = Field(None, description="static/code_reference: 1-based line number in head.")
    snippet: str | None = Field(None, description="code_reference: code copied verbatim from `file` at `line`.")
    why_relevant: str | None = Field(None, description="code_reference: why this code proves the bug.")


class Finding(BaseModel):
    title: str = Field(description="One-line statement of the defect.")
    severity: Severity
    category: Category
    project: str = Field(description="Project name from the configuration this file belongs to.")
    file: str = Field(description="Repo-relative path of the buggy code.")
    line_start: int = Field(description="1-based first line of the buggy code in head.")
    line_end: int = Field(description="1-based last line of the buggy code in head.")
    explanation: str = Field(description="What goes wrong, for which input, and why. Be concrete.")
    confidence: float = Field(ge=0, le=1, description="Your confidence that this is a real bug.")
    suggested_fix: str | None = Field(None, description="Short description or snippet of the fix.")
    evidence: list[Evidence]


class ReviewResult(BaseModel):
    summary: str = Field(description="2-4 sentence summary of what the code does and overall risk.")
    findings: list[Finding]


# --- Deterministic results ----------------------------------------------------------------------


class Diagnostic(BaseModel):
    """One static-analysis message, normalized across tools."""

    tool: str
    rule: str | None = None
    file: str  # repo-relative
    line: int
    column: int | None = None
    message: str
    severity: str = "error"

    def identity(self) -> tuple[str, str | None, str, str]:
        """Line-independent identity, used to tell new diagnostics from ones that already existed
        on base (line numbers shift between base and head, so they can't be part of the key)."""
        return (self.tool, self.rule, self.file, re.sub(r"\s+", " ", self.message).strip())


TestStatus = Literal["passed", "failed", "skipped", "error"]


class TestCase(BaseModel):
    __test__ = False  # not a pytest class

    id: str
    status: TestStatus
    message: str | None = None


class TestRun(BaseModel):
    """Parsed result of running a test command."""

    __test__ = False

    exit_code: int
    timed_out: bool = False
    cases: list[TestCase] = Field(default_factory=list)
    # The file could not be loaded (syntax/import/compile error), so no test actually ran.
    load_error: str | None = None
    output: str = ""

    @property
    def failed(self) -> list[TestCase]:
        return [c for c in self.cases if c.status in ("failed", "error")]

    @property
    def passed(self) -> list[TestCase]:
        return [c for c in self.cases if c.status == "passed"]

    def summary(self) -> str:
        if self.timed_out:
            return "timed out"
        if self.load_error:
            return f"could not load test file: {self.load_error[:300]}"
        return f"{len(self.passed)} passed, {len(self.failed)} failed (exit {self.exit_code})"


class VerifiedFinding(BaseModel):
    finding: Finding
    tier: Tier
    fingerprint: str
    notes: list[str] = Field(default_factory=list)
    pre_existing: bool = False
    evidence: list[Evidence] = Field(default_factory=list)  # only evidence that passed verification


_STOPWORDS = {
    "a",
    "an",
    "the",
    "of",
    "in",
    "on",
    "to",
    "is",
    "are",
    "be",
    "when",
    "for",
    "and",
    "or",
    "with",
    "by",
    "from",
    "can",
    "may",
    "its",
    "it",
    "this",
    "that",
    "not",
    "no",
}


def fingerprint(file: str, category: str, title: str) -> str:
    """Stable id for de-duplicating findings across runs (titles are normalized because the model
    words them slightly differently each time)."""
    words = set(re.sub(r"[^a-z0-9 ]+", " ", title.lower()).split()) - _STOPWORDS
    key = f"{file}|{category}|{' '.join(sorted(words))}"
    return hashlib.sha1(key.encode()).hexdigest()[:12]


def review_result_schema() -> dict:
    return ReviewResult.model_json_schema()
