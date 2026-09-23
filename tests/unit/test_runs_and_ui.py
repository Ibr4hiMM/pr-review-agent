import json
import subprocess
import threading
import time
import urllib.error
import urllib.request

import pytest

from pr_review_agent.config import Settings
from pr_review_agent.fixes import plan_edits, unified_patch
from pr_review_agent.models import Evidence, Finding, FixEdit, FixResult, VerifiedFinding
from pr_review_agent.render import Stats
from pr_review_agent.runs import list_runs, load_run, new_run, save_run
from pr_review_agent.ui.jobs import JobManager
from pr_review_agent.ui.server import make_server

SRC = "export function pageCount(total: number, size: number) {\n  return Math.floor(total / size);\n}\n"


def vf(fp="abcdef123456", fix=None):
    f = Finding(
        title="pageCount drops the last page",
        severity="high",
        category="logic",
        project="shop",
        file="shop/src/page.ts",
        line_start=2,
        line_end=2,
        explanation="floor",
        confidence=0.9,
        evidence=[Evidence(kind="failing_test", test_code="it()", test_output="expected 2 to be 3")],
    )
    return VerifiedFinding(finding=f, tier="verified", fingerprint=fp, evidence=f.evidence, fix=fix)


def test_runs_roundtrip_and_summary(tmp_path):
    fix = FixResult(status="verified", patch="diff --git a/x b/x\n")
    rec = new_run(
        "scan",
        "me/shop",
        "/code/shop",
        "a" * 40,
        "claude-opus-5",
        Stats(cost_usd=0.5),
        [vf(fix=fix), vf("111111111111")],
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
def repo(tmp_path, repo_cfg):
    """A git repo with the buggy file committed, and a run whose fix targets it."""
    root = tmp_path / "shop"
    (root / "shop/src").mkdir(parents=True)
    (root / "shop/package.json").write_text('{"devDependencies": {"typescript": "5", "vitest": "3"}}')
    (root / "shop/tsconfig.json").write_text("{}")
    (root / "shop/src/page.ts").write_text(SRC)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=root, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qam", "x", "--allow-empty"],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "code"], cwd=root, check=True)
    subprocess.run(["git", "branch", "feature"], cwd=root, check=True)
    return root


@pytest.fixture
def server(tmp_path, repo):
    from pr_review_agent.config import ProjectConfig, RepoConfig

    cfg = RepoConfig(projects=[ProjectConfig(name="shop", path="shop", language="typescript")])
    changes = plan_edits(repo, cfg, [FixEdit(file="shop/src/page.ts", old="Math.floor", new="Math.ceil")])
    fix = FixResult(status="verified", patch=unified_patch(changes), patched={c.file: c.patched for c in changes})
    runs = tmp_path / "runs"
    rec = new_run(
        "review-local",
        "shop",
        "main...feature",
        "b" * 40,
        "m",
        Stats(),
        [vf(fix=fix)],
        [],
        [],
        repo_path=str(repo),
        sources={"shop/src/page.ts": SRC},
    )
    save_run(rec, runs)
    settings = Settings(data_dir=tmp_path / "data", cache_dir=tmp_path / "cache")

    async def fake_executor(job, settings, progress):
        progress("working")
        return rec.id

    srv, app = make_server(runs, port=0, settings=settings, jobs=JobManager(settings, executor=fake_executor))
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield {"base": f"http://127.0.0.1:{srv.server_address[1]}", "rec": rec, "app": app, "repo": repo}
    srv.shutdown()
    srv.server_close()


def call(s, path, body=None, token=True, origin=True, host=None):
    headers = {}
    if token:
        headers["X-PR-Review-Token"] = s["app"].token
    if host:
        headers["Host"] = host
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
        if origin:
            headers["Origin"] = s["base"]
    req = urllib.request.Request(
        s["base"] + path, data=data, headers=headers, method="POST" if body is not None else "GET"
    )
    try:
        with urllib.request.urlopen(req) as r:
            raw = r.read()
            return r.status, r.headers, (json.loads(raw) if "json" in r.headers.get("Content-Type", "") else raw)
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, e.headers, (json.loads(raw) if raw.startswith(b"{") else raw)


def test_page_embeds_session_token_and_api_requires_it(server):
    status, headers, body = call(server, "/", token=False)
    assert status == 200 and server["app"].token.encode() in body and b"__SESSION_TOKEN__" not in body
    assert "script-src 'self'" in headers["Content-Security-Policy"]
    assert call(server, "/app.js", token=False)[0] == 200
    assert call(server, "/api/runs", token=False)[0] == 401
    assert call(server, "/api/runs", host="evil.example")[0] == 403  # DNS rebinding
    status, _, runs = call(server, "/api/runs")
    assert status == 200 and runs[0]["id"] == server["rec"].id


def test_run_includes_sources_triage_and_local_path(server):
    rec = server["rec"]
    _, _, run = call(server, f"/api/runs/{rec.id}")
    assert run["sources"]["shop/src/page.ts"] == SRC
    assert run["findings"][0]["fix"]["patched"]["shop/src/page.ts"].count("Math.ceil") == 1
    assert run["local_path"] == str(server["repo"]) and run["triage"] == {}
    assert call(server, f"/api/runs/{rec.id}/patch/abcdef123456")[2].startswith(b"diff --git")
    assert call(server, "/api/runs/nope")[0] == 404


def test_posts_must_be_same_origin(server):
    rec = server["rec"]
    body = {"run_id": rec.id, "fp": "abcdef123456", "status": "fixed"}
    assert call(server, "/api/triage", body, origin=False)[0] == 403
    assert call(server, "/api/triage", body, token=False)[0] == 401


def test_triage_is_saved_and_returned(server):
    rec = server["rec"]
    status, _, entry = call(
        server, "/api/triage", {"run_id": rec.id, "fp": "abcdef123456", "status": "wont_fix", "note": "intended"}
    )
    assert status == 200 and entry["status"] == "wont_fix"
    assert call(server, f"/api/runs/{rec.id}")[2]["triage"]["abcdef123456"]["note"] == "intended"
    assert call(server, "/api/triage", {"run_id": rec.id, "fp": "abcdef123456", "status": "bogus"})[0] == 400
    assert call(server, "/api/triage", {"run_id": rec.id, "fp": "000000000000", "status": "open"})[0] == 400


def test_fix_check_apply_undo_on_a_real_repo(server):
    rec, repo = server["rec"], server["repo"]
    body = {"run_id": rec.id, "fp": "abcdef123456"}
    assert call(server, "/api/fix/check", body)[2]["state"] == "applies"
    status, _, result = call(server, "/api/fix/apply", body)
    assert status == 200 and result["state"] == "applied"
    assert "Math.ceil" in (repo / "shop/src/page.ts").read_text()
    assert call(server, f"/api/runs/{rec.id}")[2]["triage"]["abcdef123456"]["status"] == "fixed"
    assert call(server, "/api/fix/check", body)[2]["state"] == "applied"
    assert call(server, "/api/fix/apply", body)[0] == 400  # already applied
    assert call(server, "/api/fix/undo", body)[2]["state"] == "applies"
    assert (repo / "shop/src/page.ts").read_text() == SRC
    # The file changed since the run: the patch no longer fits and nothing is written.
    (repo / "shop/src/page.ts").write_text(SRC.replace("total / size", "total / (size || 1)"))
    status, _, result = call(server, "/api/fix/check", body)
    assert result["state"] == "conflict" and "changed since this run" in result["detail"]
    assert call(server, "/api/fix/apply", body)[0] == 400


def test_repo_info_and_jobs(server, repo):
    status, _, info = call(server, f"/api/repo-info?path={repo}")
    assert status == 200 and info["name"] == "shop" and set(info["branches"]) == {"main", "feature"}
    assert [p["name"] for p in info["projects"]] == ["shop"]
    assert call(server, "/api/repo-info?path=/definitely/not/here")[0] == 400

    status, _, job = call(server, "/api/jobs", {"kind": "scan", "repo_path": str(repo), "budget_usd": 1})
    assert status == 201 and job["title"] == "Scan shop"
    for _ in range(50):
        job = call(server, f"/api/jobs/{job['id']}")[2]
        if job["status"] == "done":
            break
        time.sleep(0.05)
    assert job["status"] == "done" and job["run_id"] == server["rec"].id and "working" in job["log"][0]
    assert call(server, "/api/jobs")[2][0]["id"] == job["id"]
    assert str(repo.resolve()) in call(server, "/api/repos")[2]

    bad = [
        ({"kind": "scan", "repo_path": "/nope"}, "doesn't exist"),
        ({"kind": "scan", "repo_path": str(repo), "budget_usd": 1000}, "spending limit"),
        ({"kind": "scan", "repo_path": str(repo), "projects": ["other"]}, "can't be scanned"),
        ({"kind": "review-local", "repo_path": str(repo), "base": "main", "head": "nope"}, "no branch"),
        ({"kind": "review", "target": "not a pr"}, "owner/repo#123"),
    ]
    for body, message in bad:
        status, _, err = call(server, "/api/jobs", body)
        assert status == 400 and message in err["error"], (body, err)


def test_job_errors_are_plain_language():
    import httpx
    from githubkit.exception import RequestFailed
    from githubkit.response import Response

    from pr_review_agent.ui.jobs import Job, friendly_error

    job = Job(id="a" * 10, kind="review", params={"target": "me/app#9"}, title="t")
    resp = Response(httpx.Response(404, request=httpx.Request("GET", "https://api.github.com/x")), None)
    assert friendly_error(RequestFailed(resp), job).startswith("GitHub couldn't find me/app#9")
    assert friendly_error(ValueError("plain"), job) == "plain"
