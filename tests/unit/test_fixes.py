import subprocess

import pytest

from pr_review_agent.fixes import EditError, applied, changed_lines, plan_edits, real_bytes, unified_patch
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
        (e(file="backend/test/page.test.ts", old="it", new="xit"), "tests or test helpers"),
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


@pytest.mark.parametrize(
    ("file", "message"),
    [
        ("backend/tsconfig.json", "only change source files"),
        ("backend/package.json", "only change source files"),
        ("backend/vitest.config.ts", "build, lint or test configuration"),
        ("backend/eslint.config.mjs", "build, lint or test configuration"),
        ("backend/src/setupTests.ts", "build, lint or test configuration"),
        ("backend/src/vitest.setup.ts", "build, lint or test configuration"),
        ("backend/test/helpers.ts", "tests or test helpers"),
        ("backend/src/__mocks__/db.ts", "tests or test helpers"),
        ("backend/src/types.d.ts", "only change source files"),
        ("ai_service/pyproject.toml", "only change source files"),
        ("ai_service/tests/fakes.py", "tests or test helpers"),
        ("ai_service/conftest.py", "tests or test helpers"),
        ("ai_service/setup.py", "build, lint or test configuration"),
    ],
)
def test_fixes_may_only_change_source_files(root, repo_cfg, file, message):
    (root / file).parent.mkdir(parents=True, exist_ok=True)
    (root / file).write_text("strict = true\n")
    with pytest.raises(EditError, match=message):
        plan_edits(root, repo_cfg, [e(file=file, old="strict = true", new="strict = false")])


@pytest.mark.parametrize("file", ["backend/src/app/app.config.ts", "backend/src/db/setup.ts", "ai_service/app/api.py"])
def test_app_code_with_config_like_names_can_be_fixed(root, repo_cfg, file):
    (root / file).parent.mkdir(parents=True, exist_ok=True)
    (root / file).write_text("strict = true\n")
    assert plan_edits(root, repo_cfg, [e(file=file, old="strict = true", new="strict = false")])


def _git_apply_check(root, patch):
    (root / "fix.patch").write_text(patch, newline="")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "-c", "core.autocrlf=false", "apply", "--check", "fix.patch"], cwd=root, check=True)


def test_crlf_files_keep_their_line_endings_and_the_patch_applies(root, repo_cfg):
    path = root / "backend/src/win.ts"
    path.write_bytes(b"export function f(x: number) {\r\n  return x;\r\n}\r\n")
    # The agent copies code with \n line endings.
    changes = plan_edits(
        root,
        repo_cfg,
        [e(file="backend/src/win.ts", old="{\n  return x;", new="{\n  if (x < 0) return 0;\n  return x;")],
    )
    assert changes[0].patched == "export function f(x: number) {\r\n  if (x < 0) return 0;\r\n  return x;\r\n}\r\n"
    with applied(root, changes):
        assert b"\r\n  if (x < 0)" in path.read_bytes()
    assert path.read_bytes() == b"export function f(x: number) {\r\n  return x;\r\n}\r\n"  # restored exactly
    _git_apply_check(root, unified_patch(changes))


def test_patch_for_file_with_form_feed_applies(root, repo_cfg):
    # str.splitlines() treats \f as a line break; git doesn't.
    (root / "ai_service").mkdir()
    (root / "ai_service/app.py").write_text("x = 1\n\x0c\ny = 2\nz = 'a\x0cb'\n")
    changes = plan_edits(root, repo_cfg, [e(file="ai_service/app.py", old="y = 2", new="y = 3")])
    _git_apply_check(root, unified_patch(changes))


def test_non_utf8_file_is_an_edit_error(root, repo_cfg):
    (root / "backend/src/latin.ts").write_bytes(b"const s = '\xe9';\n")
    with pytest.raises(EditError, match="not UTF-8"):
        plan_edits(root, repo_cfg, [e(file="backend/src/latin.ts", old="const s", new="let s")])


def test_planning_during_another_fix_check_sees_the_real_code(root, repo_cfg):
    """Scan chunks share a checkout: chunk B planning its fix while chunk A's fix is applied must not
    take A's patched file for the original (restoring it would leave A's fix in the checkout)."""
    path = root / "backend/src/page.ts"
    a = plan_edits(root, repo_cfg, [e()])
    with applied(root, a):
        assert "Math.ceil" in path.read_text() and b"Math.floor" in real_bytes(path)
        b = plan_edits(root, repo_cfg, [e(old="total / size", new="total / Math.max(size, 1)")])
    assert b[0].original == SRC and "Math.ceil" not in b[0].patched
    with applied(root, b):
        pass
    assert path.read_text() == SRC


def test_applied_refuses_a_file_that_changed_after_planning(root, repo_cfg):
    changes = plan_edits(root, repo_cfg, [e()])
    (root / "backend/src/page.ts").write_text(SRC + "// edited\n")
    with pytest.raises(EditError, match="changed after the fix was planned"), applied(root, changes):
        pass
    assert (root / "backend/src/page.ts").read_text() == SRC + "// edited\n"  # left alone
