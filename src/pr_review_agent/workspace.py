"""Throwaway checkouts the agent and the sandbox work in.

Working from fresh checkouts (never your working copy) means untracked, git-ignored secrets such as
`.env` files or `firebase-service-account.json` simply don't exist where the agent can look.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(
    args: list[str], cwd: Path | None = None, token: str | None = None, check: bool = True, input: str | None = None
) -> str:
    cmd = ["git"]
    if token:
        # Same approach as actions/checkout: the token is passed per command, never written to disk.
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        cmd += ["-c", f"http.https://github.com/.extraheader=AUTHORIZATION: basic {basic}"]
    cmd += ["-c", "credential.helper=", "-c", "core.hooksPath=/dev/null", *args]
    proc = subprocess.run(
        cmd,
        input=input,
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env={"GIT_TERMINAL_PROMPT": "0", "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {proc.stderr.strip()[-2000:]}")
    return proc.stdout


@dataclass
class Workspace:
    root: Path  # temp dir holding the checkouts
    head: Path
    head_sha: str
    base: Path | None = None
    base_sha: str | None = None  # merge base, i.e. what the PR diff is computed against
    _mirror: Path | None = None

    def diff(self) -> str:
        if not self.base_sha:
            raise ValueError("workspace has no base")
        return git(["diff", "--no-color", "--no-ext-diff", "-U3", "-M", self.base_sha, self.head_sha], cwd=self.head)

    def tracked_files(self) -> list[str]:
        return [f for f in git(["ls-files", "-z"], cwd=self.head).split("\0") if f]

    def cleanup(self) -> None:
        if self._mirror:
            for wt in (self.head, self.base):
                if wt:
                    git(["worktree", "remove", "--force", str(wt)], cwd=self._mirror, check=False)
            git(["worktree", "prune"], cwd=self._mirror, check=False)
        shutil.rmtree(self.root, ignore_errors=True)


def _new_root(cache_dir: Path) -> Path:
    root = cache_dir / "work" / uuid.uuid4().hex[:12]
    root.mkdir(parents=True)
    return root


def prepare_pr_workspace(
    owner: str, repo: str, number: int, head_sha: str, base_sha: str, token: str | None, cache_dir: Path
) -> Workspace:
    """Bare partial clone cached per repo + one worktree for head and one for the merge base."""
    mirror = cache_dir / "repos" / f"{owner}__{repo}.git"
    url = f"https://github.com/{owner}/{repo}.git"
    if not (mirror / "HEAD").exists():
        mirror.parent.mkdir(parents=True, exist_ok=True)
        git(["clone", "--bare", "--filter=blob:none", url, str(mirror)], token=token)
    git(
        ["fetch", "--filter=blob:none", "origin", f"+refs/pull/{number}/head:refs/pr/{number}", base_sha],
        cwd=mirror,
        token=token,
    )
    merge_base = git(["merge-base", base_sha, head_sha], cwd=mirror).strip()
    root = _new_root(cache_dir)
    ws = Workspace(
        root=root, head=root / "head", head_sha=head_sha, base=root / "base", base_sha=merge_base, _mirror=mirror
    )
    try:
        git(["worktree", "add", "--detach", str(ws.head), head_sha], cwd=mirror, token=token)
        git(["worktree", "add", "--detach", str(ws.base), merge_base], cwd=mirror, token=token)
    except GitError:
        ws.cleanup()
        raise
    return ws


def prepare_local_pair(repo_path: Path, cache_dir: Path, base_ref: str, head_ref: str) -> Workspace:
    """Head + merge-base worktrees for two refs of a local repo (committed state only)."""
    repo_path = repo_path.resolve()
    head_sha = git(["rev-parse", head_ref], cwd=repo_path).strip()
    base_sha = git(["merge-base", base_ref, head_ref], cwd=repo_path).strip()
    root = _new_root(cache_dir)
    mirror = root / "repo.git"
    git(["clone", "--quiet", "--bare", "--local", str(repo_path), str(mirror)])
    ws = Workspace(
        root=root, head=root / "head", head_sha=head_sha, base=root / "base", base_sha=base_sha, _mirror=mirror
    )
    try:
        git(["worktree", "add", "--detach", str(ws.head), head_sha], cwd=mirror)
        git(["worktree", "add", "--detach", str(ws.base), base_sha], cwd=mirror)
    except GitError:
        ws.cleanup()
        raise
    return ws


def prepare_local_workspace(
    repo_path: Path, cache_dir: Path, ref: str = "HEAD", include_uncommitted: bool = False
) -> Workspace:
    """Snapshot of a local repo at `ref`. With `include_uncommitted`, tracked changes and untracked
    files are copied in too — but never git-ignored files, so ignored secrets stay out."""
    repo_path = repo_path.resolve()
    sha = git(["rev-parse", ref], cwd=repo_path).strip()
    root = _new_root(cache_dir)
    head = root / "head"
    git(["clone", "--quiet", "--local", "--no-checkout", str(repo_path), str(head)])
    git(["checkout", "--quiet", "--detach", sha], cwd=head)
    if include_uncommitted:
        changed = git(["diff", "--name-only", "-z", "HEAD"], cwd=repo_path).split("\0")
        untracked = git(["ls-files", "--others", "--exclude-standard", "-z"], cwd=repo_path).split("\0")
        for rel in filter(None, changed + untracked):
            src, dst = repo_path / rel, head / rel
            if src.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
            elif not src.exists():
                dst.unlink(missing_ok=True)  # deleted in the working tree
    return Workspace(root=root, head=head, head_sha=sha)


def repo_slug(repo_path: Path) -> str | None:
    """owner/repo from the local repo's GitHub remote, if any."""
    url = git(["remote", "get-url", "origin"], cwd=repo_path, check=False).strip()
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        if url.startswith(prefix):
            return url[len(prefix) :].removesuffix(".git")
    return None
