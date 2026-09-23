"""Custom tools the agent can call (exposed as `mcp__review__<name>`).

The agent never gets a shell: anything that executes code goes through these tools, which run it in
the sandbox, and every result they return was produced by our code (so verification can trust it).
"""

from __future__ import annotations

import re
from typing import Any

from claude_agent_sdk import McpSdkServerConfig, ToolAnnotations, create_sdk_mcp_server, tool

from ..adapters import adapter_for
from ..adapters.base import broken_test_reason
from ..files import iter_source_files
from ..fixes import EditError, plan_edits, unified_patch
from ..models import FixEdit, TestRun
from .context import ReviewContext

SERVER_NAME = "review"
TOOL_NAMES = ["run_repro_test", "check_fix", "run_existing_tests", "static_findings", "find_references"]
MAX_OUTPUT = 3500


def _text(text: str, error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "is_error": error}


def failure_excerpt(ctx: ReviewContext, run: TestRun, cases: int = 3) -> str:
    msgs = [f"{c.id}\n{ctx.clean(c.message or '')}" for c in run.failed[:cases]]
    body = "\n\n".join(msgs) if msgs else ctx.clean(run.output[-MAX_OUTPUT:], max_lines=40)
    return body[:MAX_OUTPUT]


def repro_verdict(head: TestRun, base: TestRun | None) -> tuple[bool, str]:
    """Is this repro valid evidence? Shared by the tool (feedback to the agent) and the verifier."""
    broken = broken_test_reason(head)
    if broken:
        return False, f"NOT EVIDENCE: {broken}. Fix the test and run it again."
    if not head.failed:
        return False, "NOT EVIDENCE: the test passes on head, so it does not demonstrate a bug."
    if base is not None:
        if broken_test_reason(base) is None and not base.failed:
            return True, "VALID EVIDENCE: fails on head and passes on base, so this PR introduced the bug."
        return True, "VALID EVIDENCE (pre-existing): fails on head but also on base, so the bug predates this PR."
    return True, "VALID EVIDENCE: the test fails on the current code."


def build_server(ctx: ReviewContext) -> McpSdkServerConfig:
    ro = ToolAnnotations(readOnlyHint=True)

    @tool(
        "run_repro_test",
        "Run a test file you wrote against the code, in an offline sandbox. Use this to PROVE a suspected "
        "bug: write a minimal test that asserts the correct behaviour, so it fails because of the bug. In "
        "PR reviews it runs on both head and base. Returns pass/fail per run, the failure output and "
        "whether the result is valid evidence. The file is deleted afterwards.",
        {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Project name from the configuration."},
                "test_code": {"type": "string", "description": "Complete test file content."},
            },
            "required": ["project", "test_code"],
        },
    )
    async def run_repro_test(args: dict[str, Any]) -> dict[str, Any]:
        try:
            p = ctx.project(args["project"])
            if not adapter_for(p).can_run_tests(p):
                return _text(f"Project {p.name} has no test runner; use code_reference evidence instead.", True)
            ctx.progress(f"running repro test in {p.name}")
            ctx.emit({"type": "test"})
            head, path = await ctx.runner.run_repro(ctx.ws.head, p, args["test_code"])
            base = None
            if ctx.mode == "review" and ctx.ws.base is not None and broken_test_reason(head) is None and head.failed:
                base, _ = await ctx.runner.run_repro(ctx.ws.base, p, args["test_code"])
        except Exception as e:
            return _text(f"Could not run the test: {e}", True)
        ok, verdict = repro_verdict(head, base)
        lines = [f"Saved as {path}", f"HEAD: {head.summary()}"]
        if base is not None:
            lines.append(f"BASE: {base.summary()}")
        lines.append(verdict)
        if head.failed or head.load_error:
            lines.append(
                "Failure output (head):\n"
                + (failure_excerpt(ctx, head) if head.failed else ctx.clean(head.load_error or "", max_lines=30))
            )
        if ok:
            lines.append("Include this exact test_code as `failing_test` evidence.")
        return _text("\n".join(lines))

    @tool(
        "check_fix",
        "Try a fix for a bug you proved. Applies exact search/replace edits to the head checkout, runs your "
        "failing test (it must now PASS), the project's existing tests (none may start failing) and its static "
        "checks (no new errors), then restores the files. Put edits that pass into the finding's fix_edits.",
        {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "edits": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string", "description": "Repo-relative source file."},
                            "old": {"type": "string", "description": "Exact current text; must occur once."},
                            "new": {"type": "string", "description": "Replacement text."},
                        },
                        "required": ["file", "old", "new"],
                    },
                },
                "test_code": {"type": "string", "description": "The failing repro test this fix should make pass."},
            },
            "required": ["project", "edits"],
        },
    )
    async def check_fix(args: dict[str, Any]) -> dict[str, Any]:
        try:
            p = ctx.project(args["project"])
            changes = plan_edits(ctx.ws.head, ctx.cfg, [FixEdit.model_validate(e) for e in args["edits"]])
            if not changes:
                return _text("The edits change nothing.", True)
            outside = [c.file for c in changes if ctx.cfg.project_for(c.file) is not p]
            if outside:
                return _text(f"A fix must stay inside project {p.name}; these files are not: {outside}", True)
            ctx.progress(f"checking fix in {p.name} ({', '.join(c.file for c in changes)})")
            ctx.emit({"type": "fix_check"})
            suite = ctx.suites.get(p.name)
            tests = [args["test_code"]] if args.get("test_code") else []
            result = await ctx.runner.check_fix(ctx.ws.head, p, changes, tests, suite.head if suite else None)
        except EditError as e:
            return _text(f"Could not apply the edits: {e}", True)
        except Exception as e:
            return _text(f"Could not check the fix: {e}", True)
        verdict = "FIX PASSES" if result.ok else "FIX FAILS"
        if result.ok and not result.checked_tests:
            verdict += " (applies cleanly, but no test was run against it)"
        patch = unified_patch(changes)
        return _text("\n".join([verdict, *[f"- {n}" for n in result.notes], "", "Patch:", patch[:2500]]))

    @tool(
        "run_existing_tests",
        "Run existing test files of a project (paths relative to the project directory) in the sandbox, on "
        "head (and base in PR reviews). Useful to check whether current tests cover a behaviour.",
        {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
                "files": {"type": "array", "items": {"type": "string"}, "maxItems": 10},
            },
            "required": ["project", "files"],
        },
        annotations=ro,
    )
    async def run_existing_tests(args: dict[str, Any]) -> dict[str, Any]:
        try:
            p = ctx.project(args["project"])
            files = [f.lstrip("/") for f in args["files"] if ".." not in f]
            ctx.progress(f"running {len(files)} existing test file(s) in {p.name}")
            head = await ctx.runner.run_tests(ctx.ws.head, p, files=files)
            out = [f"HEAD: {head.summary()}"]
            if ctx.mode == "review" and ctx.ws.base is not None:
                base = await ctx.runner.run_tests(ctx.ws.base, p, files=files)
                out.append(f"BASE: {base.summary()}")
            if head.failed:
                out.append(failure_excerpt(ctx, head))
            return _text("\n".join(out))
        except Exception as e:
            return _text(f"Could not run tests: {e}", True)

    @tool(
        "static_findings",
        "Static-analysis diagnostics (tsc, eslint, ruff, …) computed before the review. In PR reviews these "
        "are only diagnostics that are NEW in this PR on changed lines. Filter by project and/or file.",
        {
            "type": "object",
            "properties": {"project": {"type": "string"}, "file": {"type": "string"}},
            "required": [],
        },
        annotations=ro,
    )
    async def static_findings(args: dict[str, Any]) -> dict[str, Any]:
        diags = ctx.diagnostics
        if args.get("project"):
            try:
                p = ctx.project(args["project"])
            except ValueError as e:
                return _text(str(e), True)
            diags = [d for d in diags if p.contains(d.file)]
        if args.get("file"):
            diags = [d for d in diags if d.file == args["file"].lstrip("./")]
        if not diags:
            return _text("No diagnostics.")
        lines = [f"{d.file}:{d.line} [{d.tool} {d.rule or ''}] {d.message}" for d in diags[:80]]
        if len(diags) > 80:
            lines.append(f"…and {len(diags) - 80} more")
        return _text("\n".join(lines))

    @tool(
        "find_references",
        "Find usages of an identifier (function, class, variable, route string) across the project's source "
        "files in head, as file:line: text. Use it to inspect callers of changed code.",
        {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "project": {"type": "string", "description": "Optional; defaults to all enabled projects."},
            },
            "required": ["symbol"],
        },
        annotations=ro,
    )
    async def find_references(args: dict[str, Any]) -> dict[str, Any]:
        symbol = args["symbol"].strip()
        if not symbol or len(symbol) > 200:
            return _text("Give a non-empty symbol (max 200 chars).", True)
        try:
            projects = [ctx.project(args["project"])] if args.get("project") else ctx.cfg.enabled_projects()
        except ValueError as e:
            return _text(str(e), True)
        pattern = re.compile(rf"(?<![\w$]){re.escape(symbol)}(?![\w$])")
        hits: list[str] = []
        for p in projects:
            for rel in iter_source_files(ctx.ws.head, ctx.cfg, p):
                try:
                    lines = (ctx.ws.head / rel).read_text(errors="replace").splitlines()
                except OSError:
                    continue
                hits += [f"{rel}:{i}: {line.strip()[:200]}" for i, line in enumerate(lines, 1) if pattern.search(line)]
                if len(hits) > 150:
                    break
        if not hits:
            return _text(f"No references to {symbol!r} found.")
        more = f"\n…{len(hits) - 100} more (narrow with `project`)" if len(hits) > 100 else ""
        return _text("\n".join(hits[:100]) + more)

    return create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[run_repro_test, check_fix, run_existing_tests, static_findings, find_references],
    )


def allowed_tool_names() -> list[str]:
    return [f"mcp__{SERVER_NAME}__{n}" for n in TOOL_NAMES]
