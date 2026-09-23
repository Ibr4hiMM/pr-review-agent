"""Local dashboard server: static single-page app + a read-only JSON API over the run history.

Binds to 127.0.0.1 only, rejects requests whose Host header isn't this server (DNS-rebinding
protection), and serves everything with a strict Content-Security-Policy.
"""

from __future__ import annotations

import json
import re
import webbrowser
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..runs import RUN_ID_RE, list_runs, load_run

STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}
CSP = (
    "default-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; "
    "style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
    "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)
_RUN = re.compile(r"^/api/runs/([^/]+)$")
_PATCH = re.compile(r"^/api/runs/([^/]+)/patch/([0-9a-f]{12})$")


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "pr-review-ui"

    def __init__(self, *args: Any, runs_dir: Path, **kwargs: Any) -> None:
        self.runs_dir = runs_dir
        super().__init__(*args, **kwargs)

    def log_message(self, format: str, *args: Any) -> None:  # keep the terminal quiet
        pass

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

    def _not_found(self) -> None:
        self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def do_GET(self) -> None:
        if not self._host_ok():
            self._send(HTTPStatus.FORBIDDEN, b"forbidden host", "text/plain")
            return
        path = urlparse(self.path).path
        if path in STATIC:
            name, ctype = STATIC[path]
            body = resources.files("pr_review_agent.ui").joinpath(f"static/{name}").read_bytes()
            self._send(HTTPStatus.OK, body, ctype)
        elif path == "/api/runs":
            self._json(list_runs(self.runs_dir))
        elif m := _RUN.match(path):
            record = load_run(self.runs_dir, m[1]) if RUN_ID_RE.match(m[1]) else None
            if record is None:
                return self._not_found()
            self._json(record.model_dump(mode="json"))
        elif m := _PATCH.match(path):
            record = load_run(self.runs_dir, m[1])
            finding = next((v for v in (record.findings if record else []) if v.fingerprint == m[2]), None)
            if finding is None or finding.fix is None:
                return self._not_found()
            self._send(
                HTTPStatus.OK,
                finding.fix.patch.encode(),
                "text/x-diff; charset=utf-8",
                {"Content-Disposition": f'attachment; filename="fix-{m[2]}.patch"'},
            )
        else:
            self._not_found()


def make_server(runs_dir: Path, port: int = 8765) -> ThreadingHTTPServer:
    return ThreadingHTTPServer(("127.0.0.1", port), partial(DashboardHandler, runs_dir=runs_dir))


def serve(runs_dir: Path, port: int = 8765, open_browser: bool = True) -> None:
    server = make_server(runs_dir, port)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"pr-review dashboard: {url}  (runs from {runs_dir}; Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
