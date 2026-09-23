from pr_review_agent.analyzers.static import is_bug_relevant, new_on_changed_lines
from pr_review_agent.models import Diagnostic


def d(line, msg="Object is possibly 'undefined'.", file="backend/src/a.ts", rule="TS2532", tool="tsc", sev="error"):
    return Diagnostic(tool=tool, rule=rule, file=file, line=line, message=msg, severity=sev)


def test_existing_diagnostics_are_not_new_even_when_lines_shift():
    base = [d(10)]
    head = [d(14)]  # same diagnostic, moved down by an edit above it
    assert new_on_changed_lines(base, head, {"backend/src/a.ts": {14}}) == []


def test_new_diagnostic_on_changed_line_is_reported():
    base = [d(10)]
    head = [d(12), d(30, msg="Type 'string' is not assignable to type 'number'.", rule="TS2322")]
    new = new_on_changed_lines(base, head, {"backend/src/a.ts": {30}})
    assert [x.line for x in new] == [30]


def test_duplicate_identity_prefers_changed_line_as_new():
    base = [d(10)]
    head = [d(10), d(20)]  # one old copy, one new copy of an identical message
    new = new_on_changed_lines(base, head, {"backend/src/a.ts": {20}})
    assert [x.line for x in new] == [20]


def test_new_diagnostic_off_the_diff_is_ignored():
    assert new_on_changed_lines([], [d(99)], {"backend/src/a.ts": {1, 2}}) == []


def test_bug_relevance_filter():
    assert is_bug_relevant(d(1))
    assert is_bug_relevant(d(1, tool="ruff", rule="F821"))
    assert is_bug_relevant(d(1, tool="ruff", rule="B006"))
    assert not is_bug_relevant(d(1, tool="ruff", rule="I001"))
    assert is_bug_relevant(d(1, tool="eslint", rule="@typescript-eslint/no-floating-promises"))
    assert not is_bug_relevant(d(1, tool="eslint", rule="no-undef", sev="warning"))
    assert not is_bug_relevant(d(1, tool="eslint", rule="prefer-const"))
