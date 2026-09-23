import json

import httpx
import pytest
import respx

from pr_review_agent.github_client import GitHubClient, PullRequest, parse_target
from pr_review_agent.render import SUMMARY_MARKER

API = "https://api.github.com"


@pytest.mark.parametrize(
    ("target", "expected"),
    [
        ("acme/shop#12", ("acme", "shop", 12)),
        ("https://github.com/acme/shop/pull/7", ("acme", "shop", 7)),
        ("https://github.com/o/r.js/pull/7/", ("o", "r.js", 7)),
    ],
)
def test_parse_target(target, expected):
    assert parse_target(target) == expected


def test_parse_target_rejects_garbage():
    with pytest.raises(ValueError):
        parse_target("not a pr")


def pr() -> PullRequest:
    return PullRequest(
        owner="o",
        repo="r",
        number=5,
        title="t",
        body=None,
        author="me",
        head_sha="h" * 40,
        base_sha="b" * 40,
        base_ref="main",
        head_repo="o/r",
        draft=False,
        url="u",
    )


def review_json(url="https://github.com/o/r/pull/5#pullrequestreview-1"):
    return {
        "id": 1,
        "node_id": "x",
        "user": None,
        "body": "",
        "state": "COMMENTED",
        "html_url": url,
        "pull_request_url": "",
        "_links": {"html": {"href": url}, "pull_request": {"href": url}},
        "commit_id": "h" * 40,
        "author_association": "OWNER",
    }


def comment_json(cid, body):
    return {
        "id": cid,
        "node_id": "x",
        "url": "u",
        "html_url": f"https://github.com/o/r/pull/5#issuecomment-{cid}",
        "body": body,
        "user": None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "issue_url": "u",
        "author_association": "OWNER",
    }


@respx.mock
async def test_create_review_falls_back_to_body_on_422():
    route = respx.post(f"{API}/repos/o/r/pulls/5/reviews").mock(
        side_effect=[
            httpx.Response(422, json={"message": "Line could not be resolved"}),
            httpx.Response(200, json=review_json()),
        ]
    )
    url = await GitHubClient("t").create_review(
        pr(), "body", [{"path": "a.ts", "line": 3, "side": "RIGHT", "body": "BUG"}]
    )
    assert url.endswith("pullrequestreview-1")
    first, second = (json.loads(c.request.content) for c in route.calls)
    assert first["comments"] and first["event"] == "COMMENT" and first["commit_id"] == "h" * 40
    assert second["comments"] == [] and "BUG" in second["body"]


@respx.mock
async def test_upsert_summary_updates_existing_comment():
    respx.get(f"{API}/repos/o/r/issues/5/comments").mock(
        return_value=httpx.Response(200, json=[comment_json(1, "unrelated"), comment_json(2, f"{SUMMARY_MARKER}\nold")])
    )
    patch = respx.patch(f"{API}/repos/o/r/issues/comments/2").mock(
        return_value=httpx.Response(200, json=comment_json(2, "new"))
    )
    create = respx.post(f"{API}/repos/o/r/issues/5/comments")
    await GitHubClient("t").upsert_summary(pr(), f"{SUMMARY_MARKER}\nnew")
    assert patch.called and not create.called
    assert json.loads(patch.calls[0].request.content)["body"].endswith("new")
