// Shared by the dashboard's Overview and the public site: the proof animation (a real bug being
// marked, proven by a failing test, fixed, and re-checked), the pipeline explainer, and count-ups.
// All content is inserted as text. Motion stops entirely under prefers-reduced-motion.
(function () {
  "use strict";

  const reduced = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [key, value] of Object.entries(attrs || {})) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") node.className = value;
      else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
      else node.setAttribute(key, value === true ? "" : value);
    }
    for (const child of children.flat(Infinity)) {
      if (child === null || child === undefined || child === false) continue;
      node.append(child instanceof Node ? child : document.createTextNode(String(child)));
    }
    return node;
  }

  const SVG = "http://www.w3.org/2000/svg";
  function svg(tag, attrs) {
    const node = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs || {})) node.setAttribute(k, v);
    return node;
  }

  // ---------- the example used on the public site (a seeded bug in a small test repository) ----------

  const DEMO = {
    source: [
      "export type Role = 'customer' | 'staff' | 'admin' | 'owner';",
      "",
      "export interface User {",
      "  id: string;",
      "  role: Role;",
      "}",
      "",
      "const REFUND_ROLES: Role[] = ['admin', 'owner', 'staff'];",
      "",
      "export function canRefund(user: User): boolean {",
      "  return REFUND_ROLES.includes(user.role) || user.role !== 'staff';",
      "}",
      "",
    ].join("\n"),
    finding: {
      tier: "verified",
      finding: {
        title: "canRefund lets every role issue refunds, including customers",
        severity: "critical",
        file: "shop/src/access.ts",
        line_start: 10,
        line_end: 12,
      },
      evidence: [
        {
          kind: "failing_test",
          test_code: "it('denies refunds to customers', () => {\n  expect(canRefund({ id: 'u1', role: 'customer' })).toBe(false);\n});",
          test_output: "canRefund denies refunds to customers\nAssertionError: expected true to be false",
        },
      ],
      fix: {
        status: "verified",
        patch: [
          "diff --git a/shop/src/access.ts b/shop/src/access.ts",
          "--- a/shop/src/access.ts",
          "+++ b/shop/src/access.ts",
          "@@ -8,5 +8,5 @@",
          " const REFUND_ROLES: Role[] = ['admin', 'owner', 'staff'];",
          " ",
          " export function canRefund(user: User): boolean {",
          "-  return REFUND_ROLES.includes(user.role) || user.role !== 'staff';",
          "+  return REFUND_ROLES.includes(user.role);",
          " }",
          "",
        ].join("\n"),
        notes: ["repro test 1 passes with the fix", "existing tests still pass (2 passed)", "no new static-check diagnostics"],
      },
    },
  };

  // ---------- story: what the proof animation plays ----------

  function testName(code) {
    const m = String(code || "").match(/\b(?:it|test)\(\s*['"`]([^'"`]+)/);
    return m ? m[1] : "the agent's test";
  }

  function failureLine(output) {
    const lines = String(output || "").split("\n").map((l) => l.trim()).filter(Boolean);
    const line = lines.find((l) => /error|expected|assert/i.test(l)) || lines[1] || lines[0] || "";
    return line.replace(/\s*\/\/ Object\.is equality$/, "").replace(/^AssertionError:\s*/, "");
  }

  // Old-file line numbers the patch removes, and new lines keyed by the old line they follow.
  function parseFix(patch, file) {
    const removed = new Set();
    const added = new Map();
    let inFile = false;
    let inHunk = false;
    let old = 0;
    let anchor = 0;
    for (const line of String(patch || "").split("\n")) {
      if (line.startsWith("diff --git")) {
        inFile = line.endsWith(` b/${file}`);
        inHunk = false;
        continue;
      }
      if (!inFile) continue;
      const m = line.match(/^@@ -(\d+)(?:,\d+)? \+\d+(?:,\d+)? @@/);
      if (m) {
        old = Number(m[1]);
        anchor = old - 1;
        inHunk = true;
        continue;
      }
      if (!inHunk || line.startsWith("\\")) continue;
      if (line.startsWith("-")) {
        removed.add(old);
        anchor = old;
        old += 1;
      } else if (line.startsWith("+")) {
        if (!added.has(anchor)) added.set(anchor, []);
        added.get(anchor).push(line.slice(1));
      } else {
        anchor = old;
        old += 1;
      }
    }
    return { removed, added };
  }

  function storyFromFinding(v, source, meta) {
    const f = v.finding;
    const all = String(source || "").replace(/\n$/, "").split("\n");
    const fix = v.fix && v.fix.status === "verified" ? parseFix(v.fix.patch, f.file) : { removed: new Set(), added: new Map() };
    // For a long finding, mark just the lines the fix changes; that's where the bug is.
    let bug = [f.line_start, f.line_end];
    if (bug[1] - bug[0] > 4 && fix.removed.size) bug = [Math.min(...fix.removed), Math.max(...fix.removed)];
    const touched = [...bug, ...fix.removed, ...[...fix.added.keys()].map((n) => n + 1)];
    let lo = Math.max(1, Math.min(...touched) - 3);
    let hi = Math.min(all.length, Math.max(...touched) + 2);
    if (hi - lo > 13) hi = lo + 13;
    const lines = [];
    for (let n = lo; n <= hi; n++) lines.push({ n, text: all[n - 1] ?? "" });
    const test = v.evidence.find((e) => e.kind === "failing_test");
    const regression = v.evidence.find((e) => e.kind === "test_regression");
    const suiteNote = (v.fix?.notes || []).find((n) => n.startsWith("existing tests still pass")) || "";
    const suiteCount = (suiteNote.match(/\((\d+) passed\)/) || [])[1];
    const checks = [];
    if (v.fix && v.fix.status === "verified") {
      checks.push({ key: "rerun", text: test ? "The same test passes with the fix" : "The failing test passes again" });
      checks.push({ key: "suite", text: suiteCount ? `All ${suiteCount} existing tests still pass` : "Existing tests still pass" });
      if ((v.fix.notes || []).some((n) => n.startsWith("no new static-check"))) {
        checks.push({ key: "static", text: "No new compiler or lint errors" });
      }
    }
    return {
      file: f.file,
      title: f.title,
      severity: f.severity,
      lines,
      bug,
      removed: fix.removed,
      added: fix.added,
      test: {
        name: test ? testName(test.test_code) : regression ? regression.test_id.split("::").pop() : "",
        failure: failureLine(test ? test.test_output : regression ? regression.test_output : ""),
      },
      checks,
      meta: meta || {},
    };
  }

  // ---------- proof animation ----------

  const CHECK = "✓";
  const CROSS = "✗";

  function mountProof(root, story, opts = {}) {
    const codeLines = [];
    const byNumber = new Map();
    for (const line of story.lines) {
      const cls = ["pl"];
      if (line.n >= story.bug[0] && line.n <= story.bug[1]) cls.push("is-bug");
      if (story.removed.has(line.n)) cls.push("is-removed");
      const row = el("div", { class: cls.join(" "), "data-n": line.n },
        el("span", { class: "pn", "aria-hidden": "true" }, line.n), el("span", { class: "pt" }, line.text || " "));
      codeLines.push(row);
      byNumber.set(line.n, row);
      for (const text of story.added.get(line.n) || []) {
        codeLines.push(el("div", { class: "pl is-added" },
          el("span", { class: "pn", "aria-hidden": "true" }, "+"), el("span", { class: "pt" }, text || " ")));
      }
    }
    const pen = svg("svg", { class: "pen", "aria-hidden": "true", preserveAspectRatio: "none" });
    const penPath = svg("path", { fill: "none", "stroke-linecap": "round", "stroke-linejoin": "round" });
    pen.append(penPath);
    const sweep = el("div", { class: "proof-sweep", "aria-hidden": "true" });
    sweep.style.setProperty("--sweep", String(Math.max(1, story.lines.length - 1)));
    const body = el("div", { class: "proof-body" }, sweep, pen, el("div", { class: "proof-lines" }, codeLines));
    const status = el("span", { class: "proof-status" }, "");

    const rows = {};
    const row = (key, label, detail) => {
      rows[key] = el("li", { class: "pt-row", "data-key": key },
        el("span", { class: "pt-mark", "aria-hidden": "true" }),
        el("span", { class: "pt-text" }, el("span", { class: "pt-label" }, label), detail ? el("span", { class: "pt-detail" }, detail) : null));
      return rows[key];
    };
    const testRows = [row("first", story.test.name ? `Test: “${story.test.name}”` : "Test written by the agent", story.test.failure)];
    for (const c of story.checks) testRows.push(row(c.key, c.text));
    const verdict = el("p", { class: "verdict" }, "Verified");
    const panel = el("div", { class: "proof-tests" },
      el("p", { class: "proof-panel-title" }, "Proof"),
      el("ol", { class: "pt-list" }, testRows),
      verdict);

    const toggle = el("button", { class: "proof-toggle", type: "button" }, "Pause");
    const figure = el("figure", { class: "proof", "aria-label": `Animation: ${story.title}. The bug is marked, a test proves it fails, the fix is applied, and the test passes again.` },
      el("div", { class: "proof-card" },
        el("div", { class: "proof-code" },
          el("div", { class: "proof-head" }, el("span", { class: "proof-file" }, story.file), status),
          body),
        panel),
      el("figcaption", { class: "proof-caption" },
        el("span", {}, opts.caption || ""),
        toggle));
    root.replaceChildren(figure);

    const drawPen = () => {
      const bugRows = story.lines.filter((l) => l.n >= story.bug[0] && l.n <= story.bug[1]).map((l) => byNumber.get(l.n)).filter(Boolean);
      if (!bugRows.length) return;
      const top = bugRows[0].offsetTop + 2;
      const bottom = bugRows[bugRows.length - 1].offsetTop + bugRows[bugRows.length - 1].offsetHeight - 2;
      const h = Math.max(8, bottom - top);
      pen.setAttribute("width", "16");
      pen.setAttribute("height", String(h));
      pen.setAttribute("viewBox", `0 0 16 ${h}`);
      pen.style.top = `${top}px`;
      pen.style.height = `${h}px`;
      // A slightly uneven stroke, like a pen: in, down, out.
      penPath.setAttribute("d", `M 14 1.5 C 9 1, 6 2, 5.5 5 L 5 ${h - 5} C 5.5 ${h - 2}, 9 ${h - 1}, 14 ${h - 1.5}`);
      const len = penPath.getTotalLength();
      penPath.style.strokeDasharray = `${len}`;
      penPath.style.setProperty("--len", `${len}`);
    };
    requestAnimationFrame(drawPen);
    const resizer = typeof ResizeObserver === "function" ? new ResizeObserver(drawPen) : null;
    resizer?.observe(body);

    const STEPS = ["is-reading", "is-marked", "is-testing", "is-failed", "is-fixing", "is-fixed", "is-rerun", "is-verified"];
    const setState = (state, text) => {
      figure.classList.remove(...STEPS);
      const upto = STEPS.indexOf(state);
      figure.classList.add(...STEPS.slice(0, upto + 1));
      status.textContent = text;
    };
    const setRow = (key, rowState) => {
      const r = rows[key];
      if (!r) return;
      r.dataset.state = rowState;
      r.querySelector(".pt-mark").textContent = rowState === "pass" ? CHECK : rowState === "fail" ? CROSS : "";
    };
    const showFinal = () => {
      setState("is-verified", story.checks.length ? "Verified" : "Proven");
      setRow("first", "fail");
      for (const c of story.checks) setRow(c.key, "pass");
      if (!story.checks.length) verdict.textContent = "Proven";
    };

    let playing = !reduced();
    let token = 0;
    let visible = true;

    async function play() {
      const mine = ++token;
      const alive = () => mine === token && playing;
      const wait = async (ms) => {
        await sleep(ms);
        while (mine === token && playing && (!visible || document.hidden)) await sleep(250);
        return alive();
      };
      while (alive()) {
        figure.classList.remove("is-fading");
        Object.keys(rows).forEach((k) => setRow(k, "idle"));
        setState("is-reading", `Reading ${story.file.split("/").pop()}`);
        if (!(await wait(1500))) return;
        setState("is-marked", "Suspected bug");
        if (!(await wait(1300))) return;
        setState("is-testing", "Writing a test");
        setRow("first", "running");
        if (!(await wait(1300))) return;
        setState("is-failed", "Proven: the test fails");
        setRow("first", "fail");
        if (!story.checks.length) {
          if (!(await wait(3500))) return;
        } else {
          if (!(await wait(1500))) return;
          setState("is-fixing", "Fix proposed");
          if (!(await wait(900))) return;
          setState("is-fixed", "Fix applied in the sandbox");
          if (!(await wait(900))) return;
          setState("is-rerun", "Re-running the checks");
          for (const c of story.checks) {
            setRow(c.key, "running");
            if (!(await wait(800))) return;
            setRow(c.key, "pass");
            if (!(await wait(250))) return;
          }
          setState("is-verified", "Verified");
          if (!(await wait(3600))) return;
        }
        figure.classList.add("is-fading");
        if (!(await wait(600))) return;
      }
    }

    const setPlaying = (on) => {
      playing = on;
      toggle.textContent = on ? "Pause" : "Play";
      if (on) play();
      else {
        token++;
        figure.classList.remove("is-fading");
        showFinal();
      }
    };
    toggle.addEventListener("click", () => setPlaying(!playing));

    const io = typeof IntersectionObserver === "function"
      ? new IntersectionObserver((entries) => { visible = entries.some((e) => e.isIntersecting); }, { threshold: 0.2 })
      : null;
    io?.observe(figure);

    if (reduced()) {
      toggle.hidden = true;
      showFinal();
    } else {
      play();
    }
    return {
      replay: () => setPlaying(true),
      destroy: () => { token++; playing = false; io?.disconnect(); resizer?.disconnect(); },
    };
  }

  // ---------- pipeline explainer ----------

  const STAGES = [
    ["Your code", "A pull request, a branch, or a whole repository."],
    ["Checks and tests", "Compiler, linters and your test suite run first."],
    ["Claude investigates", "Reads the code and its callers, and writes a test for each suspicion."],
    ["Sandbox verifies", "Every test is re-run offline. Suspicions it can't prove are dropped."],
    ["Proven bugs", "Each comes with its failing test and a fix that passed your suite."],
  ];

  function mountPipeline(root) {
    const vertical = window.matchMedia("(max-width: 720px)").matches;
    const nodes = STAGES.map(([title, text], i) =>
      el("li", { class: "stage", "data-i": i },
        el("span", { class: "stage-node", "aria-hidden": "true" }),
        el("span", { class: "stage-title" }, title),
        el("span", { class: "stage-text" }, text)));
    const flow = el("div", { class: "pipe-flow", "aria-hidden": "true" });
    const legend = el("p", { class: "pipe-legend", "aria-hidden": "true" },
      el("span", { class: "lg lg-suspect" }, "suspicion"),
      el("span", { class: "lg lg-drop" }, "dropped"),
      el("span", { class: "lg lg-bug" }, "proven bug"),
      el("span", { class: "lg lg-fix" }, "verified fix"));
    const wrap = el("div", { class: `pipe${vertical ? " is-vertical" : ""}` }, el("ol", { class: "pipe-stages" }, nodes), flow, legend);
    root.replaceChildren(wrap);
    if (reduced()) return { destroy() {} };

    const css = getComputedStyle(document.documentElement);
    const color = (name) => css.getPropertyValue(name).trim();
    const ink = color("--ink");
    const amber = color("--amber");
    const red = color("--red");
    const green = color("--green");
    const muted = color("--muted");
    const pos = [10, 30, 50, 70, 90];
    const axis = vertical ? "top" : "left";
    const cross = vertical ? "left" : "top";
    const hit = (i) => {
      const n = nodes[i];
      n.classList.remove("is-hit");
      void n.offsetWidth;
      n.classList.add("is-hit");
    };
    let running = true;
    let visible = false;
    let seed = 7;
    const rand = () => ((seed = (seed * 16807) % 2147483647) / 2147483647);

    const spawn = () => {
      const dot = el("span", { class: "dot" });
      flow.append(dot);
      const r = rand();
      const fate = r < 0.45 ? "drop" : r < 0.75 ? "fix" : "bug";
      const at = (p, extra = {}) => ({ [axis]: `${p}%`, [cross]: "50%", ...extra });
      const frames = [
        at(pos[0], { backgroundColor: ink, opacity: 0, offset: 0 }),
        at(pos[0] + 2, { backgroundColor: ink, opacity: 1, offset: 0.04 }),
        at(pos[1], { backgroundColor: ink, offset: 0.22 }),
        at(pos[2], { backgroundColor: ink, offset: 0.42 }),
        at(pos[2] + 2, { backgroundColor: amber, offset: 0.46 }),
        at(pos[3], { backgroundColor: amber, offset: 0.64 }),
      ];
      if (fate === "drop") {
        frames.push({ [axis]: `${pos[3] + 3}%`, [cross]: vertical ? "85%" : "92%", backgroundColor: muted, opacity: 0, offset: 1 });
      } else {
        frames.push(at(pos[3] + 2, { backgroundColor: red, offset: 0.68 }));
        frames.push(at(pos[4], { backgroundColor: red, offset: 0.9 }));
        frames.push(at(pos[4], { backgroundColor: fate === "fix" ? green : red, opacity: 0.95, offset: 0.94 }));
        frames.push(at(pos[4], { backgroundColor: fate === "fix" ? green : red, opacity: 0, offset: 1 }));
      }
      const duration = 5200;
      const anim = dot.animate(frames, { duration, easing: "linear", fill: "forwards" });
      [0.04, 0.22, 0.42, 0.64].forEach((o, i) => setTimeout(() => running && hit(i), o * duration));
      if (fate !== "drop") setTimeout(() => running && hit(4), 0.9 * duration);
      anim.finished.then(() => dot.remove()).catch(() => dot.remove());
    };
    const loop = async () => {
      while (running) {
        if (visible && !document.hidden && flow.childElementCount < 14) spawn();
        await sleep(700);
      }
    };
    const io = typeof IntersectionObserver === "function"
      ? new IntersectionObserver((entries) => { visible = entries.some((e) => e.isIntersecting); }, { threshold: 0.25 })
      : null;
    if (io) io.observe(wrap);
    else visible = true;
    loop();
    return { destroy() { running = false; io?.disconnect(); } };
  }

  // ---------- count-up ----------

  function countUp(node, to, opts = {}) {
    const from = Number(node.dataset.value || 0);
    const decimals = opts.decimals || 0;
    const fmt = (v) => `${opts.prefix || ""}${v.toFixed(decimals)}`;
    node.dataset.value = String(to);
    if (reduced() || from === to) {
      node.textContent = fmt(to);
      return;
    }
    const start = performance.now();
    const duration = opts.duration || 900;
    const step = (now) => {
      const t = Math.min(1, (now - start) / duration);
      const eased = 1 - Math.pow(1 - t, 3);
      node.textContent = fmt(from + (to - from) * eased);
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }

  window.PRLanding = { el, DEMO, storyFromFinding, mountProof, mountPipeline, countUp, reduced, failureLine };
})();
