"""GitHub I/O. Only our orchestrator talks to GitHub; the agent never gets a token or GitHub tools."""

from __future__ import annotations

import re
from dataclasses import dataclass

from githubkit import GitHub
from githubkit.exception import RequestFailed

from .render import MARKER_PREFIX, SUMMARY_MARKER, parse_marker

_TARGET_RE = re.compile(r"^(?:https://github\.com/)?(?P<owner>[\w.-]+)/(?P<repo>[\w.-]+)(?:#|/pulls?/)(?P<num>\d+)/?$")


def parse_target(target: str) -> tuple[str, str, int]:
    """`owner/repo#12` or `https://github.com/owner/repo/pull/12` -> (owner, repo, 12)."""
    m = _TARGET_RE.match(target.strip())
    if not m:
        raise ValueError(f"expected owner/repo#123 or a PR URL, got {target!r}")
    return m["owner"], m["repo"], int(m["num"])


@dataclass
class PullRequest:
    owner: str
    repo: str
    number: int
    title: str
    body: str | None
    author: str
    head_sha: str
    base_sha: str
    base_ref: str
    head_repo: str | None  # full name; differs from owner/repo for forks
    draft: bool
    url: str

    @property
    def label(self) -> str:
        if self.number:
            return f"{self.owner}/{self.repo}#{self.number}"
        return f"{self.repo} ({self.base_ref}...{self.head_sha[:7]})"

    @property
    def is_fork(self) -> bool:
        return self.head_repo is not None and self.head_repo.lower() != f"{self.owner}/{self.repo}".lower()


@dataclass
class ExistingComment:
    path: str
    line: int | None
    fingerprint: str | None
    category: str | None


class GitHubClient:
    def __init__(self, token: str | None):
        self.gh = GitHub(token) if token else GitHub()

    async def get_pr(self, owner: str, repo: str, number: int) -> PullRequest:
        pr = (await self.gh.rest.pulls.async_get(owner, repo, number)).parsed_data
        return PullRequest(
            owner=owner,
            repo=repo,
            number=number,
            title=pr.title,
            body=pr.body,
            author=pr.user.login if pr.user else "unknown",
            head_sha=pr.head.sha,
            base_sha=pr.base.sha,
            base_ref=pr.base.ref,
            head_repo=pr.head.repo.full_name if pr.head.repo else None,
            draft=bool(pr.draft),
            url=pr.html_url,
        )

    async def existing_comments(self, pr: PullRequest) -> list[ExistingComment]:
        """Review comments this tool posted earlier on the PR (identified by their hidden marker)."""
        out = []
        async for c in self.gh.rest.paginate(
            self.gh.rest.pulls.async_list_review_comments,
            owner=pr.owner,
            repo=pr.repo,
            pull_number=pr.number,
            per_page=100,
        ):
            if MARKER_PREFIX not in (c.body or ""):
                continue
            parsed = parse_marker(c.body)
            line = (
                c.line if isinstance(c.line, int) else (c.original_line if isinstance(c.original_line, int) else None)
            )
            out.append(
                ExistingComment(
                    path=c.path,
                    line=line,
                    fingerprint=parsed[0] if parsed else None,
                    category=parsed[1] if parsed else None,
                )
            )
        return out

    async def create_review(self, pr: PullRequest, body: str, comments: list[dict]) -> str:
        data = {"commit_id": pr.head_sha, "body": body, "event": "COMMENT", "comments": comments}
        try:
            resp = await self.gh.rest.pulls.async_create_review(pr.owner, pr.repo, pr.number, data=data)
        except RequestFailed as e:
            if e.response.status_code != 422 or not comments:
                raise
            # A line GitHub doesn't consider part of the diff; fall back to one body-only review.
            extra = "\n\n---\n\n".join(c["body"] for c in comments)
            data = {**data, "comments": [], "body": body + "\n\n" + extra}
            resp = await self.gh.rest.pulls.async_create_review(pr.owner, pr.repo, pr.number, data=data)
        return resp.parsed_data.html_url

    async def upsert_summary(self, pr: PullRequest, body: str) -> str:
        """Create or update the single sticky summary comment."""
        async for c in self.gh.rest.paginate(
            self.gh.rest.issues.async_list_comments,
            owner=pr.owner,
            repo=pr.repo,
            issue_number=pr.number,
            per_page=100,
        ):
            if SUMMARY_MARKER in (c.body or ""):
                resp = await self.gh.rest.issues.async_update_comment(pr.owner, pr.repo, c.id, data={"body": body})
                return resp.parsed_data.html_url
        resp = await self.gh.rest.issues.async_create_comment(pr.owner, pr.repo, pr.number, data={"body": body})
        return resp.parsed_data.html_url
