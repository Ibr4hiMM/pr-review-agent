"""GitHub lookups for the dashboard's pull request picker, using your gh login or GITHUB_TOKEN."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from githubkit import GitHub
from githubkit.exception import RequestFailed

from ..config import github_token
from .repos import RepoError

_SLUG = re.compile(r"^[\w.-]+/[\w.-]+$")


def _client() -> GitHub:
    token = github_token()
    if not token:
        raise RepoError("You're not signed in to GitHub. Run gh auth login in a terminal, then try again.")
    return GitHub(token)


def _explain(e: RequestFailed, what: str) -> RepoError:
    code = e.response.status_code
    if code == 404:
        return RepoError(f"Can't find {what}, or your GitHub login can't see it.")
    if code in (401, 403):
        return RepoError("GitHub refused the request. Sign in again with gh auth login.")
    return RepoError(f"GitHub returned an error ({code}). Try again in a minute.")


def list_repos() -> list[dict[str, Any]]:
    """Repositories you can see, most recently pushed first."""

    async def go():
        gh = _client()
        resp = await gh.rest.repos.async_list_for_authenticated_user(sort="pushed", per_page=100)
        # Raw JSON: only a few fields are needed, and it doesn't break if GitHub's schema changes.
        return [
            {"slug": r["full_name"], "private": bool(r.get("private")), "pushed_at": r.get("pushed_at") or ""}
            for r in resp.json()
        ]

    try:
        return asyncio.run(go())
    except RequestFailed as e:
        raise _explain(e, "your repositories") from e


def list_prs(slug: str, state: str = "open") -> list[dict[str, Any]]:
    if not _SLUG.match(slug or ""):
        raise RepoError("Enter the repository as owner/name, for example octocat/hello-world.")
    if state not in ("open", "all"):
        state = "open"
    owner, repo = slug.split("/")

    async def go():
        gh = _client()
        resp = await gh.rest.pulls.async_list(owner, repo, state=state, sort="updated", direction="desc", per_page=40)
        return [
            {
                "number": pr["number"],
                "title": pr.get("title") or "",
                "state": "merged" if pr.get("merged_at") else pr.get("state", "open"),
                "draft": bool(pr.get("draft")),
                "author": (pr.get("user") or {}).get("login", ""),
                "updated_at": pr.get("updated_at") or "",
                "head": (pr.get("head") or {}).get("ref", ""),
                "base": (pr.get("base") or {}).get("ref", ""),
                "url": pr.get("html_url", ""),
            }
            for pr in resp.json()
        ]

    try:
        return asyncio.run(go())
    except RequestFailed as e:
        raise _explain(e, slug) from e
