"""Reading local repositories for the dashboard, and applying verified fixes to them."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..adapters import adapter_for
from ..config import CONFIG_FILE, load_repo_config
from ..workspace import GitError, git, repo_slug


class RepoError(ValueError):
    """A problem the person can fix; the message is shown in the dashboard as is."""


def repo_root(path: str) -> Path:
    p = Path(path).expanduser()
    if not p.is_dir():
        raise RepoError(f"{path} doesn't exist or isn't a folder.")
    top = git(["rev-parse", "--show-toplevel"], cwd=p, check=False).strip()
    if not top:
        raise RepoError(f"{path} isn't inside a git repository.")
    return Path(top).resolve()


def repo_info(path: str) -> dict[str, Any]:
    root = repo_root(path)
    cfg = load_repo_config(root)
    projects = []
    for p in cfg.projects:
        adapter = adapter_for(p)
        note = None
        if not p.enabled:
            note = f"{p.language} isn't supported yet"
        elif not adapter.can_run_tests(p):
            note = "no test runner, so bugs can't be proven with a test"
        projects.append({"name": p.name, "path": p.path, "language": p.language, "enabled": p.enabled, "note": note})
    branches = git(["for-each-ref", "--format=%(refname:short)", "refs/heads"], cwd=root).split()
    current = git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root, check=False).strip()
    dirty = bool(git(["status", "--porcelain", "--untracked-files=normal"], cwd=root, check=False).strip())
    return {
        "path": str(root),
        "name": root.name,
        "slug": repo_slug(root),
        "configured": (root / CONFIG_FILE).exists(),
        "projects": projects,
        "branches": branches,
        "current_branch": current,
        "default_base": next(
            (b for b in ("main", "master", "develop") if b in branches), branches[0] if branches else ""
        ),
        "has_uncommitted": dirty,
    }


def ref_exists(root: Path, ref: str) -> bool:
    return (
        bool(ref)
        and not ref.startswith("-")
        and bool(git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=root, check=False).strip())
    )


def patch_files(patch: str) -> list[str]:
    return re.findall(r"^diff --git a/(\S+) b/", patch, re.MULTILINE)


def fix_state(path: str, patch: str) -> dict[str, Any]:
    """Would the patch apply to the working copy right now?"""
    root = repo_root(path)
    files = patch_files(patch)
    try:
        git(["apply", "--check", "-"], cwd=root, input=patch)
        return {
            "state": "applies",
            "files": files,
            "repo": str(root),
            "detail": f"The patch applies cleanly to {len(files)} file(s) in your working copy.",
        }
    except GitError as forward:
        try:
            git(["apply", "--check", "--reverse", "-"], cwd=root, input=patch)
            return {
                "state": "applied",
                "files": files,
                "repo": str(root),
                "detail": "This fix is already applied in your working copy.",
            }
        except GitError:
            reason = str(forward).split("failed:", 1)[-1].strip()
            return {
                "state": "conflict",
                "files": files,
                "repo": str(root),
                "detail": "The code changed since this run, so the patch no longer applies. "
                f"Run a new scan, or apply it by hand. Git said: {reason[:400]}",
            }


def apply_fix(path: str, patch: str) -> dict[str, Any]:
    state = fix_state(path, patch)
    if state["state"] != "applies":
        raise RepoError(state["detail"])
    git(["apply", "-"], cwd=Path(state["repo"]), input=patch)
    return {
        **state,
        "state": "applied",
        "detail": f"Applied to {', '.join(state['files'])}. Review it with `git diff`; nothing was committed.",
    }


def undo_fix(path: str, patch: str) -> dict[str, Any]:
    state = fix_state(path, patch)
    if state["state"] != "applied":
        raise RepoError("The fix isn't applied in your working copy, so there's nothing to undo.")
    git(["apply", "--reverse", "-"], cwd=Path(state["repo"]), input=patch)
    return {**state, "state": "applies", "detail": "The fix was removed from your working copy."}
