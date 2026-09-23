"""Local dashboard server: the single-page app plus a JSON API.

Safety, since the API can start paid jobs and write to your repositories:
- listens on 127.0.0.1 only and rejects other Host headers (DNS rebinding);
- every /api request must carry the per-session token that is only embedded in the page itself, which
  other websites can't read; state-changing requests must also be same-origin JSON POSTs;
- strict Content-Security-Policy; the page renders all repo/agent content as text.
"""

from __future__ import annotations

import asyncio
import errno
import hmac
import json
import os
import re
import secrets
import sys
import webbrowser
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from ..config import Settings, github_token
from ..runs import RUN_ID_RE, RunRecord, list_runs, load_run
from ..workspace import repo_slug
from . import github as gh_lookup
from .jobs import Job, JobManager
from .repos import RepoError, apply_fix, fix_state, pick_folder, repo_info, repo_root, undo_fix
from .store import Store

STATIC = {
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/tokens.css": ("tokens.css", "text/css; charset=utf-8"),
    "/landing.js": ("landing.js", "text/javascript; charset=utf-8"),
    "/landing.css": ("landing.css", "text/css; charset=utf-8"),
}
CSP = (
    "default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data: blob:; "
    "style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
MAX_BODY = 1_000_000
_RUN = re.compile(r"^/api/runs/([^/]+)$")
_PATCH = re.compile(r"^/api/runs/([^/]+)/patch/([0-9a-f]{12})$")
_JOB = re.compile(r"^/api/jobs/([0-9a-f]{10})$")
_JOB_CANCEL = re.compile(r"^/api/jobs/([0-9a-f]{10})/cancel$")
_RUN_CONTINUE = re.compile(r"^/api/runs/([^/]+)/continue$")


class App:
    """Shared state for all request threads."""

    def __init__(self, runs_dir: Path, settings: Settings, jobs: JobManager | None = None):
        self.runs_dir = runs_dir
        self.settings = settings
        self.token = secrets.token_urlsafe(32)
        self.store = Store(settings.data_dir / "dashboard.json")
        self.jobs = jobs or JobManager(settings, on_done=self._job_done)

    def _job_done(self, job) -> None:
        if job.params.get("repo_path"):
            path = job.params["repo_path"]
            self.store.remember_repo(path, repo_slug(Path(path)))

    def run(self, run_id: str) -> RunRecord | None:
        return load_run(self.runs_dir, run_id) if RUN_ID_RE.match(run_id) else None

    def continue_scan(self, run_id: str, budget_usd: Any) -> Job:
        """Scan again with the same settings. Reviewed chunks come from the cache, so the new usage limit
        goes to the chunks this run didn't get to, and the new run holds everything found by both."""
        record = self.run(run_id)
        if record is None or not record.unfinished():
            raise RepoError("Only a scan that stopped before reviewing every chunk can be continued.")
        if self.jobs.continuing(record.id):
            raise RepoError("This scan is already being continued.")
        return self.jobs.submit("scan", {**record.scan_request(), "budget_usd": budget_usd, "continues": record.id})


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "pr-review-ui"

    def __init__(self, *args: Any, app: App, **kwargs: Any) -> None:
        self.app = app
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:  # keep the terminal quiet
        pass

    # ---------- plumbing ----------

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: Any, status: int = HTTPStatus.OK) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _origins(self) -> set[str]:
        port = self.server.server_address[1]
        return {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    def _host_ok(self) -> bool:
        return f"http://{self.headers.get('Host', '')}" in self._origins()

    def _token_ok(self) -> bool:
        return hmac.compare_digest(self.headers.get("X-PR-Review-Token", ""), self.app.token)

    def _body(self) -> dict[str, Any] | None:
        if self.headers.get("Origin") not in self._origins():
            self._error(HTTPStatus.FORBIDDEN, "cross-origin request rejected")
            return None
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "send JSON")
            return None
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request too large")
            return None
        try:
            data = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._error(HTTPStatus.BAD_REQUEST, "invalid JSON")
            return None
        if not isinstance(data, dict):
            self._error(HTTPStatus.BAD_REQUEST, "expected a JSON object")
            return None
        return data

    def _guard(self) -> bool:
        if not self._host_ok():
            self._send(HTTPStatus.FORBIDDEN, b"forbidden host", "text/plain")
            return False
        if urlparse(self.path).path.startswith("/api/") and not self._token_ok():
            self._error(HTTPStatus.UNAUTHORIZED, "missing or wrong session token; reload the page")
            return False
        return True

    # ---------- GET ----------

    def do_GET(self) -> None:
        if not self._guard():
            return
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        if path == "/":
            html = resources.files("pr_review_agent.ui").joinpath("static/index.html").read_text()
            self._send(
                HTTPStatus.OK, html.replace("__SESSION_TOKEN__", self.app.token).encode(), "text/html; charset=utf-8"
            )
        elif path in STATIC:
            name, ctype = STATIC[path]
            self._send(
                HTTPStatus.OK, resources.files("pr_review_agent.ui").joinpath(f"static/{name}").read_bytes(), ctype
            )
        elif path == "/api/runs":
            self._json(list_runs(self.app.runs_dir))
        elif m := _RUN.match(path):
            record = self.app.run(m[1])
            if record is None:
                return self._error(HTTPStatus.NOT_FOUND, "run not found")
            data = record.model_dump(mode="json")
            data["triage"] = self.app.store.triage_for(record.repo)
            data["local_path"] = record.repo_path or self.app.store.path_for_slug(record.repo)
            self._json(data)
        elif m := _PATCH.match(path):
            record = self.app.run(m[1])
            finding = next((v for v in (record.findings if record else []) if v.fingerprint == m[2]), None)
            if finding is None or finding.fix is None:
                return self._error(HTTPStatus.NOT_FOUND, "no fix for this finding")
            self._send(
                HTTPStatus.OK,
                finding.fix.patch.encode(),
                "text/x-diff; charset=utf-8",
                {"Content-Disposition": f'attachment; filename="fix-{m[2]}.patch"'},
            )
        elif path == "/api/jobs":
            self._json(self.app.jobs.list())
        elif m := _JOB.match(path):
            job = self.app.jobs.get(m[1])
            if job is None:
                return self._error(HTTPStatus.NOT_FOUND, "job not found")
            since = int((query.get("since") or ["0"])[0] or 0)
            self._json(job.to_dict(since=max(0, since)))
        elif path == "/api/repos":
            self._json(self._recent_repos())
        elif path == "/api/repo-info":
            try:
                self._json(repo_info((query.get("path") or [""])[0]))
            except RepoError as e:
                self._error(HTTPStatus.BAD_REQUEST, str(e))
        elif path == "/api/setup":
            self._json(self._setup())
        elif path == "/api/overview":
            self._json(self._overview())
        elif path == "/api/github/repos":
            try:
                self._json(gh_lookup.list_repos())
            except RepoError as e:
                self._error(HTTPStatus.BAD_REQUEST, str(e))
        elif path == "/api/github/prs":
            try:
                self._json(gh_lookup.list_prs((query.get("repo") or [""])[0], (query.get("state") or ["open"])[0]))
            except RepoError as e:
                self._error(HTTPStatus.BAD_REQUEST, str(e))
        else:
            self._error(HTTPStatus.NOT_FOUND, "not found")

    def _recent_repos(self) -> list[str]:
        paths = self.app.store.recent_repos()
        for summary in list_runs(self.app.runs_dir):
            record = self.app.run(summary["id"])
            if record and record.repo_path and record.repo_path not in paths:
                paths.append(record.repo_path)
        return [p for p in paths if Path(p).is_dir()][:12]

    def _overview(self) -> dict[str, Any]:
        """Totals across runs (each bug counted once per repo), recent runs, and one real verified
        finding with its source for the home screen's animation."""
        summaries = list_runs(self.app.runs_dir)
        seen: dict[tuple[str, str], Any] = {}
        story = None
        for summary in summaries[:50]:
            record = self.app.run(summary["id"])
            if record is None:
                continue
            triage = self.app.store.triage_for(record.repo)
            for v in record.findings:
                key = (record.repo, v.fingerprint)
                if key not in seen:
                    seen[key] = (v, triage.get(v.fingerprint, {}).get("status", "open"))
                if (
                    story is None
                    and v.tier == "verified"
                    and v.fix
                    and v.fix.status == "verified"
                    and v.finding.file in record.sources
                ):
                    story = {
                        "run": {
                            "id": record.id,
                            "kind": record.kind,
                            "repo": record.repo,
                            "created_at": record.created_at,
                        },
                        "finding": v.model_dump(mode="json"),
                        "source": record.sources[v.finding.file],
                    }
        found = [v for v, _ in seen.values()]
        return {
            "totals": {
                "runs": len(summaries),
                "proven": sum(v.tier == "verified" for v in found),
                "possible": sum(v.tier == "possible" for v in found),
                "fixes": sum(1 for v in found if v.fix and v.fix.status == "verified"),
                "open": sum(1 for _, status in seen.values() if status == "open"),
                "usage_usd": round(sum(s.get("cost_usd", 0.0) for s in summaries), 2),
            },
            "recent": summaries[:6],
            "story": story,
        }

    def _setup(self) -> dict[str, Any]:
        from ..sandbox import DockerSandbox

        docker_ok, docker_note = asyncio.run(DockerSandbox({}).available())
        return {
            "anthropic_api_key": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "github_login": github_token() is not None,
            "docker": docker_ok,
            "docker_note": docker_note,
            "runs_dir": str(self.app.runs_dir),
            "model": self.app.settings.model,
            "folder_picker": sys.platform == "darwin",
        }

    # ---------- POST ----------

    def do_POST(self) -> None:
        if not self._guard():
            return
        data = self._body()
        if data is None:
            return
        path = urlparse(self.path).path
        try:
            if path == "/api/jobs":
                job = self.app.jobs.submit(str(data.get("kind", "")), data)
                self._json(job.to_dict(), HTTPStatus.CREATED)
            elif m := _RUN_CONTINUE.match(path):
                job = self.app.continue_scan(m[1], data.get("budget_usd"))
                self._json(job.to_dict(), HTTPStatus.CREATED)
            elif m := _JOB_CANCEL.match(path):
                job = self.app.jobs.cancel(m[1])
                if job is None:
                    return self._error(HTTPStatus.NOT_FOUND, "job not found")
                self._json(job.to_dict())
            elif path == "/api/triage":
                record = self._record(data)
                fp = self._fingerprint(record, data)
                entry = self.app.store.set_triage(record.repo, fp, str(data.get("status", "")), data.get("note"))
                self._json(entry)
            elif path == "/api/pick-folder":
                self._json({"path": pick_folder()})
            elif path in ("/api/fix/check", "/api/fix/apply", "/api/fix/undo"):
                self._fix(path.rsplit("/", 1)[1], data)
            else:
                self._error(HTTPStatus.NOT_FOUND, "not found")
        except (RepoError, ValueError) as e:
            self._error(HTTPStatus.BAD_REQUEST, str(e))

    def _record(self, data: dict[str, Any]) -> RunRecord:
        record = self.app.run(str(data.get("run_id", "")))
        if record is None:
            raise RepoError("That run no longer exists.")
        return record

    def _fingerprint(self, record: RunRecord, data: dict[str, Any]) -> str:
        fp = str(data.get("fp", ""))
        if not any(v.fingerprint == fp for v in record.findings):
            raise RepoError("That finding isn't part of this run.")
        return fp

    def _fix(self, action: str, data: dict[str, Any]) -> None:
        record = self._record(data)
        fp = self._fingerprint(record, data)
        finding = next(v for v in record.findings if v.fingerprint == fp)
        if finding.fix is None:
            raise RepoError("This finding has no fix.")
        if action != "check" and finding.fix.status == "failed":
            raise RepoError("This fix failed its checks, so it can't be applied from here.")
        path = str(data.get("repo_path") or record.repo_path or "")
        if not path:
            raise RepoError("Choose the folder of your local copy of this repository.")
        root = repo_root(path)
        result = {"check": fix_state, "apply": apply_fix, "undo": undo_fix}[action](str(root), finding.fix.patch)
        self.app.store.remember_repo(str(root), repo_slug(root) or (record.repo if "/" in record.repo else None))
        if action == "apply":
            self.app.store.set_triage(record.repo, fp, "fixed")  # keeps any note the person wrote
        elif action == "undo":
            self.app.store.set_triage(record.repo, fp, "open")
        self._json(result)


def make_server(
    runs_dir: Path, port: int = 8765, settings: Settings | None = None, jobs: JobManager | None = None
) -> tuple[ThreadingHTTPServer, App]:
    app = App(runs_dir, settings or Settings(), jobs)
    server = ThreadingHTTPServer(("127.0.0.1", port), partial(DashboardHandler, app=app))
    return server, app


def serve(runs_dir: Path, port: int = 8765, open_browser: bool = True) -> None:
    for candidate in range(port, port + 20):
        try:
            server, _app = make_server(runs_dir, candidate)
            break
        except OSError as e:
            if e.errno != errno.EADDRINUSE:
                raise
    else:
        raise SystemExit(f"Ports {port}-{port + 19} are all in use. Pass a free one with --port.")
    if candidate != port:
        print(f"Port {port} is in use (is another dashboard already open?), so using {candidate} instead.")
    url = f"http://127.0.0.1:{candidate}/"
    print(f"pr-review dashboard: {url}  (runs from {runs_dir}; Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
