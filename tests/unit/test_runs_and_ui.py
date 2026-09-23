import asyncio
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


async def test_scan_record_keeps_what_is_needed_to_continue(tmp_path, monkeypatch):
    from pr_review_agent import actions
    from pr_review_agent.scan.runner import ScanOutcome

    async def fake_scan(*_args):
        return ScanOutcome(repo="me/shop", sha="a" * 40, chunks_done=1, chunks_total=3)

    monkeypatch.setattr(actions, "run_scan", fake_scan)
    settings = Settings(data_dir=tmp_path / "data", cache_dir=tmp_path / "cache")
    first_id = "20260101-000000-scan-abcdef"
    _, rec = await actions.scan(tmp_path, settings, None, print, ["shop"], 2.0, None, True, continues=first_id)
    rec = load_run(settings.runs_dir, rec.id)
    assert (rec.budget_usd, rec.projects, rec.uncommitted, rec.continues) == (2.0, ["shop"], True, first_id)
    assert rec.unfinished() and list_runs(settings.runs_dir)[0]["continues"] == first_id
    assert rec.scan_request() == {"repo_path": str(tmp_path.resolve()), "projects": ["shop"], "uncommitted": True}

    # Runs saved before the projects were recorded: they're read back from the target.
    old = rec.model_copy(update={"projects": [], "uncommitted": None, "target": f"{rec.repo_path} (api, web)"})
    assert old.scan_request()["projects"] == ["api", "web"] and old.scan_request()["uncommitted"] is True
    assert not rec.model_copy(update={"chunks_done": 3}).unfinished()


def test_unfinished_scan_can_be_continued(server, repo):
    runs_dir = server["app"].runs_dir

    def scan_run(done, total, kind="scan"):
        rec = new_run(
            kind,
            "shop",
            str(repo),
            "a" * 40,
            "m",
            Stats(cost_usd=2.0),
            [],
            [],
            [],
            chunks_done=done,
            chunks_total=total,
            repo_path=str(repo),
            budget_usd=2.0,
            projects=["shop"],
            uncommitted=False,
        )
        save_run(rec, runs_dir)
        return rec

    rec = scan_run(1, 3)
    status, _, job = call(server, f"/api/runs/{rec.id}/continue", {"budget_usd": 4})
    assert status == 201 and job["title"] == "Scan shop (shop), continued"
    assert job["params"] == {
        "repo_path": str(repo.resolve()),
        "projects": ["shop"],
        "budget_usd": 4.0,
        "uncommitted": False,
        "continues": rec.id,
    }

    for done_rec in (scan_run(3, 3), scan_run(0, 0, "review-local")):
        status, _, err = call(server, f"/api/runs/{done_rec.id}/continue", {"budget_usd": 4})
        assert status == 400 and "stopped before reviewing every chunk" in err["error"]
    status, _, err = call(server, f"/api/runs/{rec.id}/continue", {"budget_usd": 0.5})
    assert status == 400 and "between $1.00" in err["error"]  # too little to review a single chunk


def test_a_scan_is_not_continued_twice(tmp_path, repo):
    started = threading.Event()
    release = threading.Event()

    async def slow(job, settings, progress):
        started.set()
        await asyncio.to_thread(release.wait, 5)
        return "x"

    jobs = JobManager(Settings(data_dir=tmp_path / "d", cache_dir=tmp_path / "c"), executor=slow)
    run_id = "20260101-000000-scan-abcdef"
    job = jobs.submit("scan", {"repo_path": str(repo), "budget_usd": 2, "continues": run_id})
    assert started.wait(5) and jobs.continuing(run_id) is job
    release.set()
    for _ in range(100):
        if job.status == "done":
            break
        time.sleep(0.02)
    assert job.status == "done" and jobs.continuing(run_id) is None


def test_job_errors_are_plain_language():
    import httpx
    from githubkit.exception import RequestFailed
    from githubkit.response import Response

    from pr_review_agent.ui.jobs import Job, friendly_error

    job = Job(id="a" * 10, kind="review", params={"target": "me/app#9"}, title="t")
    resp = Response(httpx.Response(404, request=httpx.Request("GET", "https://api.github.com/x")), None)
    assert friendly_error(RequestFailed(resp), job).startswith("GitHub couldn't find me/app#9")
    assert friendly_error(ValueError("plain"), job) == "plain"


def test_job_state_is_built_from_pipeline_events():
    from pr_review_agent.ui.jobs import Job

    job = Job(id="b" * 10, kind="scan", params={"budget_usd": 5}, title="Scan shop")
    for event in [
        {"type": "phase", "phase": "prepare"},
        {
            "type": "plan",
            "budget": 5.0,
            "chunks": [
                {"label": "routes.ts 1–1200", "files": ["a"], "lines": 1200},
                {"label": "auth.ts", "files": ["b"], "lines": 80},
            ],
        },
        {"type": "phase", "phase": "review"},
        {"type": "chunk", "i": 0, "state": "reviewing"},
        {"type": "spent", "usd": 0.0, "reserved": 2.0},
        {"type": "test"},
        {"type": "fix_check"},
        {
            "type": "finding",
            "i": 0,
            "title": "t",
            "severity": "high",
            "file": "a",
            "line": 3,
            "tier": "verified",
            "fix": "verified",
        },
        {"type": "chunk", "i": 0, "state": "done", "proven": 1},
        {"type": "spent", "usd": 1.2, "reserved": 0.0},
        {"type": "chunk", "i": 1, "state": "skipped"},
        {"type": "chunk", "i": 9, "state": "done"},  # out of range: ignored
    ]:
        job.apply_event(event)
    st = job.to_dict()["state"]
    assert st["phase"] == "review" and st["budget"] == 5.0 and st["spent"] == 1.2
    assert [c["state"] for c in st["chunks"]] == ["done", "skipped"] and st["chunks"][0]["proven"] == 1
    assert st["tests"] == 1 and st["fix_checks"] == 1 and st["findings"][0]["fix"] == "verified"


def test_overview_totals_and_story(server):
    rec = server["rec"]
    status, _, ov = call(server, "/api/overview")
    assert status == 200 and ov["totals"]["runs"] == 1 and ov["totals"]["proven"] == 1 and ov["totals"]["fixes"] == 1
    assert ov["totals"]["open"] == 1 and ov["recent"][0]["id"] == rec.id
    assert ov["story"]["finding"]["fingerprint"] == "abcdef123456" and "Math.floor" in ov["story"]["source"]
    call(server, "/api/triage", {"run_id": rec.id, "fp": "abcdef123456", "status": "fixed"})
    assert call(server, "/api/overview")[2]["totals"]["open"] == 0
    for asset in ("/landing.js", "/landing.css", "/tokens.css"):
        assert call(server, asset, token=False)[0] == 200


def test_pasted_paths_are_cleaned_up():
    from pr_review_agent.ui.repos import clean_path

    assert clean_path("'/Users/me/My Repo'") == "/Users/me/My Repo"
    assert clean_path("file:///Users/me/My%20Repo/") == "/Users/me/My Repo"
    assert clean_path("/Users/me/My\\ Repo/") == "/Users/me/My Repo"
    assert clean_path('  "~/code/app"  ') == "~/code/app"


def test_folder_picker_endpoint(server, monkeypatch):
    import subprocess
    import sys

    import pr_review_agent.ui.repos as repos

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(repos.shutil, "which", lambda name: "/usr/bin/osascript")
    picked = subprocess.CompletedProcess([], 0, stdout=f"{server['repo']}/\n", stderr="")
    monkeypatch.setattr(repos.subprocess, "run", lambda *a, **k: picked)
    status, _, body = call(server, "/api/pick-folder", {})
    assert status == 200 and body["path"] == str(server["repo"])
    cancelled = subprocess.CompletedProcess([], 1, stdout="", stderr="User canceled. (-128)")
    monkeypatch.setattr(repos.subprocess, "run", lambda *a, **k: cancelled)
    assert call(server, "/api/pick-folder", {})[2] == {"path": None}
    monkeypatch.setattr(sys, "platform", "linux")
    status, _, body = call(server, "/api/pick-folder", {})
    assert status == 400 and "only works on macOS" in body["error"]


def test_pull_request_listing(server, monkeypatch):
    import httpx
    import respx

    import pr_review_agent.ui.github as gh

    monkeypatch.setattr(gh, "github_token", lambda: "t")
    pr = {
        "id": 1,
        "node_id": "x",
        "number": 2,
        "title": "merge changes",
        "state": "closed",
        "locked": False,
        "draft": False,
        "merged_at": "2026-05-06T08:25:56Z",
        "updated_at": "2026-05-06T08:25:56Z",
        "user": {
            "login": "me",
            "id": 1,
            "node_id": "u",
            "avatar_url": "",
            "gravatar_id": "",
            "url": "",
            "html_url": "",
            "followers_url": "",
            "following_url": "",
            "gists_url": "",
            "starred_url": "",
            "subscriptions_url": "",
            "organizations_url": "",
            "repos_url": "",
            "events_url": "",
            "received_events_url": "",
            "type": "User",
            "site_admin": False,
        },
        "head": {"ref": "dev", "sha": "a", "label": "me:dev", "repo": None, "user": None},
        "base": {"ref": "main", "sha": "b", "label": "me:main", "repo": None, "user": None},
        "html_url": "https://github.com/me/app/pull/2",
    }
    with respx.mock(assert_all_called=False) as mock:
        mock.get("https://api.github.com/repos/me/app/pulls").mock(return_value=httpx.Response(200, json=[pr]))
        mock.get("https://api.github.com/repos/me/nope/pulls").mock(
            return_value=httpx.Response(404, json={"message": "Not Found"})
        )
        mock.route(host="127.0.0.1").pass_through()
        status, _, body = call(server, "/api/github/prs?repo=me/app&state=all")
        assert status == 200 and body[0]["number"] == 2 and body[0]["state"] == "merged" and body[0]["head"] == "dev"
        status, _, body = call(server, "/api/github/prs?repo=me/nope")
        assert status == 400 and "Can't find me/nope" in body["error"]
    status, _, body = call(server, "/api/github/prs?repo=not%20a%20slug")
    assert status == 400 and "owner/name" in body["error"]
    monkeypatch.setattr(gh, "github_token", lambda: None)
    assert "gh auth login" in call(server, "/api/github/prs?repo=me/app")[2]["error"]
