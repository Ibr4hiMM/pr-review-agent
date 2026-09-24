// pr-review dashboard. Everything from a run (code, test output, agent text) is inserted as text,
// never as HTML. Every API call carries the per-session token embedded in the page.
"use strict";

const TOKEN = document.querySelector('meta[name="pr-review-token"]')?.content || "";
const KIND_LABEL = { scan: "Scan", review: "Pull request review", "review-local": "Branch review" };
const SEVERITIES = ["critical", "high", "medium", "low"];
const SEV_LABEL = { critical: "Critical", high: "High", medium: "Medium", low: "Low" };
const TRIAGE = [
  ["open", "Open"],
  ["fixed", "Fixed"],
  ["wont_fix", "Won't fix"],
  ["false_positive", "False alarm"],
];
const TRIAGE_LABEL = Object.fromEntries(TRIAGE);
const CONTEXT_LINES = 12;

const state = {
  runs: [],
  run: null,
  fp: null,
  jobs: [],
  filters: loadPref("pr-review-filters", { sev: [...SEVERITIES], tier: "all", fixOnly: false, openOnly: false }),
  editor: loadPref("pr-review-editor", "vscode"),
  q: "",
  viewer: null, // { fp, file, mode: "current" | "fixed", whole, focus }
  apply: null, // { fp, open, path, result, busy, error }
  form: null, // new-scan form state
};

// ---------- helpers ----------

function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else if (key === "value") node.value = value;
    else if (key === "checked") node.checked = Boolean(value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

function fill(el, ...children) {
  el.replaceChildren(...children.flat(Infinity).filter((c) => c !== null && c !== undefined && c !== false));
}

function inline(text) {
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

function duration(seconds) {
  if (seconds == null) return "";
  const s = Math.round(seconds);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

function money(n) {
  return `$${Number(n || 0).toFixed(2)}`;
}

function toast(message) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.classList.add("is-shown");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => el.classList.remove("is-shown"), 2200);
}

async function copy(text, what) {
  try {
    await navigator.clipboard.writeText(text);
    toast(`${what} copied`);
  } catch {
    toast(`Couldn't copy the ${what.toLowerCase()}; select it and copy it instead`);
  }
}

function loadPref(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    if (raw === null) return fallback;
    const value = JSON.parse(raw);
    return typeof fallback === "object" && !Array.isArray(fallback) ? { ...fallback, ...value } : value;
  } catch {
    return fallback;
  }
}

function savePref(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* storage unavailable: the preference just won't persist */
  }
}

async function api(path, options = {}) {
  const init = { method: options.method || "GET", headers: { "X-PR-Review-Token": TOKEN } };
  if (options.body !== undefined) {
    init.method = options.method || "POST";
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(options.body);
  }
  const res = await fetch(path, init);
  if (res.status === 401) showRestarted();
  const data = res.headers.get("Content-Type")?.includes("application/json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error((data && data.error) || `Request failed (${res.status})`);
  return data;
}

function showRestarted() {
  if (document.getElementById("restarted")) return;
  document.body.append(h("div", { class: "restarted", id: "restarted", role: "alert" },
    h("span", {}, "The dashboard was restarted, so this page is out of date."),
    h("button", { class: "btn btn-primary", type: "button", onclick: () => location.reload() }, "Reload page")));
}

function segmented(label, options, current, onPick, cls = "") {
  return h(
    "div",
    { class: `segmented ${cls}`, role: "radiogroup", "aria-label": label },
    options.map(([value, text]) =>
      h("button", {
        type: "button", role: "radio", "aria-checked": String(value === current), class: "seg",
        onclick: () => onPick(value),
      }, text),
    ),
  );
}

// ---------- routing ----------
// #/r/<run>[/f/<fp>]   #/new   #/guide   #/jobs/<id>

function parseHash() {
  const hash = location.hash;
  let m;
  if ((m = hash.match(/^#\/r\/([^/]+)(?:\/f\/([0-9a-f]+))?/))) return { page: "run", runId: decodeURIComponent(m[1]), fp: m[2] || null };
  if (hash === "#/new") return { page: "new" };
  if (hash === "#/guide") return { page: "guide" };
  if ((m = hash.match(/^#\/jobs\/([0-9a-f]+)/))) return { page: "job", jobId: m[1] };
  return { page: "home" };
}

function go(runId, fp) {
  location.hash = `#/r/${encodeURIComponent(runId)}${fp ? `/f/${fp}` : ""}`;
}

const anims = []; // live animations on the current page, stopped when leaving it
let pageEnter = false; // true until the first paint after navigating to a page

// "page" plus the entrance class, but only for the first paint after navigation; re-draws
// while you use the page must not replay the entrance (it made the form flicker).
function pageClass(extra = "") {
  const cls = `page${pageEnter ? " is-entering" : ""}${extra ? " " + extra : ""}`;
  pageEnter = false;
  return cls;
}
function stopAnims() {
  while (anims.length) anims.pop().destroy();
}

async function route() {
  const r = parseHash();
  const app = document.getElementById("app");
  stopJobPolling();
  stopAnims();
  renderSidebar();
  if (r.page !== "run") {
    pageEnter = true;
    app.dataset.mode = "page";
    app.dataset.view = "detail";
    if (r.page === "new") return renderNewScan();
    if (r.page === "job") return renderJob(r.jobId);
    if (r.page === "guide") return renderGuide();
    return renderOverview();
  }
  app.dataset.mode = "run";
  const prevRun = state.run && state.run.id;
  const prevFp = state.fp;
  if (!state.run || state.run.id !== r.runId) {
    try {
      state.run = await api(`/api/runs/${encodeURIComponent(r.runId)}`);
    } catch {
      state.run = null;
    }
  }
  state.fp = r.fp;
  const narrow = window.matchMedia("(max-width: 860px)").matches;
  if (!state.fp && state.run && !narrow) {
    const first = visibleFindings()[0];
    if (first) state.fp = first.fingerprint;
  }
  app.dataset.view = r.fp ? "detail" : "list";
  const paint = () => {
    renderSidebar();
    renderList();
    renderDetail(Boolean(r.fp));
  };
  const switching = prevRun === r.runId && prevFp && state.fp && prevFp !== state.fp;
  if (switching && document.startViewTransition && !window.PRLanding.reduced()) document.startViewTransition(paint);
  else paint();
}

// ---------- sidebar: jobs + runs ----------

function jobStatusText(j) {
  if (j.status === "running") return `Running, ${duration(j.elapsed_s)}`;
  if (j.status === "queued") return "Waiting to start";
  if (j.status === "done") return `Finished in ${duration(j.elapsed_s)}`;
  if (j.status === "failed") return "Failed";
  return "Cancelled";
}

function renderSidebar() {
  const r = parseHash();
  const active = state.jobs.filter((j) => j.status === "running" || j.status === "queued");
  const recent = state.jobs.filter((j) => j.status !== "running" && j.status !== "queued").slice(0, 3);
  const jobs = [...active, ...recent];
  document.getElementById("jobs").replaceChildren(
    ...(jobs.length
      ? [h("section", { class: "jobs" }, h("h2", { class: "pane-title" }, "Started from here"),
          h("ol", {}, jobs.map((j) => h("li", {},
            h("a", { class: `job-link job-${j.status}`, href: `#/jobs/${j.id}`, "aria-current": r.jobId === j.id ? "page" : null },
              h("span", { class: "job-title" }, j.title),
              h("span", { class: "job-state" }, j.status === "running" ? h("span", { class: "pulse", "aria-hidden": "true" }) : null, jobStatusText(j)))))))]
      : []),
  );
  document.getElementById("runs").replaceChildren(
    ...(state.runs.length ? state.runs.map((run) => {
      const current = (r.page === "run" || r.page === "home") && state.run && state.run.id === run.id;
      const found = run.verified + run.possible;
      return h("li", {},
        h("a", { class: "run-link", href: `#/r/${encodeURIComponent(run.id)}`, "aria-current": current ? "page" : null },
          h("span", { class: "run-repo" }, run.repo),
          h("span", { class: "run-sub" }, `${KIND_LABEL[run.kind] || run.kind}, ${when(run.created_at)}`),
          h("span", { class: "run-counts" },
            found ? h("span", { class: "hit" }, plural(found, "bug")) : "No bugs found",
            run.fixes ? `, ${plural(run.fixes, "verified fix", "verified fixes")}` : "")));
    }) : [h("li", { class: "runs-empty" }, "Runs you start show up here.")]),
  );
}

let jobsTimer = null;
async function refreshJobs() {
  try {
    const before = new Map(state.jobs.map((j) => [j.id, j.status]));
    state.jobs = await api("/api/jobs");
    const finished = state.jobs.some((j) => before.get(j.id) && before.get(j.id) !== j.status && j.status === "done");
    if (finished) state.runs = await api("/api/runs");
    renderSidebar();
  } catch {
    /* server restarted or unreachable; try again on the next tick */
  }
  const busy = state.jobs.some((j) => j.status === "running" || j.status === "queued");
  clearTimeout(jobsTimer);
  jobsTimer = setTimeout(refreshJobs, busy ? 1500 : 6000);
}

// ---------- findings list ----------

function triageOf(fp) {
  return (state.run && state.run.triage && state.run.triage[fp]) || { status: "open" };
}

function visibleFindings() {
  if (!state.run) return [];
  const { sev, tier, fixOnly, openOnly } = state.filters;
  const needle = state.q.trim().toLowerCase();
  return state.run.findings.filter((v) => {
    const f = v.finding;
    if (!sev.includes(f.severity)) return false;
    if (tier !== "all" && v.tier !== tier) return false;
    if (fixOnly && !(v.fix && v.fix.status === "verified")) return false;
    if (openOnly && triageOf(v.fingerprint).status !== "open") return false;
    if (needle && !`${f.title} ${f.file} ${f.explanation}`.toLowerCase().includes(needle)) return false;
    return true;
  });
}

function runMeta(run) {
  const parts = [`${KIND_LABEL[run.kind] || run.kind} of ${run.sha.slice(0, 7)}`, when(run.created_at), money(run.stats.cost_usd)];
  if (run.chunks_total) parts.push(`${run.chunks_done} of ${run.chunks_total} chunks reviewed`);
  return parts.join(", ");
}

// The job or run that picked up an unfinished scan, so it isn't continued twice.
function continuationOf(runId) {
  const job = state.jobs.find((j) => j.params.continues === runId && (j.status === "running" || j.status === "queued"));
  if (job) return h("a", { href: `#/jobs/${job.id}` }, "It's being continued now");
  const run = state.runs.find((r) => r.continues === runId);
  return run ? h("a", { href: `#/r/${encodeURIComponent(run.id)}` }, "Open the run that continued it") : null;
}

// Offered when a scan stopped before reviewing every chunk: scan again with a new limit. Reviewed chunks
// come from the cache, so the whole limit goes to the chunks that are left.
function continueBox(runId, done, total, budget) {
  const title = h("h2", { class: "continue-title" }, `${plural(total - done, "chunk")} not reviewed yet`);
  const next = continuationOf(runId);
  if (next) return h("section", { class: "continue" }, title, h("p", {}, "This scan stopped before the end. ", next, "."));
  const amount = h("input", { class: "input", type: "number", min: "1", max: "100", step: "0.5", value: budget || 5 });
  const error = h("p", { class: "field-error", role: "alert" });
  const btn = h("button", { class: "btn btn-primary", type: "button", onclick: async () => {
    btn.disabled = true;
    error.textContent = "";
    try {
      const job = await api(`/api/runs/${encodeURIComponent(runId)}/continue`, { body: { budget_usd: Number(amount.value) } });
      state.jobs = [job, ...state.jobs.filter((j) => j.id !== job.id)];
      refreshJobs();
      location.hash = `#/jobs/${job.id}`;
    } catch (e) {
      error.textContent = e.message;
      btn.disabled = false;
    }
  } }, "Continue");
  return h("section", { class: "continue" }, title,
    h("p", {}, `The scan stopped after ${done} of ${total} chunks, riskiest first. Continuing reviews the rest. `,
      "Chunks already reviewed are reused at no cost, and the new run keeps everything this one found."),
    h("div", { class: "continue-row" },
      // The input sits inside its label: the list pane keeps its last run's content while a job page
      // shows, so an id could exist twice.
      h("label", { class: "continue-amount" }, "Usage limit for the rest",
        h("span", { class: "money-input" }, h("span", { "aria-hidden": "true" }, "$"), amount)),
      btn),
    error);
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
  const open = run.findings.filter((v) => triageOf(v.fingerprint).status === "open").length;
  const f = state.filters;
  const setFilter = (patch) => { Object.assign(f, patch); savePref("pr-review-filters", f); renderList(); };
  const fresh = renderList.lastRun !== run.id; // first paint of this run: count up and stagger in
  renderList.lastRun = run.id;
  const rows = h("div", { role: "list", class: `rows${fresh ? " is-entering" : ""}` });

  pane.replaceChildren(
    h("header", { class: "run-head" },
      h("a", { class: "back", href: "#/" }, "Overview"),
      h("h1", { class: "run-title" }, run.repo),
      h("p", { class: "run-meta" }, run.target),
      h("p", { class: "run-meta" }, runMeta(run)),
      run.continues ? h("p", { class: "run-meta" }, "Picks up where ", h("a", { href: `#/r/${encodeURIComponent(run.continues)}` }, "an earlier scan"), " stopped.") : null,
      run.url ? h("p", { class: "run-meta" }, h("a", { href: run.url, target: "_blank", rel: "noreferrer" }, "Open the pull request on GitHub")) : null,
      h("div", { class: "tally" },
        h("div", { class: "is-verified" }, tallyNum(verified, fresh), h("span", {}, "proven by a test")),
        h("div", {}, tallyNum(run.findings.length - verified, fresh), h("span", {}, "possible")),
        h("div", { class: "is-fixed" }, tallyNum(fixes, fresh), h("span", {}, fixes === 1 ? "verified fix" : "verified fixes")),
        h("div", {}, tallyNum(open, fresh), h("span", {}, "still open"))),
      run.kind === "scan" && run.repo_path && run.chunks_done < run.chunks_total
        ? continueBox(run.id, run.chunks_done, run.chunks_total, run.budget_usd) : null),
    h("div", { class: "filters", role: "group", "aria-label": "Filter findings" },
      SEVERITIES.map((s) => h("button", { class: "chip", type: "button", "aria-pressed": String(f.sev.includes(s)),
        onclick: () => setFilter({ sev: f.sev.includes(s) ? f.sev.filter((x) => x !== s) : [...f.sev, s] }) }, SEV_LABEL[s])),
      h("button", { class: "chip", type: "button", "aria-pressed": String(f.tier === "verified"), onclick: () => setFilter({ tier: f.tier === "verified" ? "all" : "verified" }) }, "Proven only"),
      h("button", { class: "chip", type: "button", "aria-pressed": String(f.fixOnly), onclick: () => setFilter({ fixOnly: !f.fixOnly }) }, "Has verified fix"),
      h("button", { class: "chip", type: "button", "aria-pressed": String(f.openOnly), onclick: () => setFilter({ openOnly: !f.openOnly }) }, "Open only"),
      h("input", { class: "search", type: "search", placeholder: "Search titles, files and explanations", "aria-label": "Search findings", value: state.q,
        oninput: (e) => { state.q = e.target.value; renderRows(rows); } })),
    rows,
    h("div", { class: "list-foot" },
      run.dropped.length
        ? h("details", { class: "fold" }, h("summary", {}, `Dropped by verification (${run.dropped.length})`),
            h("ul", {}, run.dropped.map((d) => h("li", {}, d.finding.title, h("span", { class: "why" }, `${d.finding.file}:${d.finding.line_start}, ${d.reason}`)))))
        : null,
      run.notes.length
        ? h("details", { class: "fold" }, h("summary", {}, `Run notes (${run.notes.length})`), h("ul", {}, run.notes.map((n) => h("li", {}, n))))
        : null),
  );
  renderRows(rows);
}

function tallyNum(value, animate) {
  const node = h("strong", {}, animate ? 0 : value);
  if (animate) requestAnimationFrame(() => window.PRLanding.countUp(node, value, { duration: 700 }));
  return node;
}

function renderRows(container) {
  const run = state.run;
  const items = visibleFindings();
  if (!run.findings.length) {
    container.replaceChildren(h("div", { class: "empty" }, h("h2", {}, "No proven bugs in this run"),
      h("p", {}, "Nothing the agent suspected survived verification. Its suspicions are listed under Dropped by verification.")));
    return;
  }
  if (!items.length) {
    container.replaceChildren(h("div", { class: "empty" }, h("p", {}, "No findings match these filters."),
      h("button", { class: "btn", type: "button", onclick: () => {
        state.filters = { sev: [...SEVERITIES], tier: "all", fixOnly: false, openOnly: false };
        state.q = "";
        savePref("pr-review-filters", state.filters);
        renderList();
      } }, "Clear filters")));
    return;
  }
  container.replaceChildren(...items.map((v) => {
    const f = v.finding;
    const fix = v.fix;
    const triage = triageOf(v.fingerprint).status;
    return h("button", {
      class: `finding-row sev-${f.severity}${triage !== "open" ? " is-closed" : ""}`, type: "button", role: "listitem",
      "aria-current": state.fp === v.fingerprint ? "true" : null, "data-fp": v.fingerprint,
      onclick: () => go(run.id, v.fingerprint),
    },
      h("span", { class: "margin-bar", "aria-hidden": "true" }),
      h("span", {},
        h("span", { class: "row-title" }, f.title),
        h("span", { class: "row-where" }, `${f.file}:${f.line_start}`),
        h("span", { class: "row-tags" },
          h("span", { class: `sev-text-${f.severity}` }, SEV_LABEL[f.severity]),
          h("span", { class: v.tier === "verified" ? "tag-verified" : "tag-possible" }, v.tier === "verified" ? "Proven" : "Possible"),
          fix ? h("span", { class: fix.status === "verified" ? "tag-fix" : "tag-fix-failed" },
            { verified: "Fix verified", unverified: "Fix not tested", failed: "Fix failed checks" }[fix.status]) : null,
          triage !== "open" ? h("span", { class: "tag-triage" }, TRIAGE_LABEL[triage]) : null)));
  }));
}

// ---------- detail: trail ----------

function failureLine(output) {
  const lines = String(output || "").split("\n").map((l) => l.trim()).filter(Boolean);
  return lines.find((l) => /error|expected|assert/i.test(l)) || lines[1] || lines[0] || "";
}

// Fix-check notes that explain why a fix failed (see runner.py).
const FAILED_NOTE = /still fails|fails in new places|flaky|breaks|introduces|did not run|could not|still reported/;

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
    const passNote = fix.notes.find((n) => /passes with the fix|pass again/.test(n)) || "";
    const partial = passNote.match(/for (\d+) of (\d+) failing cases/);
    const runs = passNote.match(/\((\d+) runs in a row\)/);
    steps.push({ state: "pass", title: "Passes with the fix",
      detail: partial ? `${partial[1]} of ${partial[2]} failing test cases pass; the others belong to another finding that cites the same test.`
        : passNote ? `The same test passes once the patch is applied${runs ? `, ${runs[1]} runs in a row` : ""}.` : "The regressed test passes again." });
    const suite = fix.notes.find((n) => n.startsWith("existing tests still pass"));
    if (suite) steps.push({ state: "pass", title: "Existing tests still pass", detail: suite.replace(/^existing tests still pass\s*/, "").replace(/[()]/g, "") });
  } else if (fix.status === "unverified") {
    steps.push({ state: "warn", title: "Fix applies cleanly", detail: "No test could confirm it, so review the patch yourself." });
  } else {
    steps.push({ state: "fail", title: "Proposed fix failed its checks", detail: fix.notes.find((n) => FAILED_NOTE.test(n)) || fix.notes[0] || "" });
  }
  return steps;
}

function trail(run, v, animate) {
  const glyph = { fail: "✗", pass: "✓", warn: "!", none: "" };
  return h("ol", { class: `trail${animate ? " is-entering" : ""}`, "aria-label": "How this bug was verified" },
    trailSteps(run, v).map((s) => h("li", { class: `step is-${s.state}` },
      h("span", { class: "node", "aria-hidden": "true" }, glyph[s.state]),
      h("span", { class: "step-title" }, s.title),
      s.detail ? h("span", { class: "step-detail" }, inline(s.detail)) : null)));
}

// ---------- detail: triage ----------

function triageBar(run, v) {
  const t = triageOf(v.fingerprint);
  const noteBox = h("textarea", { class: "note", rows: "2", placeholder: "Add a note for your team (optional)", "aria-label": "Note", value: t.note || "" });
  const save = async (status, note) => {
    try {
      const entry = await api("/api/triage", { body: { run_id: run.id, fp: v.fingerprint, status, note } });
      run.triage = { ...(run.triage || {}), [v.fingerprint]: entry };
      toast(note !== undefined && status === t.status ? "Note saved" : `Marked ${TRIAGE_LABEL[status].toLowerCase()}`);
      renderList();
      renderDetail(false);
    } catch (e) {
      toast(e.message);
    }
  };
  return h("div", { class: "triage" },
    segmented("Status", TRIAGE, t.status, (status) => save(status, undefined), "triage-seg"),
    h("details", { class: "note-fold", open: t.note ? true : null },
      h("summary", {}, t.note ? "Note" : "Add a note"),
      noteBox,
      h("div", { class: "actions" }, h("button", { class: "btn", type: "button", onclick: () => save(t.status, noteBox.value) }, "Save note"),
        t.updated_at ? h("span", { class: "hint" }, `Updated ${when(t.updated_at)}`) : null)));
}

// ---------- detail: code viewer ----------

function addedLines(patch, file) {
  const out = new Set();
  let inFile = false;
  let inHunk = false;
  let n = 0;
  for (const line of String(patch || "").split("\n")) {
    if (line.startsWith("diff --git")) {
      inFile = line.endsWith(` b/${file}`);
      inHunk = false;
      continue;
    }
    if (!inFile) continue;
    const m = line.match(/^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    if (m) {
      n = Number(m[1]);
      inHunk = true;
      continue;
    }
    if (!inHunk) continue;
    if (line.startsWith("+")) out.add(n++);
    else if (line.startsWith("-") || line.startsWith("\\")) continue;
    else n++;
  }
  return out;
}

function viewerFiles(run, v) {
  const files = [v.finding.file];
  if (v.fix && v.fix.patched) files.push(...Object.keys(v.fix.patched));
  for (const e of v.evidence) if (e.file) files.push(e.file);
  return [...new Set(files)].filter((f) => run.sources && run.sources[f] !== undefined);
}

function editorLink(run, file, line) {
  const scheme = { vscode: "vscode", cursor: "cursor" }[state.editor];
  if (!scheme || !run.local_path) return null;
  const abs = `${run.local_path.replace(/\/$/, "")}/${file}`;
  return h("a", { class: "btn btn-quiet", href: `${scheme}://file${encodeURI(abs)}:${line || 1}` },
    state.editor === "cursor" ? "Open in Cursor" : "Open in VS Code");
}

function openInViewer(file, line, mode) {
  state.viewer = { ...state.viewer, file, focus: line || null, mode: mode || "current", whole: Boolean(line) || state.viewer.whole };
  renderDetail(false);
  requestAnimationFrame(() => document.getElementById("code-viewer")?.scrollIntoView({ behavior: "smooth", block: "start" }));
}

function codeViewer(run, v) {
  const f = v.finding;
  if (!state.viewer || state.viewer.fp !== v.fingerprint) {
    state.viewer = { fp: v.fingerprint, file: f.file, mode: "current", whole: false, focus: null };
  }
  const vw = state.viewer;
  const files = viewerFiles(run, v);
  if (!files.length) return v.code ? excerptFallback(v.code) : null; // runs saved before the viewer existed
  if (!files.includes(vw.file)) vw.file = files[0];
  const fixed = Boolean(v.fix && v.fix.patched && v.fix.patched[vw.file] !== undefined);
  if (!fixed) vw.mode = "current";
  const text = vw.mode === "fixed" ? v.fix.patched[vw.file] : run.sources[vw.file];
  const lines = text.replace(/\n$/, "").split("\n");
  const bug = vw.mode === "current" && vw.file === f.file ? [f.line_start, f.line_end] : null;
  const added = vw.mode === "fixed" ? addedLines(v.fix.patch, vw.file) : new Set();
  const center = vw.focus || (bug ? bug[0] : added.size ? Math.min(...added) : 1);
  const [lo, hi] = vw.whole ? [1, lines.length] : [Math.max(1, center - CONTEXT_LINES), Math.min(lines.length, (bug ? bug[1] : center) + CONTEXT_LINES)];
  const rows = [];
  for (let n = lo; n <= hi; n++) {
    const cls = bug && n >= bug[0] && n <= bug[1] ? " hot" : added.has(n) ? " added" : vw.focus === n ? " focus" : "";
    rows.push(h("span", { class: `line${cls}`, "data-line": n }, h("span", { class: "num", "aria-hidden": "true" }, n), h("span", { class: "src" }, lines[n - 1] || " "), "\n"));
  }
  const set = (patch) => { Object.assign(vw, patch); renderDetail(false); };
  const where = bug ? (bug[0] === bug[1] ? `bug on line ${bug[0]}` : `bug on lines ${bug[0]}–${bug[1]}`)
    : vw.focus ? `line ${vw.focus}` : added.size ? "changed lines in green" : "";
  const block = h("div", { class: `codeblock viewer${vw.whole ? " is-whole" : ""}` },
    h("div", { class: "codeblock-head viewer-head" },
      h("span", { class: "path" }, vw.file, where ? h("span", { class: "hint" }, `, ${where}`) : null),
      h("span", { class: "viewer-tools" },
        fixed ? segmented("Version", [["current", "Current code"], ["fixed", "With the fix"]], vw.mode, (mode) => set({ mode, focus: null })) : null,
        h("button", { class: "btn btn-quiet", type: "button", onclick: () => set({ whole: !vw.whole }) }, vw.whole ? "Show less" : `Whole file (${lines.length} lines)`),
        editorLink(run, vw.file, center))),
    h("pre", { tabindex: "0", "aria-label": `${vw.file}${vw.mode === "fixed" ? " with the fix applied" : ""}` }, h("code", {}, rows)));
  if (vw.whole) {
    requestAnimationFrame(() => {
      const pre = block.querySelector("pre");
      const target = block.querySelector(".line.hot, .line.added, .line.focus");
      if (pre && target) pre.scrollTop = Math.max(0, target.offsetTop - pre.clientHeight / 3);
    });
  }
  return h("section", { class: "section", id: "code-viewer" },
    h("h2", { class: "section-title" }, "Code"),
    files.length > 1 ? h("div", { class: "file-tabs", role: "tablist", "aria-label": "Files" },
      files.map((file) => h("button", { class: "file-tab", type: "button", role: "tab", "aria-selected": String(file === vw.file), title: file,
        onclick: () => set({ file, focus: null, mode: "current" }) }, file.split("/").pop()))) : null,
    block);
}

function excerptFallback(code) {
  const [a, b] = code.highlight;
  const rows = code.lines.map((text, i) => {
    const n = code.start + i;
    return h("span", { class: `line${n >= a && n <= b ? " hot" : ""}` }, h("span", { class: "num", "aria-hidden": "true" }, n), h("span", { class: "src" }, text || " "), "\n");
  });
  return h("section", { class: "section" }, h("h2", { class: "section-title" }, "Code"),
    h("div", { class: "codeblock" }, h("div", { class: "codeblock-head" }, h("span", { class: "path" }, code.file)),
      h("pre", { tabindex: "0" }, h("code", {}, rows))),
    h("p", { class: "hint" }, "This run was saved before whole-file previews existed. Run it again to preview the whole file and the fix."));
}

// ---------- detail: fix + apply ----------

function diffBlock(patch) {
  const lines = [];
  for (const line of patch.split("\n")) {
    if (line.startsWith("diff --git") || line.startsWith("--- ") || line === "") continue;
    if (line.startsWith("+++ ")) {
      lines.push(h("span", { class: "dline hunk" }, h("span", { class: "sign" }, ""), h("span", {}, line.slice(6)), "\n"));
      continue;
    }
    const kind = line.startsWith("@@") || line.startsWith("\\") ? "hunk" : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "ctx";
    const sign = kind === "add" ? "+" : kind === "del" ? "−" : "";
    const body = kind === "hunk" || kind === "ctx" ? line.replace(/^ /, "") : line.slice(1);
    lines.push(h("span", { class: `dline ${kind}` }, h("span", { class: "sign", "aria-hidden": "true" }, sign), h("span", {}, body || " "), "\n"));
  }
  return h("div", { class: "codeblock diff" }, h("pre", { tabindex: "0" }, h("code", {}, lines)));
}

async function downloadPatch(run, v) {
  try {
    const text = await api(`/api/runs/${encodeURIComponent(run.id)}/patch/${v.fingerprint}`);
    const url = URL.createObjectURL(new Blob([text], { type: "text/x-diff" }));
    const a = h("a", { href: url, download: `fix-${v.fingerprint}.patch` });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (e) {
    toast(e.message);
  }
}

async function fixAction(run, v, action) {
  const ap = state.apply;
  ap.busy = true;
  ap.error = null;
  renderDetail(false);
  try {
    ap.result = await api(`/api/fix/${action}`, { body: { run_id: run.id, fp: v.fingerprint, repo_path: ap.path } });
    if (action === "apply") {
      toast("Fix applied to your working copy");
      run.triage = { ...(run.triage || {}), [v.fingerprint]: { ...triageOf(v.fingerprint), status: "fixed" } };
      renderList();
    } else if (action === "undo") {
      toast("Fix removed from your working copy");
      run.triage = { ...(run.triage || {}), [v.fingerprint]: { ...triageOf(v.fingerprint), status: "open" } };
      renderList();
    }
  } catch (e) {
    ap.error = e.message;
    ap.result = null;
  }
  ap.busy = false;
  renderDetail(false);
}

function applyPanel(run, v) {
  const ap = state.apply;
  const pathInput = h("input", { class: "input", type: "text", value: ap.path || "", placeholder: "/Users/you/code/my-app", "aria-label": "Repository folder",
    oninput: (e) => { ap.path = e.target.value; ap.result = null; } });
  const r = ap.result;
  // The patch applies, but a file it changes differs from the code the fix was checked against.
  const drifted = Boolean(r && r.state === "applies" && r.changed_since_run && r.changed_since_run.length);
  return h("div", { class: "apply-panel", role: "region", "aria-label": "Apply the fix" },
    h("label", { class: "field-label" }, "Your local copy of the repository", pathInput),
    h("p", { class: "hint" }, "The patch goes into your working copy only. Nothing is committed or pushed."),
    h("div", { class: "actions" },
      h("button", { class: "btn", type: "button", disabled: ap.busy || !ap.path, onclick: () => fixAction(run, v, "check") }, "Check"),
      r && r.state === "applies" ? h("button", { class: "btn btn-primary", type: "button", disabled: ap.busy, onclick: () => fixAction(run, v, "apply") }, drifted ? "Apply anyway" : "Apply fix") : null,
      r && r.state === "applied" ? h("button", { class: "btn", type: "button", disabled: ap.busy, onclick: () => fixAction(run, v, "undo") }, "Undo fix") : null,
      h("button", { class: "btn btn-quiet", type: "button", onclick: () => { ap.open = false; renderDetail(false); } }, "Close"),
      ap.busy ? h("span", { class: "hint" }, "Working…") : null),
    r ? h("p", { class: `apply-result is-${drifted ? "warn" : r.state}`, role: "status" }, inline(r.detail)) : null,
    ap.error ? h("p", { class: "apply-result is-conflict", role: "alert" }, ap.error) : null);
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
  const fixFile = Object.keys(fix.patched || {})[0];
  if (!state.apply || state.apply.fp !== v.fingerprint) state.apply = { fp: v.fingerprint, open: false, path: run.local_path || "" };
  const ap = state.apply;
  const toggleApply = () => {
    ap.open = !ap.open;
    if (ap.open && ap.path && !ap.result) fixAction(run, v, "check");
    else renderDetail(false);
  };
  return h("section", { class: "section", id: "fix" },
    h("h2", { class: "section-title" }, "Fix"),
    h("p", { class: `fix-status fix-${fix.status}` }, h("strong", {}, status[0]), h("span", { class: "hint" }, status[1])),
    f.suggested_fix ? prose(f.suggested_fix) : null,
    h("div", { class: "fix-diff" }, diffBlock(fix.patch)),
    h("ul", { class: "checks" }, fix.notes.map((n) => h("li", { class: FAILED_NOTE.test(n) ? "bad" : "" }, n))),
    h("div", { class: "actions" },
      fix.status !== "failed" ? h("button", { class: "btn btn-primary", type: "button", "aria-expanded": String(Boolean(ap.open)), onclick: toggleApply }, "Apply to my repo") : null,
      fixFile && run.sources && run.sources[fixFile] !== undefined ? h("button", { class: "btn", type: "button", onclick: () => openInViewer(fixFile, null, "fixed") }, "Preview in the file") : null,
      h("button", { class: "btn", type: "button", onclick: () => copy(fix.patch, "Patch") }, "Copy patch"),
      h("button", { class: "btn", type: "button", onclick: () => downloadPatch(run, v) }, "Download patch")),
    ap.open ? applyPanel(run, v) : null);
}

// ---------- detail: evidence ----------

function fileButton(file, line) {
  return h("button", { class: "path-link", type: "button", title: "Show this file in the code viewer", onclick: () => openInViewer(file, line) },
    line ? `${file}, line ${line}` : file);
}

function codeBlock(title, lines, opts = {}) {
  return h("div", { class: `codeblock${opts.extra ? " " + opts.extra : ""}` },
    h("div", { class: "codeblock-head" }, title instanceof Node ? title : h("span", {}, title), opts.action || null),
    h("pre", { tabindex: "0" }, h("code", {}, lines)));
}

function evidenceSection(run, v) {
  const canOpen = (file) => file && run.sources && run.sources[file] !== undefined;
  const blocks = v.evidence.map((e) => {
    if (e.kind === "failing_test") {
      return [
        codeBlock("Test written by the agent and re-run by pr-review", h("span", { class: "plain" }, e.test_code || ""),
          { action: h("button", { class: "btn btn-quiet", type: "button", onclick: () => copy(e.test_code || "", "Test") }, "Copy test") }),
        codeBlock("Output on the code under review", h("span", { class: "plain" }, e.test_output || ""), { extra: "output" }),
      ];
    }
    if (e.kind === "test_regression") {
      return codeBlock(`Existing test ${e.test_id} now fails`, h("span", { class: "plain" }, e.test_output || "It passes on the base branch and fails with this change."), { extra: "output" });
    }
    if (e.kind === "static") {
      const title = h("span", {}, `${e.tool} ${e.rule || ""} at `, canOpen(e.file) ? fileButton(e.file, e.line) : `${e.file}:${e.line}`);
      return codeBlock(title, h("span", { class: "plain" }, "Reported only for this version of the code."), { extra: "output" });
    }
    if (e.kind === "code_reference") {
      return h("div", { class: "codeblock" },
        h("div", { class: "codeblock-head" }, canOpen(e.file) ? fileButton(e.file, e.line) : h("span", { class: "path" }, `${e.file}, line ${e.line}`)),
        h("pre", { tabindex: "0" }, h("code", {}, h("span", { class: "plain" }, e.snippet || ""))),
        e.why_relevant ? h("div", { class: "ref-why" }, inline(e.why_relevant)) : null);
    }
    return null;
  });
  return h("section", { class: "section" }, h("h2", { class: "section-title" }, "Evidence"),
    h("p", { class: "section-sub" }, "Checked by pr-review after the agent finished; nothing here is taken on trust. Click a file name to open it in the code viewer."),
    blocks);
}

function renderDetail(focus) {
  const pane = document.getElementById("detail");
  const run = state.run;
  const v = run && run.findings.find((x) => x.fingerprint === state.fp);
  if (!v) {
    pane.replaceChildren(run && run.findings.length
      ? h("div", { class: "empty" }, h("h2", {}, "Pick a bug"), h("p", {}, "Choose a finding to see its proof, code and fix."))
      : h("div", { class: "empty" }));
    return;
  }
  const f = v.finding;
  const animate = renderDetail.last !== v.fingerprint;
  renderDetail.last = v.fingerprint;
  const range = f.line_start === f.line_end ? `line ${f.line_start}` : `lines ${f.line_start}–${f.line_end}`;
  const scroll = pane.scrollTop;
  pane.replaceChildren(h("article", { class: "detail-inner" },
    h("button", { class: "back", type: "button", onclick: () => go(run.id) }, "Back to findings"),
    h("p", { class: "kicker" }, h("span", { class: `sev sev-text-${f.severity}` }, `${SEV_LABEL[f.severity]} severity`), `, ${f.category.replace("-", " ")} in ${f.project}`),
    h("h1", { class: "detail-title" }, f.title),
    h("p", { class: "where" }, `${f.file}, ${range}`),
    triageBar(run, v),
    trail(run, v, animate),
    prose(f.explanation),
    codeViewer(run, v),
    fixSection(run, v),
    evidenceSection(run, v),
    v.notes.length ? h("section", { class: "section" }, h("details", { class: "fold" }, h("summary", {}, "Verification notes"), h("ul", {}, v.notes.map((n) => h("li", {}, n))))) : null));
  if (animate) {
    pane.scrollTop = 0;
    if (focus) pane.focus({ preventScroll: true });
  } else {
    pane.scrollTop = scroll;
  }
}

// ---------- new scan page ----------

function newForm() {
  return {
    kind: "scan", path: "", checkedPath: null, info: null, infoError: null, checking: false, checkedAt: 0,
    budget: { scan: 5, "review-local": 2, review: 2 }, projects: [], uncommitted: false, base: "", head: "",
    recent: [], picker: false,
    ghRepos: null, ghRepo: "", ghError: null, prState: "open", prs: null, prsError: null, prsLoading: false,
    prNumber: null, prLink: "", post: false,
  };
}

async function checkFolder(raw) {
  const form = state.form;
  const path = String(raw || "").trim();
  if (!path) {
    form.infoError = "Choose or type the folder of a repository first.";
    return renderNewScan();
  }
  form.info = null;
  form.infoError = null;
  form.checking = true;
  renderNewScan();
  try {
    const info = await api(`/api/repo-info?path=${encodeURIComponent(path)}`);
    form.info = info;
    form.path = info.path;
    form.checkedPath = info.path;
    form.checkedAt = Date.now();
    form.projects = info.projects.filter((p) => p.enabled).map((p) => p.name);
    form.uncommitted = info.has_uncommitted;
    form.base = info.default_base;
    form.head = info.branches.includes(info.current_branch) && info.current_branch !== info.default_base
      ? info.current_branch : info.branches.find((b) => b !== info.default_base) || "";
    if (info.slug && !form.ghRepo) form.ghRepo = info.slug;
  } catch (e) {
    form.infoError = e.message;
    form.checkedPath = null;
  }
  form.checking = false;
  renderNewScan();
}

async function chooseFolder() {
  const form = state.form;
  form.picking = true;
  renderNewScan();
  try {
    const { path } = await api("/api/pick-folder", { body: {} });
    form.picking = false;
    if (path) {
      form.path = path;
      return checkFolder(path);
    }
  } catch (e) {
    form.infoError = e.message;
  }
  form.picking = false;
  renderNewScan();
}

async function loadPrs() {
  const form = state.form;
  if (!form.ghRepo) return;
  form.prsLoading = true;
  form.prsError = null;
  form.prs = null;
  form.prNumber = null;
  renderNewScan();
  try {
    form.prs = await api(`/api/github/prs?repo=${encodeURIComponent(form.ghRepo)}&state=${form.prState}`);
  } catch (e) {
    form.prsError = e.message;
  }
  form.prsLoading = false;
  renderNewScan();
}

async function loadGithub() {
  const form = state.form;
  if (form.ghRepos) return loadPrs();
  form.ghRepos = [];
  form.ghError = null;
  renderNewScan();
  try {
    form.ghRepos = await api("/api/github/repos");
    if (!form.ghRepo && form.ghRepos.length) form.ghRepo = form.ghRepos[0].slug;
  } catch (e) {
    form.ghError = e.message;
  }
  return loadPrs();
}

const PR_STATE = { open: "Open", merged: "Merged", closed: "Closed" };

function prPicker(form) {
  if (form.ghError) return h("p", { class: "field-error", role: "alert" }, form.ghError);
  const repoSelect = h("select", { class: "input", id: "gh-repo", onchange: (e) => { form.ghRepo = e.target.value; loadPrs(); } },
    (form.ghRepos || []).length
      ? form.ghRepos.map((r) => h("option", { value: r.slug, selected: r.slug === form.ghRepo ? true : null }, `${r.slug}${r.private ? " (private)" : ""}`))
      : h("option", { value: "" }, "Loading your repositories…"));
  let list;
  if (form.prsLoading) list = h("p", { class: "hint check-line" }, h("span", { class: "spin", "aria-hidden": "true" }), "Loading pull requests…");
  else if (form.prsError) list = h("p", { class: "field-error", role: "alert" }, form.prsError);
  else if (form.prs && !form.prs.length) {
    list = h("div", { class: "pr-empty" },
      h("p", {}, h("strong", {}, form.prState === "open" ? `No open pull requests in ${form.ghRepo}.` : `No pull requests in ${form.ghRepo} yet.`)),
      h("p", { class: "hint" }, "To review work that isn't in a pull request, use Review a branch. Or open a pull request on GitHub first."),
      h("div", { class: "actions" },
        form.prState === "open" ? h("button", { class: "btn", type: "button", onclick: () => { form.prState = "all"; loadPrs(); } }, "Show closed and merged") : null,
        h("button", { class: "btn", type: "button", onclick: () => { form.kind = "review-local"; form.error = null; renderNewScan(); } }, "Review a branch instead"),
        h("a", { class: "btn btn-quiet", href: `https://github.com/${form.ghRepo}/compare`, target: "_blank", rel: "noreferrer" }, "Open a pull request on GitHub")));
  } else if (form.prs) {
    list = h("div", { class: "pr-list", role: "radiogroup", "aria-label": "Pull requests" },
      form.prs.map((pr) => h("button", {
        type: "button", role: "radio", class: "pr-row", "aria-checked": String(form.prNumber === pr.number),
        onclick: () => { form.prNumber = pr.number; form.prLink = ""; renderNewScan(); },
      },
        h("span", { class: "pr-num" }, `#${pr.number}`),
        h("span", { class: "pr-main" }, h("strong", {}, pr.title),
          h("span", { class: "hint" }, `${pr.head} into ${pr.base}, by ${pr.author}, updated ${when(pr.updated_at)}`)),
        h("span", { class: `pr-state is-${pr.draft ? "draft" : pr.state}` }, pr.draft ? "Draft" : PR_STATE[pr.state] || pr.state))));
  }
  return [
    h("div", { class: "field" }, h("label", { class: "field-label", for: "gh-repo" }, "Repository on GitHub"), repoSelect),
    h("label", { class: "check" }, h("input", { type: "checkbox", checked: form.prState === "all",
      onchange: (e) => { form.prState = e.target.checked ? "all" : "open"; loadPrs(); } }),
      h("span", {}, "Include closed and merged pull requests")),
    h("div", { class: "field" }, h("span", { class: "field-label" }, "Pull request"), list || null),
    h("details", { class: "fold", open: form.prLink ? true : null }, h("summary", {}, "Or paste a pull request link"),
      h("input", { class: "input", id: "pr-link", type: "text", value: form.prLink, placeholder: "https://github.com/owner/repo/pull/123",
        oninput: (e) => { form.prLink = e.target.value.trim(); form.prNumber = null; updateSubmit(); } })),
  ];
}

function prTarget(form) {
  if (form.prLink) return form.prLink;
  return form.ghRepo && form.prNumber ? `${form.ghRepo}#${form.prNumber}` : "";
}

let updateSubmit = () => {};

async function renderNewScan() {
  const pane = document.getElementById("detail");
  if (!state.form) {
    state.form = newForm();
    const [recent, setup] = await Promise.all([api("/api/repos").catch(() => []), api("/api/setup").catch(() => ({}))]);
    state.form.recent = recent;
    state.form.picker = Boolean(setup.folder_picker);
    if (recent.length) {
      state.form.path = recent[0];
      return checkFolder(recent[0]);
    }
  }
  if (parseHash().page !== "new") return;
  const form = state.form;
  const info = form.info;
  const needsRepo = form.kind !== "review";
  const kinds = [["scan", "Scan a repository"], ["review-local", "Review a branch"], ["review", "Review a GitHub pull request"]];

  const pathInput = h("input", { class: "input", id: "repo-path", type: "text", list: "recent-repos", value: form.path,
    placeholder: "/Users/you/code/my-app", autocomplete: "off", spellcheck: "false",
    oninput: (e) => { form.path = e.target.value; },
    onkeydown: (e) => { if (e.key === "Enter") { e.preventDefault(); checkFolder(e.target.value); } } });
  let status = null;
  if (form.picking) status = h("p", { class: "hint check-line" }, h("span", { class: "spin", "aria-hidden": "true" }), "Waiting for you to choose a folder in the window that opened…");
  else if (form.checking) status = h("p", { class: "hint check-line" }, h("span", { class: "spin", "aria-hidden": "true" }), "Reading the repository…");
  else if (form.infoError) status = h("p", { class: "field-error", role: "alert" }, form.infoError);
  else if (info) {
    const usable = info.projects.filter((p) => p.enabled).length;
    status = h("p", { class: `check-ok${Date.now() - form.checkedAt < 700 ? " is-new" : ""}`, role: "status" },
      h("span", { class: "check-mark", "aria-hidden": "true" }, "✓"),
      h("span", {}, h("strong", {}, info.name), ` on ${info.current_branch}${info.has_uncommitted ? ", with uncommitted changes" : ""}. `,
        `${usable} of ${plural(info.projects.length, "project")} can be scanned${info.configured ? "" : ", detected automatically"}.`));
  }
  const repoField = h("div", { class: "field" },
    h("label", { class: "field-label", for: "repo-path" }, "Repository folder"),
    h("div", { class: "field-row" },
      pathInput,
      form.picker ? h("button", { class: "btn", type: "button", disabled: form.picking || null, onclick: chooseFolder }, "Choose folder…") : null,
      h("button", { class: "btn", type: "button", disabled: form.checking || null, onclick: () => checkFolder(pathInput.value) }, "Check folder")),
    h("datalist", { id: "recent-repos" }, (form.recent || []).map((p) => h("option", { value: p }))),
    status);

  const budget = h("div", { class: "field field-narrow" },
    h("label", { class: "field-label", for: "budget" }, "Usage limit"),
    h("div", { class: "money-input" }, h("span", { "aria-hidden": "true" }, "$"),
      h("input", { class: "input", id: "budget", type: "number", min: form.kind === "scan" ? "1" : "0.3", max: "100", step: "0.5", value: form.budget[form.kind],
        oninput: (e) => { form.budget[form.kind] = e.target.value; } })),
    h("p", { class: "hint" }, form.kind === "scan"
      ? "Measured at API prices. With a Claude login this comes out of your plan's usage, not your card. The riskiest code is reviewed first, and the scan stops at this amount. You can continue it afterwards."
      : "Measured at API prices. The review stops at this amount."));

  let fields = [];
  if (form.kind === "scan" && info) {
    fields = [
      h("fieldset", { class: "field" }, h("legend", { class: "field-label" }, "Projects to scan"),
        info.projects.map((p) => h("label", { class: `check${p.enabled ? "" : " is-disabled"}` },
          h("input", { type: "checkbox", checked: form.projects.includes(p.name), disabled: !p.enabled,
            onchange: (e) => { form.projects = e.target.checked ? [...form.projects, p.name] : form.projects.filter((x) => x !== p.name); updateSubmit(); } }),
          h("span", {}, h("strong", {}, p.name), `${p.path === "." ? " at the repository root" : p.path !== p.name ? ` in ${p.path}` : ""}, ${p.language}`,
            p.note ? h("span", { class: "hint" }, `, ${p.note}`) : null)))),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: form.uncommitted, onchange: (e) => { form.uncommitted = e.target.checked; } }),
        h("span", {}, "Include uncommitted changes", h("span", { class: "hint" }, ". Git-ignored files such as .env are never included."))),
    ];
  } else if (form.kind === "review-local" && info) {
    const branchSelect = (id, value, onPick) => h("select", { class: "input", id, onchange: (e) => { onPick(e.target.value); updateSubmit(); } },
      info.branches.map((b) => h("option", { value: b, selected: b === value ? true : null }, b)));
    fields = info.branches.length < 2
      ? [h("p", { class: "hint" }, `${info.name} has only one branch, so there's nothing to compare. Create a branch for your change, or scan the repository instead.`)]
      : [
          h("div", { class: "field-pair" },
            h("div", { class: "field" }, h("label", { class: "field-label", for: "base" }, "Compare against"), branchSelect("base", form.base, (b) => { form.base = b; })),
            h("div", { class: "field" }, h("label", { class: "field-label", for: "head" }, "Branch to review"), branchSelect("head", form.head, (b) => { form.head = b; }))),
          h("p", { class: "hint" }, "Only committed changes are reviewed. Commit your work to the branch first."),
        ];
  } else if (form.kind === "review") {
    fields = [
      ...prPicker(form),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: form.post, onchange: (e) => { form.post = e.target.checked; renderNewScan(); } }),
        h("span", {}, "Post the review on the pull request", h("span", { class: "hint" }, ". Leave this off to only see it here."))),
    ];
  }

  const readyReason = () => {
    if (form.kind === "review") return prTarget(form) ? null : "Choose a pull request above.";
    if (!info) return "Check a repository folder first.";
    if (form.kind === "scan" && !form.projects.length) return "Choose at least one project.";
    if (form.kind === "review-local" && (info.branches.length < 2 || form.base === form.head)) return "Choose two different branches.";
    return null;
  };
  const submitBtn = h("button", { class: "btn btn-primary btn-large", type: "button" }, form.kind === "scan" ? "Start scan" : "Start review");
  const submitHint = h("span", { class: "hint" });
  updateSubmit = () => {
    const why = readyReason();
    submitBtn.disabled = Boolean(why) || form.busy;
    const selected = (form.prs || []).find((pr) => pr.number === form.prNumber);
    submitHint.textContent = why
      || (form.kind === "review" && form.post ? `The review will be posted on GitHub when it finishes${selected && selected.state !== "open" ? ". This pull request is already " + selected.state : ""}.`
        : "Nothing is posted or changed in your repository.");
  };
  submitBtn.addEventListener("click", async () => {
    form.error = null;
    const body = { kind: form.kind, budget_usd: Number(form.budget[form.kind]) };
    if (form.kind === "scan") Object.assign(body, { repo_path: form.path, projects: form.projects, uncommitted: form.uncommitted });
    if (form.kind === "review-local") Object.assign(body, { repo_path: form.path, base: form.base, head: form.head });
    if (form.kind === "review") Object.assign(body, { target: prTarget(form), post: form.post });
    try {
      form.busy = true;
      updateSubmit();
      const job = await api("/api/jobs", { body });
      form.busy = false;
      state.jobs = [job, ...state.jobs.filter((j) => j.id !== job.id)];
      refreshJobs();
      location.hash = `#/jobs/${job.id}`;
    } catch (e) {
      form.busy = false;
      form.error = e.message;
      renderNewScan();
    }
  });
  updateSubmit();

  pane.replaceChildren(h("article", { class: pageClass() },
    h("a", { class: "back", href: "#/" }, "Overview"),
    h("h1", { class: "page-title" }, "New scan"),
    h("p", { class: "page-lead" }, "Runs in the background on this computer. You can keep browsing while it works."),
    segmented("What to do", kinds, form.kind, (kind) => {
      form.kind = kind;
      form.error = null;
      if (kind === "review") loadGithub();
      else renderNewScan();
    }, "kind-seg"),
    h("div", { class: "form" },
      needsRepo ? repoField : null,
      fields,
      needsRepo && !info ? null : budget,
      form.error ? h("p", { class: "field-error", role: "alert" }, form.error) : null,
      h("div", { class: "actions" }, submitBtn, submitHint))));
}

// ---------- overview (home) ----------

async function renderOverview() {
  const L = window.PRLanding;
  const pane = document.getElementById("detail");
  let ov = { totals: { runs: 0, proven: 0, possible: 0, fixes: 0, open: 0, usage_usd: 0 }, recent: [], story: null };
  try {
    ov = await api("/api/overview");
  } catch {
    /* show the page with the example below */
  }
  if (parseHash().page !== "home") return;
  const active = state.jobs.filter((j) => j.status === "running" || j.status === "queued");
  const proofHost = h("div", { class: "ov-proof" });
  const pipeHost = h("div", {});
  const stat = (value, label, cls, opts) => {
    const num = h("strong", { class: "ov-num" }, 0);
    requestAnimationFrame(() => L.countUp(num, value, opts));
    return h("div", { class: `ov-stat ${cls || ""}` }, num, h("span", {}, label));
  };
  const t = ov.totals;

  pane.replaceChildren(h("article", { class: pageClass("overview") },
    h("section", { class: "hero ov-hero" },
      h("div", {},
        h("h1", { class: "hero-title ov-title" }, "Every bug here comes with proof."),
        h("p", { class: "hero-lead" }, "Scan a repository or review a change. pr-review writes a failing test for each bug it suspects, re-runs it in a sandbox, and checks every fix against your own tests."),
        h("div", { class: "hero-actions" },
          h("a", { class: "btn btn-primary", href: "#/new" }, t.runs ? "New scan" : "Start your first scan"),
          h("a", { class: "btn", href: "#/guide" }, "Guide"))),
      active.length
        ? h("a", { class: "ov-live", href: `#/jobs/${active[0].id}` },
            h("span", { class: "pulse", "aria-hidden": "true" }),
            h("span", {}, h("strong", {}, active[0].title),
              h("span", { class: "hint" }, active[0].chunks_total
                ? `, ${active[0].chunks_done} of ${active[0].chunks_total} chunks reviewed, ${plural(active[0].proven, "bug")} proven so far`
                : `, ${jobStatusText(active[0]).toLowerCase()}`)),
            h("span", { class: "ov-live-go" }, "Watch"))
        : null,
      proofHost),
    t.runs ? h("section", { class: "section ov-stats-wrap" },
      h("h2", { class: "section-title" }, "Across your runs"),
      h("div", { class: "ov-stats" },
        stat(t.proven, t.proven === 1 ? "bug proven by a test" : "bugs proven by a test", "is-red"),
        stat(t.fixes, t.fixes === 1 ? "fix verified" : "fixes verified", "is-green"),
        stat(t.open, "still open"),
        stat(t.runs, t.runs === 1 ? "run" : "runs"),
        stat(t.usage_usd, "usage, at API prices", "", { prefix: "$", decimals: 2 }))) : null,
    h("section", { class: "section" },
      h("h2", { class: "section-title" }, "How a finding is made"),
      h("p", { class: "section-sub" }, "Suspicions are cheap. Only the ones a test can prove reach you."),
      pipeHost),
    ov.recent.length ? h("section", { class: "section" },
      h("h2", { class: "section-title" }, "Recent runs"),
      h("ol", { class: "ov-runs" }, ov.recent.map((run) => h("li", {},
        h("a", { href: `#/r/${encodeURIComponent(run.id)}` },
          h("span", { class: "ov-run-main" }, h("strong", {}, run.repo), h("span", { class: "hint" }, run.target)),
          h("span", { class: "ov-run-meta" },
            h("span", {},
              run.verified + run.possible ? h("span", { class: "hit" }, plural(run.verified + run.possible, "bug")) : "No bugs",
              run.fixes ? `, ${plural(run.fixes, "verified fix", "verified fixes")}` : ""),
            h("span", { class: "hint" }, `${KIND_LABEL[run.kind] || run.kind}, ${when(run.created_at)}`))))))) : null));

  let story;
  let caption;
  if (ov.story) {
    story = L.storyFromFinding(ov.story.finding, ov.story.source);
    const r = ov.story.run;
    caption = h("span", {}, `From your ${(KIND_LABEL[r.kind] || r.kind).toLowerCase()} of ${r.repo}, ${when(r.created_at)}. `,
      h("a", { href: `#/r/${encodeURIComponent(r.id)}/f/${ov.story.finding.fingerprint}` }, "Open it"));
  } else {
    story = L.storyFromFinding(L.DEMO.finding, L.DEMO.source);
    caption = "An example from a small test repository. Your own verified fixes appear here after your first scan.";
  }
  anims.push(L.mountProof(proofHost, story, { caption: "" }));
  proofHost.querySelector(".proof-caption span")?.replaceWith(caption instanceof Node ? caption : h("span", {}, caption));
  anims.push(L.mountPipeline(pipeHost));
}

// ---------- job page: live view ----------

let jobPoll = null;
function stopJobPolling() {
  clearTimeout(jobPoll);
  jobPoll = null;
}

const PHASES = [
  ["prepare", "Preparing a copy of the repository"],
  ["analyze", "Running checks and tests"],
  ["agent", "Claude is investigating"],
  ["verify", "Verifying what it found"],
  ["done", "Finished"],
];
const SCAN_PHASE = {
  queued: "Waiting to start",
  prepare: "Installing dependencies, running tests and checks",
  review: "Reviewing the riskiest code first",
  done: "Finished",
};
const CHUNK_STATE = {
  queued: "Waiting",
  reviewing: "Reviewing",
  done: "Reviewed",
  cached: "Reviewed before",
  failed: "Didn't finish",
  skipped: "Not reached",
};

// The pipeline's log lines, in words a person would use. null = a duplicate of the next line.
const ACTIVITY = [
  [/^Read (.+)/, (m) => `Reading ${m[1]}`],
  [/^Grep (.+)/, (m) => `Searching the code for “${m[1]}”`],
  [/^Glob (.+)/, (m) => `Listing files matching ${m[1]}`],
  [/^find_references (.+)/, (m) => `Looking for everything that uses ${m[1]}`],
  [/^static_findings/, () => "Checking the compiler and linter results"],
  [/^(run_repro_test|check_fix|run_existing_tests)\b/, () => null],
  [/^running repro test in (.+)/, (m) => `Running a test it wrote, in ${m[1]}`],
  [/^checking fix in \S+ \((.+)\)/, (m) => `Trying a fix to ${m[1]}`],
  [/^running (\d+) existing test file\(s\) in (.+)/, (m) => `Running ${m[1]} of your test files in ${m[2]}`],
  [/^\[\d+\/\d+\] [^:]+: (.+)/, (m) => `Reviewing ${m[1]}`],
  [/^preparing (.+?) \(/, (m) => `Installing ${m[1]} and running its tests and checks`],
  [/^(\d+) chunk\(s\) planned/, (m) => `Split the code into ${m[1]} chunks, riskiest first`],
  [/^analyzing (.+?) \(/, (m) => `Running checks and tests for ${m[1]}`],
  [/^agent reviewing/, () => "Claude is reading the change"],
  [/^verifying (\d+) proposed/, (m) => `Verifying ${m[1]} suspected ${m[1] === "1" ? "bug" : "bugs"}`],
];

function activityText(line) {
  const text = line.replace(/^\[\s*[\d.]+s\]\s*/, "");
  for (const [re, say] of ACTIVITY) {
    const m = text.match(re);
    if (m) return say(m);
  }
  return text;
}

async function renderJob(jobId) {
  const L = window.PRLanding;
  const pane = document.getElementById("detail");
  let job;
  try {
    job = await api(`/api/jobs/${jobId}`);
  } catch {
    pane.replaceChildren(h("div", { class: "empty" }, h("h2", {}, "This job isn't running here"),
      h("p", {}, "Jobs are kept only while the dashboard is open. Finished runs are listed on the left.")));
    return;
  }
  const logLines = [...job.log];
  const statusLine = h("p", { class: "job-status" });
  const actions = h("div", { class: "actions" });
  const meterFill = h("span", { class: "meter-spent" });
  const meterReserved = h("span", { class: "meter-reserved" });
  const meterText = h("span", { class: "meter-text" });
  const meter = h("div", { class: "meter" },
    h("div", { class: "meter-bar", role: "img", "aria-label": "Usage so far" }, meterFill, meterReserved), meterText);
  const counters = {};
  const counter = (key, label, cls) => {
    counters[key] = h("strong", {}, 0);
    return h("div", { class: `live-count ${cls || ""}` }, counters[key], h("span", {}, label));
  };
  const counterRow = h("div", { class: "live-counts" },
    counter("chunks", "chunks reviewed"), counter("tests", "tests run"),
    counter("proven", "bugs proven", "is-red"), counter("fixes", "fixes verified", "is-green"));
  const chunkMap = h("ol", { class: "chunk-map", "aria-label": "Chunks of the codebase, riskiest first" });
  const phaseList = h("ol", { class: "phases" }, PHASES.map(([key, label]) => h("li", { "data-phase": key }, h("span", { class: "phase-dot", "aria-hidden": "true" }), label)));
  const feed = h("ol", { class: "live-feed" });
  const feedEmpty = h("p", { class: "hint" }, "Nothing proven yet. Bugs appear here the moment a test proves them.");
  const feedTitle = h("h2", { class: "section-title" }, "Found so far");
  const activity = h("ol", { class: "activity" });
  const logCode = h("code", {});
  const pre = h("pre", { tabindex: "0", "aria-label": "Full log" }, logCode);
  const chunkTiles = [];
  let shownFindings = 0;

  const progressSection = h("section", { class: "section live-progress" });
  const continueHost = h("div", {});
  pane.replaceChildren(h("article", { class: pageClass("live") },
    h("a", { class: "back", href: "#/" }, "Overview"),
    h("h1", { class: "page-title" }, job.title),
    statusLine,
    actions,
    continueHost,
    meter,
    counterRow,
    progressSection,
    h("section", { class: "section" }, feedTitle, feedEmpty, feed),
    h("section", { class: "section" }, h("h2", { class: "section-title" }, "What it's doing"), activity,
      h("details", { class: "fold" }, h("summary", {}, "Full log"), h("div", { class: "codeblock job-log" }, pre)))));

  const paint = () => {
    const st = job.state || {};
    const active = job.status === "running" || job.status === "queued";
    // status + actions
    statusLine.className = `job-status job-${job.status}`;
    const phaseText = st.chunks ? SCAN_PHASE[st.phase] : (PHASES.find(([k]) => k === st.phase) || [])[1];
    fill(statusLine, active ? h("span", { class: "pulse", "aria-hidden": "true" }) : null,
      active ? `${phaseText || "Working"}, ${duration(job.elapsed_s)}` : jobStatusText(job), job.error ? `: ${job.error}` : "");
    fill(actions,
      active ? h("button", { class: "btn", type: "button", onclick: async () => {
        try { job = await api(`/api/jobs/${job.id}/cancel`, { body: {} }); paint(); } catch (e) { toast(e.message); }
      } }, "Cancel") : null,
      job.status === "done" && job.run_id ? h("a", { class: "btn btn-primary", href: `#/r/${encodeURIComponent(job.run_id)}` }, "Open results") : null,
      !active ? h("a", { class: "btn", href: "#/new" }, "Start another") : null);
    // usage meter
    const budget = st.budget || job.params.budget_usd || 0;
    meter.hidden = !budget;
    if (budget) {
      const spent = Math.min(st.spent || 0, budget);
      const reserved = Math.min(st.reserved || 0, budget - spent);
      meterFill.style.width = `${(spent / budget) * 100}%`;
      meterReserved.style.width = `${(reserved / budget) * 100}%`;
      meterText.textContent = `${money(st.spent)} of your ${money(budget)} limit used${reserved > 0.01 ? `, up to ${money(reserved)} more in progress` : ""}`;
    }
    // counters
    const chunks = st.chunks || [];
    const proven = (st.findings || []).filter((f) => f.tier === "verified").length;
    const fixes = (st.findings || []).filter((f) => f.fix === "verified").length;
    counters.chunks.parentElement.hidden = !chunks.length;
    // Chunks that didn't finish aren't counted, the same as in the saved run.
    const reviewed = chunks.filter((c) => c.state === "done" || c.state === "cached").length;
    if (job.kind === "scan" && job.status === "done" && job.run_id && reviewed < chunks.length && !continueHost.firstChild) {
      continueHost.append(continueBox(job.run_id, reviewed, chunks.length, job.params.budget_usd));
    }
    L.countUp(counters.chunks, reviewed);
    counters.chunks.nextSibling.textContent = `of ${chunks.length} chunks reviewed`;
    L.countUp(counters.tests, st.tests || 0);
    L.countUp(counters.proven, proven);
    L.countUp(counters.fixes, fixes);
    // chunk map (scans) or phase steps (reviews)
    if (chunks.length) {
      if (!chunkTiles.length) {
        progressSection.replaceChildren(h("h2", { class: "section-title" }, "The codebase, riskiest first"),
          h("p", { class: "section-sub" }, "Each tile is one piece of code the agent reviews on its own."), chunkMap);
        chunks.forEach((c, i) => {
          const tile = h("li", { class: "tile", title: c.files.join("\n") },
            h("span", { class: "tile-name" }, c.label), h("span", { class: "tile-state" }), h("span", { class: "tile-bugs" }));
          chunkTiles[i] = tile;
          chunkMap.append(tile);
        });
      }
      chunks.forEach((c, i) => {
        const tile = chunkTiles[i];
        if (!tile) return;
        tile.dataset.state = c.state;
        tile.querySelector(".tile-state").textContent = CHUNK_STATE[c.state] || c.state;
        tile.querySelector(".tile-bugs").textContent = c.proven ? plural(c.proven, "bug") : "";
      });
    } else if (!progressSection.firstChild || progressSection.firstChild !== phaseList) {
      progressSection.replaceChildren(phaseList);
    }
    if (!chunks.length) {
      const idx = PHASES.findIndex(([k]) => k === st.phase);
      [...phaseList.children].forEach((li, i) => {
        li.dataset.state = i < idx || st.phase === "done" ? "done" : i === idx && active ? "active" : "waiting";
      });
    }
    // findings feed: new ones slide in
    const found = st.findings || [];
    for (const f of found.slice(shownFindings)) {
      feed.append(h("li", { class: `feed-item sev-${f.severity}` },
        h("span", { class: "margin-bar", "aria-hidden": "true" }),
        h("span", {}, h("strong", {}, f.title),
          h("span", { class: "row-where" }, `${f.file}:${f.line}`),
          h("span", { class: "row-tags" },
            h("span", { class: `sev-text-${f.severity}` }, SEV_LABEL[f.severity]),
            h("span", { class: f.tier === "verified" ? "tag-verified" : "tag-possible" }, f.tier === "verified" ? "Proven" : "Possible"),
            f.fix === "verified" ? h("span", { class: "tag-fix" }, "Fix verified") : null))));
    }
    shownFindings = found.length;
    feedTitle.textContent = active ? "Found so far" : "What it found";
    feedEmpty.hidden = found.length > 0;
    if (!found.length && !active) {
      feedEmpty.textContent = job.status === "done" ? "No bugs were proven in this run." : "Nothing was proven.";
    }
    // activity + full log
    const recentLines = logLines.map(activityText).filter(Boolean).slice(-5);
    fill(activity, recentLines.map((l, i) => h("li", { class: i === recentLines.length - 1 && active ? "is-now" : "" }, l)));
    const stick = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
    fill(logCode, h("span", { class: "plain" }, logLines.join("\n") || "Waiting to start…"));
    if (stick) pre.scrollTop = pre.scrollHeight;
  };
  paint();

  const tick = async () => {
    if (parseHash().jobId !== jobId) return;
    try {
      const update = await api(`/api/jobs/${jobId}?since=${logLines.length}`);
      logLines.push(...update.log);
      const finishedNow = job.status !== update.status && update.status === "done";
      job = update;
      paint();
      if (finishedNow) {
        state.runs = await api("/api/runs");
        renderSidebar();
        toast("Finished. Open the results to see what it found.");
      }
    } catch {
      /* keep trying */
    }
    if (job.status === "running" || job.status === "queued") jobPoll = setTimeout(tick, 1000);
  };
  if (job.status === "running" || job.status === "queued") jobPoll = setTimeout(tick, 1000);
}

// ---------- guide page ----------

function cmd(text) {
  return h("div", { class: "cmd" }, h("code", {}, text),
    h("button", { class: "btn btn-quiet", type: "button", onclick: () => copy(text, "Command"), "aria-label": `Copy: ${text}` }, "Copy"));
}

async function renderGuide() {
  const pane = document.getElementById("detail");
  const setupBox = h("div", { class: "setup" }, h("p", { class: "hint" }, "Checking your setup…"));
  const editorPick = segmented("Open files in", [["vscode", "VS Code"], ["cursor", "Cursor"], ["none", "Don't show"]], state.editor,
    (value) => { state.editor = value; savePref("pr-review-editor", value); renderGuide(); });

  const steps = [
    ["Start a scan", [
      h("p", {}, "Click ", h("a", { href: "#/new" }, "New scan"), ", pick a repository folder (Choose folder… opens the Finder picker), choose the projects and a usage limit, then Start scan. Progress appears live; open the results when it finishes. The same page reviews a branch, or a pull request picked from your GitHub repositories."),
      h("p", {}, "If the limit runs out before every chunk is reviewed, the results offer Continue. It reviews only the chunks that are left, with a new limit, and the new run keeps everything already found. In a terminal, run the same command again."),
      h("p", {}, "From a terminal it's the same thing:"), cmd("pr-review scan ~/code/my-app --project backend --uncommitted --max-budget-usd 5")]],
    ["Read a finding", [
      h("p", {}, "Pick a bug in the middle column. The trail at the top shows how it was proven, in order: the test passes before the change, fails with it, passes with the fix, and the existing tests still pass. Red means failing, green means passing."),
      h("p", {}, "Proven bugs have a failing test the tool re-ran itself. Possible bugs are backed only by quoted code or a compiler error, so read the explanation carefully.")]],
    ["Preview the code", [
      h("p", {}, "The Code section shows the lines around the bug, marked in red. From there:"),
      h("ul", { class: "bullets" },
        h("li", {}, h("strong", {}, "Whole file"), " shows the entire file and scrolls to the bug."),
        h("li", {}, h("strong", {}, "With the fix"), " shows the file after the patch, with changed lines in green. ", h("strong", {}, "Current code"), " switches back."),
        h("li", {}, "When a finding touches several files, tabs above the code switch between them."),
        h("li", {}, "In Evidence, click any file name to open it in the viewer at that line."),
        h("li", {}, h("strong", {}, "Open in VS Code"), " jumps to the same line in your editor, for scans and branch reviews of a folder on this computer."),
        h("li", {}, "In the Fix section, ", h("strong", {}, "Preview in the file"), " opens the fixed file directly.")),
      h("div", { class: "pref" }, h("span", { class: "field-label" }, "Open files in"), editorPick)]],
    ["Apply a fix", [
      h("p", {}, "In the Fix section, click Apply to my repo. The dashboard checks the patch against your working copy and tells you whether it applies, is already applied, or no longer fits because the code changed. If it still applies but a file it changes is different from the code the fix was checked against, you get a warning first. Click Apply fix to write it, then review it with git diff. Undo fix takes it out again."),
      h("p", {}, "You can also copy or download the patch and apply it yourself:"), cmd("git apply fix-<id>.patch")]],
    ["Triage", [
      h("p", {}, "Mark each bug Open, Fixed, Won't fix or False alarm, and add a note. Applying a fix marks it Fixed. Use Open only in the filters to see what's left. Decisions carry over to later runs of the same repository.")]],
    ["Review every pull request automatically", [
      h("p", {}, "In the repository, write the config and a GitHub workflow, commit them, and add your Anthropic API key as a secret:"),
      cmd("pr-review init"), cmd("gh secret set ANTHROPIC_API_KEY"),
      h("p", {}, "Each review is also saved as a workflow artifact. Download it and open it here with pr-review ui --runs-dir <folder>.")]],
  ];

  pane.replaceChildren(h("article", { class: pageClass("guide") },
    h("a", { class: "back", href: "#" }, "All runs"),
    h("h1", { class: "page-title" }, state.runs.length ? "Guide" : "Get started"),
    h("p", { class: "page-lead" }, "pr-review finds bugs, proves each one with a test it runs itself, and checks every fix before showing it to you."),
    state.runs.length ? null : h("p", {}, h("a", { class: "btn btn-primary btn-large", href: "#/new" }, "Start your first scan")),
    setupBox,
    h("ol", { class: "guide-steps" }, steps.map(([title, body]) => h("li", {}, h("h2", {}, title), body))),
    h("section", { class: "section" }, h("h2", { class: "section-title" }, "Keyboard"),
      h("p", {}, h("kbd", {}, "j"), " / ", h("kbd", {}, "k"), " or the arrow keys move between findings."))));

  try {
    const s = await api("/api/setup");
    const row = (ok, label, detail) => h("li", { class: ok ? "ok" : "warn" }, h("strong", {}, label), h("span", {}, detail));
    setupBox.replaceChildren(h("h2", { class: "section-title" }, "Your setup"), h("ul", { class: "setup-list" },
      row(true, "Claude", s.anthropic_api_key ? "API key found." : "Using your Claude Code login. Set ANTHROPIC_API_KEY to use an API key instead."),
      row(s.github_login, "GitHub", s.github_login ? "Signed in, so pull requests can be reviewed." : "Not signed in. Run gh auth login to review pull requests."),
      row(true, "Sandbox", s.docker ? "Docker: tests run in isolated containers." : "Local: tests run on this computer with the network and your home folder blocked. Install Docker for full isolation."),
      row(true, "Runs are saved in", s.runs_dir)));
  } catch {
    setupBox.replaceChildren();
  }
}

// ---------- keyboard ----------

document.addEventListener("keydown", (e) => {
  if (!state.run || e.metaKey || e.ctrlKey || e.altKey) return;
  if (document.getElementById("app").dataset.mode !== "run") return;
  if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement || e.target instanceof HTMLSelectElement) return;
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
    [state.runs, state.jobs] = await Promise.all([api("/api/runs"), api("/api/jobs")]);
  } catch (e) {
    document.getElementById("detail").replaceChildren(h("div", { class: "empty" }, h("h2", {}, "Can't reach the dashboard server"),
      h("p", {}, `${e.message}. Start it again with pr-review ui, then reload this page.`)));
    return;
  }
  refreshJobs();
  route();
})();
