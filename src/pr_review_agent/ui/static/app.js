// pr-review dashboard. Everything from a run (code, test output, agent text) is inserted as text,
// never as HTML.
"use strict";

const KIND_LABEL = { scan: "Scan", review: "Pull request review", "review-local": "Branch review" };
const SEVERITIES = ["critical", "high", "medium", "low"];
const SEV_LABEL = { critical: "Critical", high: "High", medium: "Medium", low: "Low" };

const state = { runs: [], run: null, fp: null, filters: loadFilters() };

// ---------- small helpers ----------

function h(tag, attrs, ...children) {
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

function inline(text) {
  // `code` spans become <code>; everything else stays plain text.
  return String(text || "").split(/(`[^`\n]+`)/g).map((part) =>
    part.length > 2 && part.startsWith("`") && part.endsWith("`") ? h("code", {}, part.slice(1, -1)) : part,
  );
}

function prose(text) {
  return h("div", { class: "prose" }, String(text || "").split(/\n{2,}/).map((para) => h("p", {}, inline(para))));
}

function plural(n, one, many) {
  return `${n} ${n === 1 ? one : many || one + "s"}`;
}

function when(iso) {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const mins = Math.round((Date.now() - date.getTime()) / 60000);
  const rtf = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
  if (mins < 60) return rtf.format(-mins, "minute");
  if (mins < 60 * 24) return rtf.format(-Math.round(mins / 60), "hour");
  if (mins < 60 * 24 * 7) return rtf.format(-Math.round(mins / 1440), "day");
  return new Intl.DateTimeFormat(undefined, { day: "numeric", month: "short", year: "numeric" }).format(date);
}

function money(n) {
  return `$${Number(n || 0).toFixed(2)}`;
}

function toast(message) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.classList.add("is-shown");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("is-shown"), 1800);
}

async function copy(text, what) {
  try {
    await navigator.clipboard.writeText(text);
    toast(`${what} copied`);
  } catch {
    toast(`Couldn't copy the ${what.toLowerCase()}; select it and copy it instead`);
  }
}

function loadFilters() {
  const fallback = { sev: [...SEVERITIES], tier: "all", fixOnly: false, q: "" };
  try {
    return { ...fallback, ...JSON.parse(localStorage.getItem("pr-review-filters") || "{}"), q: "" };
  } catch {
    return fallback;
  }
}

function saveFilters() {
  try {
    const { q, ...rest } = state.filters;
    localStorage.setItem("pr-review-filters", JSON.stringify(rest));
  } catch {
    /* storage unavailable: filters just won't persist */
  }
}

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} returned ${res.status}`);
  return res.json();
}

// ---------- routing: #/r/<run id>[/f/<fingerprint>] ----------

function parseHash() {
  const m = location.hash.match(/^#\/r\/([^/]+)(?:\/f\/([0-9a-f]+))?/);
  return { runId: m ? decodeURIComponent(m[1]) : null, fp: m ? m[2] || null : null };
}

function go(runId, fp) {
  location.hash = `#/r/${encodeURIComponent(runId)}${fp ? `/f/${fp}` : ""}`;
}

async function route() {
  const { runId, fp } = parseHash();
  const app = document.getElementById("app");
  if (!state.runs.length) {
    renderRuns();
    renderEmpty();
    return;
  }
  const id = runId || state.runs[0].id;
  if (!state.run || state.run.id !== id) {
    try {
      state.run = await getJSON(`/api/runs/${encodeURIComponent(id)}`);
    } catch {
      state.run = null;
    }
  }
  state.fp = fp;
  const narrow = window.matchMedia("(max-width: 860px)").matches;
  if (!fp && state.run && !narrow) {
    const first = visibleFindings()[0];
    if (first) state.fp = first.fingerprint;
  }
  app.dataset.view = fp ? "detail" : runId || !narrow ? "list" : "runs";
  renderRuns();
  renderList();
  renderDetail(Boolean(fp));
}

// ---------- runs pane ----------

function renderRuns() {
  const list = document.getElementById("runs");
  list.replaceChildren(
    ...state.runs.map((r) => {
      const current = state.run && state.run.id === r.id;
      const found = r.verified + r.possible;
      return h(
        "li",
        {},
        h(
          "a",
          { class: "run-link", href: `#/r/${encodeURIComponent(r.id)}`, "aria-current": current ? "page" : null },
          h("span", { class: "run-repo" }, r.repo),
          h("span", { class: "run-sub" }, `${KIND_LABEL[r.kind] || r.kind}, ${when(r.created_at)}`),
          h(
            "span",
            { class: "run-counts" },
            found ? h("span", { class: "hit" }, plural(found, "bug")) : "No bugs found",
            r.fixes ? `, ${plural(r.fixes, "verified fix", "verified fixes")}` : "",
          ),
        ),
      );
    }),
  );
}

// ---------- findings pane ----------

function visibleFindings() {
  if (!state.run) return [];
  const { sev, tier, fixOnly, q } = state.filters;
  const needle = q.trim().toLowerCase();
  return state.run.findings.filter((v) => {
    const f = v.finding;
    if (!sev.includes(f.severity)) return false;
    if (tier !== "all" && v.tier !== tier) return false;
    if (fixOnly && !(v.fix && v.fix.status === "verified")) return false;
    if (needle && !`${f.title} ${f.file} ${f.explanation}`.toLowerCase().includes(needle)) return false;
    return true;
  });
}

function runMeta(run) {
  const parts = [`${KIND_LABEL[run.kind] || run.kind} of ${run.sha.slice(0, 7)}`, when(run.created_at), money(run.stats.cost_usd)];
  if (run.chunks_total) parts.push(`${run.chunks_done} of ${run.chunks_total} chunks reviewed`);
  return parts.join(", ");
}

function renderList() {
  const pane = document.getElementById("list");
  const run = state.run;
  if (!run) {
    pane.replaceChildren(h("div", { class: "empty" }, h("h2", {}, "This run couldn't be loaded"),
      h("p", {}, "The file may have been moved or deleted. Pick another run from the list.")));
    return;
  }
  const verified = run.findings.filter((v) => v.tier === "verified").length;
  const fixes = run.findings.filter((v) => v.fix && v.fix.status === "verified").length;
  const head = h(
    "header",
    { class: "run-head" },
    h("button", { class: "back", type: "button", onclick: () => { location.hash = ""; document.getElementById("app").dataset.view = "runs"; } }, "All runs"),
    h("h1", { class: "run-title" }, run.repo),
    h("p", { class: "run-meta" }, run.target),
    h("p", { class: "run-meta" }, runMeta(run)),
    run.url ? h("p", { class: "run-meta" }, h("a", { href: run.url, target: "_blank", rel: "noreferrer" }, "Open the pull request on GitHub")) : null,
    h(
      "div",
      { class: "tally" },
      h("div", { class: "is-verified" }, h("strong", {}, verified), h("span", {}, "proven by a test")),
      h("div", {}, h("strong", {}, run.findings.length - verified), h("span", {}, "possible")),
      h("div", { class: "is-fixed" }, h("strong", {}, fixes), h("span", {}, fixes === 1 ? "verified fix" : "verified fixes")),
      h("div", {}, h("strong", {}, run.dropped.length), h("span", {}, "dropped")),
    ),
  );

  const f = state.filters;
  const toggleSev = (s) => {
    f.sev = f.sev.includes(s) ? f.sev.filter((x) => x !== s) : [...f.sev, s];
    saveFilters();
    renderList();
  };
  const filters = h(
    "div",
    { class: "filters", role: "group", "aria-label": "Filter findings" },
    SEVERITIES.map((s) => h("button", { class: "chip", type: "button", "aria-pressed": String(f.sev.includes(s)), onclick: () => toggleSev(s) }, SEV_LABEL[s])),
    h("button", { class: "chip", type: "button", "aria-pressed": String(f.tier === "verified"), onclick: () => { f.tier = f.tier === "verified" ? "all" : "verified"; saveFilters(); renderList(); } }, "Proven only"),
    h("button", { class: "chip", type: "button", "aria-pressed": String(f.fixOnly), onclick: () => { f.fixOnly = !f.fixOnly; saveFilters(); renderList(); } }, "Has verified fix"),
    h("input", {
      class: "search", type: "search", placeholder: "Search titles, files and explanations", "aria-label": "Search findings", value: f.q,
      oninput: (e) => { f.q = e.target.value; renderRows(rows); },
    }),
  );

  const rows = h("div", { role: "list" });
  renderRows(rows);

  const foot = h(
    "div",
    { class: "list-foot" },
    run.dropped.length
      ? h("details", { class: "fold" }, h("summary", {}, `Dropped by verification (${run.dropped.length})`),
          h("ul", {}, run.dropped.map((d) => h("li", {}, d.finding.title, h("span", { class: "why" }, `${d.finding.file}:${d.finding.line_start}, ${d.reason}`)))))
      : null,
    run.notes.length
      ? h("details", { class: "fold" }, h("summary", {}, `Run notes (${run.notes.length})`), h("ul", {}, run.notes.map((n) => h("li", {}, n))))
      : null,
  );
  pane.replaceChildren(head, filters, rows, foot);
}

function renderRows(container) {
  const run = state.run;
  const items = visibleFindings();
  if (!run.findings.length) {
    container.replaceChildren(h("div", { class: "empty" }, h("h2", {}, "No proven bugs in this run"),
      h("p", {}, "Nothing the agent suspected survived verification. Anything it suspected is listed under Dropped by verification.")));
    return;
  }
  if (!items.length) {
    container.replaceChildren(h("div", { class: "empty" }, h("p", {}, "No findings match these filters."),
      h("button", { class: "btn", type: "button", onclick: () => { state.filters = { sev: [...SEVERITIES], tier: "all", fixOnly: false, q: "" }; saveFilters(); renderList(); } }, "Clear filters")));
    return;
  }
  container.replaceChildren(
    ...items.map((v) => {
      const f = v.finding;
      const fix = v.fix;
      return h(
        "button",
        {
          class: `finding-row sev-${f.severity}`, type: "button", role: "listitem",
          "aria-current": state.fp === v.fingerprint ? "true" : null, "data-fp": v.fingerprint,
          onclick: () => go(run.id, v.fingerprint),
        },
        h("span", { class: "margin-bar", "aria-hidden": "true" }),
        h(
          "span",
          {},
          h("span", { class: "row-title" }, f.title),
          h("span", { class: "row-where" }, `${f.file}:${f.line_start}`),
          h(
            "span",
            { class: "row-tags" },
            h("span", { class: `sev-text-${f.severity}` }, SEV_LABEL[f.severity]),
            h("span", { class: v.tier === "verified" ? "tag-verified" : "tag-possible" }, v.tier === "verified" ? "Proven" : "Possible"),
            fix ? h("span", { class: fix.status === "verified" ? "tag-fix" : "tag-fix-failed" },
              { verified: "Fix verified", unverified: "Fix not tested", failed: "Fix failed checks" }[fix.status]) : null,
          ),
        ),
      );
    }),
  );
}

// ---------- detail pane ----------

function failureLine(output) {
  const lines = String(output || "").split("\n").map((l) => l.trim()).filter(Boolean);
  return lines.find((l) => /error|expected|assert/i.test(l)) || lines[1] || lines[0] || "";
}

function trailSteps(run, v) {
  const f = v.finding;
  const steps = [];
  const test = v.evidence.find((e) => e.kind === "failing_test");
  const regression = v.evidence.find((e) => e.kind === "test_regression");
  const isPR = run.kind !== "scan";
  if (test) {
    if (isPR) {
      steps.push(v.pre_existing
        ? { state: "warn", title: "Already failed before this change", detail: "The bug predates the pull request, which touches this code." }
        : { state: "pass", title: "Passes before this change", detail: "The same test passes on the base branch." });
    }
    steps.push({ state: "fail", title: isPR ? "Fails with this change" : "Fails on this code", detail: failureLine(test.test_output) });
  } else if (regression) {
    steps.push({ state: "pass", title: "Passed before this change", detail: regression.test_id });
    steps.push({ state: "fail", title: "Fails with this change", detail: failureLine(regression.test_output) || "An existing test now fails." });
  } else {
    const kinds = [...new Set(v.evidence.map((e) => (e.kind === "static" ? "a compiler or linter error" : "quoted code")))];
    steps.push({ state: "warn", title: "Not proven by a test", detail: `Backed by ${kinds.join(" and ")}, with ${Math.round(f.confidence * 100)}% confidence.` });
  }
  const fix = v.fix;
  if (!fix) {
    steps.push({ state: "none", title: "No checked fix", detail: f.suggested_fix || "The agent didn't propose a code change." });
  } else if (fix.status === "verified") {
    const passNote = fix.notes.find((n) => /passes with the fix|pass again/.test(n));
    steps.push({ state: "pass", title: "Passes with the fix", detail: passNote ? "The same test passes once the patch is applied." : "The regressed test passes again." });
    const suite = fix.notes.find((n) => n.startsWith("existing tests still pass"));
    if (suite) steps.push({ state: "pass", title: "Existing tests still pass", detail: suite.replace(/^existing tests still pass\s*/, "").replace(/[()]/g, "") });
  } else if (fix.status === "unverified") {
    steps.push({ state: "warn", title: "Fix applies cleanly", detail: "No test could confirm it, so review the patch yourself." });
  } else {
    steps.push({ state: "fail", title: "Proposed fix failed its checks", detail: fix.notes.find((n) => /still fails|breaks|introduces|did not run/.test(n)) || fix.notes[0] || "" });
  }
  return steps;
}

function trail(run, v, animate) {
  const glyph = { fail: "✗", pass: "✓", warn: "!", none: "" };
  return h(
    "ol",
    { class: `trail${animate ? " is-entering" : ""}`, "aria-label": "How this bug was verified" },
    trailSteps(run, v).map((s) =>
      h("li", { class: `step is-${s.state}` },
        h("span", { class: "node", "aria-hidden": "true" }, glyph[s.state]),
        h("span", { class: "step-title" }, s.title),
        s.detail ? h("span", { class: "step-detail" }, inline(s.detail)) : null),
    ),
  );
}

function codeBlock(title, lines, opts = {}) {
  return h(
    "div",
    { class: `codeblock${opts.extra ? " " + opts.extra : ""}` },
    h("div", { class: "codeblock-head" }, h("span", { class: opts.path ? "path" : "" }, title), opts.action || null),
    h("pre", { tabindex: "0" }, h("code", {}, lines)),
  );
}

function excerpt(code) {
  const [a, b] = code.highlight;
  const label = a === b ? `${code.file}, line ${a}` : `${code.file}, lines ${a}–${b}`;
  const lines = code.lines.map((text, i) => {
    const n = code.start + i;
    const hot = n >= a && n <= b;
    return h("span", { class: `line${hot ? " hot" : ""}` }, h("span", { class: "num", "aria-hidden": "true" }, n), h("span", { class: "src" }, text || " "), "\n");
  });
  return codeBlock(label, lines, { path: true });
}

function plainBlock(title, text, opts = {}) {
  return codeBlock(title, h("span", { class: "plain" }, text), opts);
}

function diffBlock(patch) {
  const lines = [];
  let file = "";
  for (const line of patch.split("\n")) {
    if (line.startsWith("diff --git") || line.startsWith("--- ") || line === "") continue;
    if (line.startsWith("+++ ")) {
      file = line.slice(6);
      lines.push(h("span", { class: "dline hunk" }, h("span", { class: "sign" }, ""), h("span", {}, file), "\n"));
      continue;
    }
    const kind = line.startsWith("@@") ? "hunk" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : line.startsWith("\\") ? "hunk" : "ctx";
    const sign = kind === "add" ? "+" : kind === "del" ? "−" : "";
    const body = kind === "hunk" || kind === "ctx" ? line.replace(/^ /, "") : line.slice(1);
    lines.push(h("span", { class: `dline ${kind}` }, h("span", { class: "sign", "aria-hidden": "true" }, sign), h("span", {}, body || " "), "\n"));
  }
  return h("div", { class: "codeblock diff" }, h("pre", { tabindex: "0" }, h("code", {}, lines)));
}

function fixSection(run, v) {
  const fix = v.fix;
  const f = v.finding;
  if (!fix) {
    return f.suggested_fix
      ? h("section", { class: "section" }, h("h2", { class: "section-title" }, "Suggested fix"), prose(f.suggested_fix),
          h("p", { class: "hint" }, "The agent described a fix but didn't propose a patch that passed its checks."))
      : null;
  }
  const status = {
    verified: ["Verified", "The failing test passes with this patch, and nothing else broke."],
    unverified: ["Not tested", "The patch applies cleanly, but there was no test to confirm it."],
    failed: ["Failed its checks", "Shown so you can see what was tried. Don't apply it as is."],
  }[fix.status];
  const patchUrl = `/api/runs/${encodeURIComponent(run.id)}/patch/${v.fingerprint}`;
  return h(
    "section",
    { class: "section" },
    h("h2", { class: "section-title" }, "Fix"),
    h("p", { class: `fix-status fix-${fix.status}` }, h("strong", {}, status[0]), h("span", { class: "hint" }, status[1])),
    f.suggested_fix ? prose(f.suggested_fix) : null,
    h("div", { class: "fix-diff" }, diffBlock(fix.patch)),
    h("ul", { class: "checks" }, fix.notes.map((n) => h("li", { class: /still fails|breaks|introduces|did not run|could not/.test(n) ? "bad" : "" }, n))),
    fix.status !== "failed"
      ? h("div", { class: "actions" },
          h("button", { class: "btn btn-primary", type: "button", onclick: () => copy(fix.patch, "Patch") }, "Copy patch"),
          h("a", { class: "btn", href: patchUrl, download: `fix-${v.fingerprint}.patch` }, "Download patch"),
          h("span", { class: "hint" }, "Apply it from the repository root with ", h("code", { class: "inline-code" }, `git apply fix-${v.fingerprint}.patch`)))
      : null,
  );
}

function evidenceSection(v) {
  const blocks = v.evidence.map((e) => {
    if (e.kind === "failing_test") {
      return [
        codeBlock("Test written by the agent and re-run by pr-review", h("span", { class: "plain" }, e.test_code || ""),
          { action: h("button", { class: "btn", type: "button", onclick: () => copy(e.test_code || "", "Test") }, "Copy test") }),
        plainBlock("Output on the code under review", e.test_output || "", { extra: "output" }),
      ];
    }
    if (e.kind === "test_regression") {
      return plainBlock(`Existing test ${e.test_id} now fails`, e.test_output || "It passes on the base branch and fails with this change.", { extra: "output" });
    }
    if (e.kind === "static") {
      return plainBlock(`${e.tool} ${e.rule || ""} at ${e.file}:${e.line}`, "Reported only for this version of the code.", { extra: "output" });
    }
    if (e.kind === "code_reference") {
      return h("div", { class: "codeblock" },
        h("div", { class: "codeblock-head" }, h("span", { class: "path" }, `${e.file}, line ${e.line}`)),
        h("pre", { tabindex: "0" }, h("code", {}, h("span", { class: "plain" }, e.snippet || ""))),
        e.why_relevant ? h("div", { class: "ref-why" }, inline(e.why_relevant)) : null);
    }
    return null;
  });
  return h("section", { class: "section" }, h("h2", { class: "section-title" }, "Evidence"),
    h("p", { class: "section-sub" }, "Checked by pr-review after the agent finished; nothing here is taken on trust."),
    blocks);
}

function renderDetail(focus) {
  const pane = document.getElementById("detail");
  const run = state.run;
  const v = run && run.findings.find((x) => x.fingerprint === state.fp);
  if (!v) {
    pane.replaceChildren(
      run && run.findings.length
        ? h("div", { class: "empty" }, h("h2", {}, "Pick a bug"), h("p", {}, "Choose a finding to see its evidence and fix."))
        : h("div", { class: "empty" }),
    );
    return;
  }
  const f = v.finding;
  const animate = renderDetail.last !== v.fingerprint;
  renderDetail.last = v.fingerprint;
  const range = f.line_start === f.line_end ? `line ${f.line_start}` : `lines ${f.line_start}–${f.line_end}`;
  pane.replaceChildren(
    h(
      "article",
      { class: "detail-inner" },
      h("button", { class: "back", type: "button", onclick: () => go(run.id) }, "Back to findings"),
      h("p", { class: "kicker" }, h("span", { class: `sev sev-text-${f.severity}` }, `${SEV_LABEL[f.severity]} severity`), `, ${f.category.replace("-", " ")} in ${f.project}`),
      h("h1", { class: "detail-title" }, f.title),
      h("p", { class: "where" }, `${f.file}, ${range}`),
      trail(run, v, animate),
      prose(f.explanation),
      v.code ? h("section", { class: "section" }, h("h2", { class: "section-title" }, "Where it goes wrong"), excerpt(v.code)) : null,
      fixSection(run, v),
      evidenceSection(v),
      v.notes.length ? h("section", { class: "section" }, h("details", { class: "fold" }, h("summary", {}, "Verification notes"), h("ul", {}, v.notes.map((n) => h("li", {}, n))))) : null,
    ),
  );
  if (focus) pane.focus({ preventScroll: true });
  pane.scrollTop = 0;
}

function renderEmpty() {
  document.getElementById("app").dataset.view = "list";
  document.getElementById("list").replaceChildren(
    h("div", { class: "empty" },
      h("h2", {}, "No runs yet"),
      h("p", {}, "Scan a repository or review a pull request, and the results show up here."),
      h("p", {}, h("code", { class: "inline-code" }, "pr-review scan ~/code/my-app")),
      h("p", {}, h("code", { class: "inline-code" }, "pr-review review owner/repo#123"))),
  );
  document.getElementById("detail").replaceChildren();
}

// ---------- keyboard: j/k or arrows move through findings ----------

document.addEventListener("keydown", (e) => {
  if (!state.run || e.metaKey || e.ctrlKey || e.altKey) return;
  if (e.target instanceof HTMLInputElement) return;
  const step = { j: 1, ArrowDown: 1, k: -1, ArrowUp: -1 }[e.key];
  if (!step) return;
  const items = visibleFindings();
  if (!items.length) return;
  const i = items.findIndex((v) => v.fingerprint === state.fp);
  const next = items[Math.min(items.length - 1, Math.max(0, i + step))];
  if (next && next.fingerprint !== state.fp) {
    e.preventDefault();
    go(state.run.id, next.fingerprint);
    requestAnimationFrame(() => document.querySelector(`[data-fp="${next.fingerprint}"]`)?.scrollIntoView({ block: "nearest" }));
  }
});

window.addEventListener("hashchange", route);

(async function start() {
  try {
    state.runs = await getJSON("/api/runs");
  } catch {
    state.runs = [];
  }
  route();
})();
