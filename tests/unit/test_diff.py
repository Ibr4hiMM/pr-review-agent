from pr_review_agent.diff import parse_diff

DIFF = """\
diff --git a/backend/src/routes.ts b/backend/src/routes.ts
index 1111111..2222222 100644
--- a/backend/src/routes.ts
+++ b/backend/src/routes.ts
@@ -10,6 +10,7 @@ router.get('/a', h);
 line10
 line11
-old12
+new12
+new13
 line14
 line15
 line16
diff --git a/backend/src/new.ts b/backend/src/new.ts
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/backend/src/new.ts
@@ -0,0 +1,2 @@
+export const a = 1;
+export const b = 2;
diff --git a/old.ts b/old.ts
deleted file mode 100644
index 4444444..0000000
--- a/old.ts
+++ /dev/null
@@ -1 +0,0 @@
-gone
"""


def test_parse_diff_lines_and_statuses():
    d = parse_diff(DIFF)
    routes = d.get("backend/src/routes.ts")
    assert routes.status == "modified"
    assert routes.added_lines == {12, 13}
    assert routes.commentable_lines == set(range(10, 17))
    new = d.get("backend/src/new.ts")
    assert new.status == "added" and new.added_lines == {1, 2}
    assert d.get("old.ts").status == "removed"
    assert "old.ts" not in d.changed_lines()


def test_touches_and_filter_and_render():
    d = parse_diff(DIFF)
    assert d.touches("backend/src/routes.ts", 12, 12)
    assert not d.touches("backend/src/routes.ts", 20, 25)
    assert d.touches("backend/src/routes.ts", 14, 15, slack=2)
    only_new = d.filtered(lambda p: p.endswith("new.ts"))
    assert [f.path for f in only_new.files] == ["backend/src/new.ts"]
    rendered = d.render(max_chars=300)
    assert "Diff truncated" in rendered
