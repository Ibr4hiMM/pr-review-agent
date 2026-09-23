import json
import threading
import urllib.error
import urllib.request

import pytest

from pr_review_agent.models import Evidence, Finding, FixResult, VerifiedFinding
from pr_review_agent.render import Stats
from pr_review_agent.runs import list_runs, load_run, new_run, save_run
from pr_review_agent.ui.server import make_server


def vf(fp="abcdef123456", fix=True):
    f = Finding(
        title="pageCount drops the last page",
        severity="high",
        category="logic",
        project="shop",
        file="shop/src/pagination.ts",
        line_start=3,
        line_end=3,
        explanation="floor",
        confidence=0.9,
        evidence=[Evidence(kind="failing_test", test_code="it()", test_output="expected 2 to be 3")],
    )
    return VerifiedFinding(
        finding=f,
        tier="verified",
        fingerprint=fp,
        evidence=f.evidence,
        fix=FixResult(status="verified", patch="diff --git a/x b/x\n") if fix else None,
    )


def test_runs_roundtrip_and_summary(tmp_path):
    rec = new_run(
        "scan",
        "me/shop",
        "/code/shop",
        "a" * 40,
        "claude-opus-5",
        Stats(cost_usd=0.5),
        [vf(), vf("111111111111", fix=False)],
        [(vf().finding, "duplicate")],
        ["note"],
        chunks_done=1,
        chunks_total=2,
    )
    save_run(rec, tmp_path)
    [summary] = list_runs(tmp_path)
    assert summary["verified"] == 2 and summary["fixes"] == 1 and summary["dropped"] == 1
    assert summary["severities"]["high"] == 2 and summary["cost_usd"] == 0.5
    assert load_run(tmp_path, rec.id).findings[0].fix.status == "verified"
    assert load_run(tmp_path, "../../etc/passwd") is None
    (tmp_path / "20990101-000000-scan-ffffff.json").write_text("{broken")
    assert len(list_runs(tmp_path)) == 1  # unreadable files are skipped


@pytest.fixture
def server(tmp_path):
    rec = new_run("review-local", "shop", "main...feature", "b" * 40, "m", Stats(), [vf()], [], [])
    save_run(rec, tmp_path)
    srv = make_server(tmp_path, port=0)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", rec
    srv.shutdown()
    srv.server_close()


def get(url, host=None):
    req = urllib.request.Request(url, headers={"Host": host} if host else {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.headers, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def test_dashboard_serves_app_and_api(server):
    base, rec = server
    status, headers, body = get(base + "/")
    assert status == 200 and b"/app.js" in body
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert get(base + "/app.js")[0] == 200 and get(base + "/app.css")[0] == 200
    assert json.loads(get(base + "/api/runs")[2])[0]["id"] == rec.id
    run = json.loads(get(f"{base}/api/runs/{rec.id}")[2])
    assert run["findings"][0]["fix"]["status"] == "verified"
    status, headers, body = get(f"{base}/api/runs/{rec.id}/patch/abcdef123456")
    assert status == 200 and body.startswith(b"diff --git") and "attachment" in headers["Content-Disposition"]


def test_dashboard_rejects_bad_requests(server):
    base, rec = server
    assert get(f"{base}/api/runs/nope")[0] == 404
    assert get(f"{base}/api/runs/{rec.id}/patch/000000000000")[0] == 404
    assert get(f"{base}/../../etc/passwd")[0] == 404
    assert get(base + "/api/runs", host="evil.example")[0] == 403  # DNS rebinding
