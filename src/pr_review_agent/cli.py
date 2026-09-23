"""`pr-review` command line: init | doctor | scan | review."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from importlib import resources
from pathlib import Path
from typing import Annotated

import typer

from .config import (
    CONFIG_FILE,
    RepoConfig,
    Settings,
    detect_projects,
    github_token,
    load_repo_config,
    render_toml,
)
from .sandbox import Sandbox, pick_sandbox

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Evidence-backed PR reviewer and codebase scanner (Claude Agent SDK).",
)

SandboxOpt = Annotated[str, typer.Option("--sandbox", help="auto | docker | local")]
ModelOpt = Annotated[str | None, typer.Option("--model", help="Claude model (default claude-opus-5)")]
VerboseOpt = Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging")]

_t0 = time.monotonic()


def _progress(msg: str) -> None:
    typer.secho(f"[{time.monotonic() - _t0:6.1f}s] {msg}", err=True, fg="cyan")


def _setup(verbose: bool, model: str | None) -> Settings:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s"
    )
    settings = Settings()
    if model:
        settings.model = model
    return settings


async def _pick_sandbox(kind: str, settings: Settings) -> Sandbox:
    try:
        sandbox, note = await pick_sandbox(kind, settings.cache_dir)
    except RuntimeError as e:
        raise typer.BadParameter(str(e)) from e
    if note:
        typer.secho(note, err=True, fg="yellow")
    return sandbox


def _require_anthropic_credentials() -> None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        typer.secho(
            "ANTHROPIC_API_KEY is not set; the agent will fall back to any Claude Code login on this "
            "machine, if there is one.",
            err=True,
            fg="yellow",
        )


@app.command()
def init(
    path: Annotated[Path, typer.Argument(help="Repository to configure")] = Path("."),
    force: Annotated[bool, typer.Option(help="Overwrite existing files")] = False,
    workflow: Annotated[bool, typer.Option(help="Also write .github/workflows/pr-review.yml")] = True,
    tool_source: Annotated[str | None, typer.Option(help="uvx --from source used by the workflow")] = None,
) -> None:
    """Detect the repo's projects and write .pr-review.toml (+ a GitHub Actions workflow)."""
    from .workspace import repo_slug

    path = path.resolve()
    cfg_file = path / CONFIG_FILE
    if cfg_file.exists() and not force:
        typer.secho(f"{cfg_file} exists (use --force to overwrite).", fg="yellow")
    else:
        cfg = RepoConfig(projects=detect_projects(path))
        cfg_file.write_text(render_toml(cfg))
        typer.secho(f"wrote {cfg_file}", fg="green")
        for p in cfg.projects:
            state = "enabled" if p.enabled else "disabled"
            typer.echo(f"  - {p.name:<16} {p.language:<11} {p.path:<16} test={p.test or '-'}  ({state})")
    if workflow:
        wf = path / ".github" / "workflows" / "pr-review.yml"
        if wf.exists() and not force:
            typer.secho(f"{wf} exists (use --force to overwrite).", fg="yellow")
        else:
            slug = repo_slug(path)
            owner = slug.split("/")[0] if slug else "YOUR-GITHUB-USER"
            source = tool_source or f"git+https://github.com/{owner}/pr-review-agent"
            text = resources.files("pr_review_agent").joinpath("templates/pr-review.yml").read_text()
            wf.parent.mkdir(parents=True, exist_ok=True)
            wf.write_text(text.replace("__TOOL_SOURCE__", source))
            typer.secho(f"wrote {wf} (installs the tool from {source})", fg="green")
    typer.echo("\nNext: review the files, run `pr-review doctor`, then commit them.")


@app.command()
def doctor(
    path: Annotated[Path, typer.Argument(help="Repository to check")] = Path("."),
    sandbox: SandboxOpt = "auto",
    uncommitted: Annotated[bool, typer.Option(help="Include uncommitted changes")] = False,
    verbose: VerboseOpt = False,
) -> None:
    """Check credentials, the sandbox, and that every project installs and passes its tests offline."""
    settings = _setup(verbose, None)

    async def go() -> bool:
        from .adapters import adapter_for
        from .runner import ProjectRunner
        from .workspace import prepare_local_workspace

        ok_all = True

        def row(ok: bool, label: str, detail: str) -> None:
            nonlocal ok_all
            ok_all &= ok
            typer.secho(f"{'✔' if ok else '✘'} {label:<28} {detail}", fg="green" if ok else "red")

        row(
            bool(os.environ.get("ANTHROPIC_API_KEY")),
            "ANTHROPIC_API_KEY",
            "set"
            if os.environ.get("ANTHROPIC_API_KEY")
            else "not set (needed for CI; locally a Claude Code login may work)",
        )
        token = github_token()
        row(token is not None, "GitHub token", "found" if token else "missing (GITHUB_TOKEN or `gh auth login`)")
        sb = await _pick_sandbox(sandbox, settings)
        _, why = await sb.available()
        row(True, f"sandbox: {sb.name}", why)

        ws = await asyncio.to_thread(prepare_local_workspace, path, settings.cache_dir, "HEAD", uncommitted)
        try:
            from_file = (ws.head / CONFIG_FILE).exists()
            cfg = load_repo_config(ws.head)
            row(True, "config", CONFIG_FILE if from_file else "auto-detected (run `pr-review init` to pin it)")
            runner = ProjectRunner(sb, settings)
            for p in cfg.projects:
                if not p.enabled:
                    typer.secho(f"- {p.name:<28} disabled ({p.language})", fg="bright_black")
                    continue
                t = time.monotonic()
                try:
                    await runner.ensure_installed(ws.head, p)
                    row(True, f"{p.name}: install", f"{time.monotonic() - t:.0f}s")
                except Exception as e:
                    row(False, f"{p.name}: install", str(e)[:600])
                    continue
                if adapter_for(p).can_run_tests(p):
                    run = await runner.run_tests(ws.head, p)
                    row(
                        not run.failed and not run.load_error and not run.timed_out,
                        f"{p.name}: tests (offline)",
                        run.summary(),
                    )
                else:
                    typer.secho(
                        f"- {p.name + ': tests':<28} no supported test runner (static evidence only)", fg="yellow"
                    )
                diags, errs = await runner.run_checks(ws.head, p)
                row(not errs, f"{p.name}: static checks", f"{len(diags)} diagnostic(s)" + (f"; {errs}" if errs else ""))
        finally:
            ws.cleanup()
        return ok_all

    ok = asyncio.run(go())
    raise typer.Exit(0 if ok else 1)


@app.command()
def scan(
    path: Annotated[Path, typer.Argument(help="Local repository to scan")] = Path("."),
    project: Annotated[list[str] | None, typer.Option("--project", "-p", help="Only these projects")] = None,
    max_budget_usd: Annotated[float, typer.Option(help="Total spend cap for the scan")] = 10.0,
    max_chunks: Annotated[int | None, typer.Option(help="Review at most N chunks")] = None,
    out: Annotated[Path, typer.Option(help="Directory for the report files")] = Path("."),
    uncommitted: Annotated[bool, typer.Option(help="Include uncommitted changes (never ignored files)")] = False,
    concurrency: Annotated[int, typer.Option(help="Chunks reviewed in parallel")] = 3,
    sandbox: SandboxOpt = "auto",
    model: ModelOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Scan a local repo for bugs, highest-risk code first. Writes pr-review-report.md + findings.json."""
    from . import actions
    from .render import findings_json, scan_report

    settings = _setup(verbose, model)
    _require_anthropic_credentials()

    async def go():
        sb = await _pick_sandbox(sandbox, settings)
        return await actions.scan(
            path, settings, sb, _progress, project, max_budget_usd, max_chunks, uncommitted, concurrency
        )

    res, record = asyncio.run(go())
    out.mkdir(parents=True, exist_ok=True)
    report = scan_report(res.repo, res.sha, res.kept, res.stats, res.notes, res.chunks_done, res.chunks_total)
    (out / "pr-review-report.md").write_text(report)
    (out / "findings.json").write_text(findings_json(res.kept))
    verified = sum(v.tier == "verified" for v in res.kept)
    typer.secho(
        f"\n{verified} verified, {len(res.kept) - verified} possible finding(s); "
        f"{res.stats.dropped} dropped by verification. ${res.stats.cost_usd:.2f}",
        fg="green",
        bold=True,
    )
    for v in res.kept:
        f = v.finding
        fix = f"  [fix {v.fix.status}]" if v.fix else ""
        typer.echo(f"  [{v.tier}] {f.severity:<8} {f.file}:{f.line_start}  {f.title}{fix}")
    if res.chunks_done < res.chunks_total:
        typer.secho(
            f"Stopped after {res.chunks_done} of {res.chunks_total} chunks. Run the same command again to "
            "continue: reviewed chunks are cached, so the budget only goes to the rest.",
            fg="yellow",
        )
    typer.echo(f"Report: {out / 'pr-review-report.md'}")
    _saved(record, settings)


@app.command()
def review(
    target: Annotated[str, typer.Argument(help="owner/repo#123 or a PR URL")],
    post: Annotated[bool, typer.Option(help="Post the review to GitHub (default: dry run)")] = False,
    budget_usd: Annotated[float | None, typer.Option(help="Spend cap for this review")] = None,
    json_out: Annotated[Path | None, typer.Option("--json", help="Also write findings as JSON")] = None,
    sandbox: SandboxOpt = "auto",
    model: ModelOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Review one pull request. Prints the review; --post publishes it."""
    from . import actions
    from .render import findings_json

    settings = _setup(verbose, model)
    if budget_usd is not None:
        settings.review_budget_usd = budget_usd
    _require_anthropic_credentials()

    async def go():
        sb = await _pick_sandbox(sandbox, settings)
        return await actions.review_pr(target, settings, sb, _progress, post)

    res, record = asyncio.run(go())
    if json_out:
        json_out.write_text(findings_json(res.kept))
    typer.echo("\n" + res.summary)
    if res.inline:
        typer.secho(f"--- {len(res.inline)} inline comment(s) ---", fg="cyan")
        for c in res.inline:
            typer.echo(f"\n{c['path']}:{c['line']}\n{c['body']}")
    if post:
        typer.secho(f"posted: {res.review_url or '(no new inline comments)'}; summary: {res.summary_url}", fg="green")
    else:
        typer.secho("dry run: nothing was posted (use --post)", fg="yellow", err=True)
    _saved(record, settings)


@app.command("review-local")
def review_local(
    path: Annotated[Path, typer.Argument(help="Local repository")] = Path("."),
    base: Annotated[str, typer.Option(help="Base ref (the diff is taken from the merge base)")] = "main",
    head: Annotated[str, typer.Option(help="Head ref")] = "HEAD",
    budget_usd: Annotated[float | None, typer.Option(help="Spend cap for this review")] = None,
    json_out: Annotated[Path | None, typer.Option("--json", help="Also write findings as JSON")] = None,
    sandbox: SandboxOpt = "auto",
    model: ModelOpt = None,
    verbose: VerboseOpt = False,
) -> None:
    """Review a local branch (base...head) like a PR, without GitHub. Nothing is posted."""
    from . import actions
    from .render import findings_json

    settings = _setup(verbose, model)
    if budget_usd is not None:
        settings.review_budget_usd = budget_usd
    _require_anthropic_credentials()

    async def go():
        sb = await _pick_sandbox(sandbox, settings)
        return await actions.review_local(path, base, head, settings, sb, _progress)

    res, record = asyncio.run(go())
    if json_out:
        json_out.write_text(findings_json(res.kept))
    typer.echo("\n" + res.summary)
    for c in res.inline:
        typer.echo(f"\n--- {c['path']}:{c['line']} ---\n{c['body']}")
    _saved(record, settings)


def _saved(record, settings: Settings) -> None:
    typer.secho(f"Saved run {record.id}. Browse it with `pr-review ui`.", fg="green", err=True)


@app.command()
def ui(
    runs_dir: Annotated[Path | None, typer.Option(help="Folder of run JSON files (default: your run history)")] = None,
    port: Annotated[int, typer.Option(help="Port on 127.0.0.1")] = 8765,
    open_browser: Annotated[bool, typer.Option("--open/--no-open", help="Open a browser tab")] = True,
) -> None:
    """Open the local dashboard: every scan and review, with each bug's evidence, code and fix."""
    from .ui.server import serve

    folder = runs_dir or Settings().runs_dir
    folder.mkdir(parents=True, exist_ok=True)
    serve(folder, port=port, open_browser=open_browser)


if __name__ == "__main__":
    app()
