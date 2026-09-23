import subprocess

import pytest

from pr_review_agent.fixes import EditError, applied, changed_lines, plan_edits, unified_patch
from pr_review_agent.models import FixEdit

SRC = "export function pageCount(total: number, size: number) {\n  return Math.floor(total / size);\n}\n"


@pytest.fixture
def root(tmp_path):
    (tmp_path / "backend/src").mkdir(parents=True)
    (tmp_path / "backend/test").mkdir()
    (tmp_path / "backend/src/page.ts").write_text(SRC)
    (tmp_path / "backend/src/dup.ts").write_text("x = 1;\nx = 1;\n")
    (tmp_path / "backend/test/page.test.ts").write_text("it('x', () => {});\n")
    return tmp_path


def e(file="backend/src/page.ts", old="Math.floor", new="Math.ceil"):
    return FixEdit(file=file, old=old, new=new)


def test_plan_and_patch_apply_with_git(root, repo_cfg):
    changes = plan_edits(root, repo_cfg, [e()])
    patch = unified_patch(changes)
    assert patch.startswith("diff --git a/backend/src/page.ts b/backend/src/page.ts\n--- a/backend/src/page.ts\n")
    assert "-  return Math.floor(total / size);\n+  return Math.ceil(total / size);\n" in patch
    assert changed_lines(changes) == {"backend/src/page.ts": {2}}
    # the patch really applies from the repo root
    (root / "fix.patch").write_text(patch)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "apply", "--check", "fix.patch"], cwd=root, check=True)


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (e(old="Math.round"), "not found"),
        (e(file="backend/src/dup.ts", old="x = 1;", new="x = 2;"), "found 2 times"),
        (e(file="backend/test/page.test.ts", old="it", new="xit"), "test files"),
        (e(file="../outside.ts"), "invalid path"),
        (e(file="backend/src/missing.ts"), "does not exist"),
        (e(file="backend/node_modules/x.ts"), "ignored"),
        (e(file="lib/main.dart"), "not in an enabled project"),
    ],
)
def test_invalid_edits_are_rejected(root, repo_cfg, edit, message):
    with pytest.raises(EditError, match=message):
        plan_edits(root, repo_cfg, [edit])


def test_sequential_edits_on_one_file_and_restore(root, repo_cfg):
    changes = plan_edits(
        root, repo_cfg, [e(), e(old="Math.ceil(total / size)", new="Math.ceil(total / Math.max(size, 1))")]
    )
    assert len(changes) == 1 and "Math.max(size, 1)" in changes[0].patched
    with applied(root, changes):
        assert "Math.max" in (root / "backend/src/page.ts").read_text()
    assert (root / "backend/src/page.ts").read_text() == SRC
    with pytest.raises(RuntimeError), applied(root, changes):
        raise RuntimeError("test run crashed")
    assert (root / "backend/src/page.ts").read_text() == SRC  # restored even on errors
