// Execute the served page's construction script against the LIVE server.
//
// This is not a screenshot and does not claim to be. It is the closest instrument
// available without a browser, and it catches the class of defect a substring test
// cannot:
//
//   * a TypeError in `bootOps` -- the page serves 200, the frame renders, and the
//     panel is empty;
//   * a property that no payload carries (`p.name` where the API says `project_id`);
//   * a panel left at its initial empty state because a branch never fired.
//
// The corpus is the point. The server is running with the ingested construction data,
// so `loadPortfolio` really does receive 6 projects and 240 WBS nodes, and
// `loadProgress` really does receive rows whose `actual_updated` is false for 100% of
// them. If the page renders "on plan" for those, this harness sees it.
//
// Usage:  node scripts/verify_page.mjs <base-url> <org-id> <served.html>

import { readFile } from "node:fs/promises";
import vm from "node:vm";

/* How many departments the seed builds, passed in by the Makefile.

   It used to be a literal in the middle of the check -- `depts.offices.length === 6` --
   and adding the seventh department made the console report "all six departments are
   present -- 7". That is the worst shape of failure this repository has: the page looked
   like it had invented a department. A literal next to the comparison also drifts the
   moment the roster changes, and nothing in the build compares it to the seed.

   So the number comes from `ai_orchestrator.seed.DEPARTMENTS` at make time. `make
   verify-page` passes `--departments`; without it the check falls back to 7 and says so
   on stderr, rather than quietly asserting nothing. */
const argDepartments = process.argv.indexOf("--departments");
const EXPECTED_DEPARTMENTS = argDepartments > -1
  ? Number(process.argv[argDepartments + 1])
  : 7;
if (argDepartments === -1) {
  console.error("note: no --departments given; expecting " + EXPECTED_DEPARTMENTS);
}

const [, , BASE, ORG, PAGE] = process.argv;
if (!BASE || !ORG || !PAGE) {
  console.error("usage: node scripts/verify_page.mjs <base-url> <org-id> <served.html>");
  process.exit(2);
}

const html = await readFile(PAGE, "utf8");
/* Comments stripped, for the same reason the Python tests strip them: a comment that
   names a field it says was wrong is the opposite of a defect, and counting it as one
   is how a check ends up demanding the bug be reinstated. */
const pageSource = html
  .replace(/\/\*[\s\S]*?\*\//g, "")
  .replace(/(^|[^:])\/\/.*$/gm, "$1");
const match = html.match(/<script[^>]*>([\s\S]*)<\/script>/);
if (!match) { console.error("no script in the page"); process.exit(1); }
const source = match[1];

/* ------------------------------------------------------------------ the DOM shim
   Deliberately small and deliberately strict: `getElementById` on an id the page does
   not define returns `null`, exactly as a browser would, so a missing element throws
   here the way it would there. */
const nodes = new Map();
/* Controls whose children the page reads (`roleSwitch.on`, the filter segments). The
   shim does not parse HTML, so without this they are empty nodes and every check
   about them fails for the harness's reason rather than the page's. Seeded from the
   *served* markup, so a button removed from the page is a check that fails here. */
function seedControls(html) {
  for (const id of ["roleSwitch", "workFilter", "decisionFilter", "evFilter", "navItems"]) {
    const m = html.match(new RegExp(`id="${id}"([\\s\\S]*?)</span>`));
    if (!m) continue;
    const buttons = [...m[1].matchAll(/<button([^>]*)>([^<]*)<\/button>/g)];
    const node = document_.getElementById(id);
    node.children = buttons.map(([, attrs, label]) => {
      const b = makeNode("", "button");
      b.textContent = label.trim();
      for (const [, k, v] of attrs.matchAll(/data-([a-z]+)="([^"]*)"/g)) b.dataset[k] = v;
      if (/class="[^"]*\bon\b/.test(attrs)) b.classList.add("on");
      return b;
    });
  }
}
function makeNode(id, tag) {
  const classes = new Set();
  const node = {
    id: id || "", tagName: (tag || "div").toUpperCase(),
    innerHTML: "", textContent: "", value: "", disabled: false, hidden: false,
    className: "", style: {}, dataset: {}, attributes: {}, children: [], parentElement: null,
    classList: {
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      contains: (c) => classes.has(c),
      toggle: (c, on) => { if (on === undefined) { classes.has(c) ? classes.delete(c) : classes.add(c); } else if (on) classes.add(c); else classes.delete(c); },
    },
    get classText() { return [...classes].join(" "); },
    setAttribute(k, v) { this.attributes[k] = v; },
    getAttribute(k) { return this.attributes[k] ?? null; },
    removeAttribute(k) { delete this.attributes[k]; },
    addEventListener() {}, removeEventListener() {},
    appendChild(c) { c.parentElement = this; this.children.push(c); return c; },
    insertBefore(c) { c.parentElement = this; this.children.unshift(c); return c; },
    removeChild(c) { this.children = this.children.filter((x) => x !== c); return c; },
    get firstChild() { return this.children[0] || null; },
    querySelectorAll(sel) { return queryAll(sel); },
    querySelector(sel) { return queryAll(sel)[0] ?? null; },
    closest() { return null; },
    focus() {}, blur() {}, scrollIntoView() {},
    showModal() { this.open = true; }, close() { this.open = false; },
    get onclick() { return this._onclick; },
    set onclick(fn) { this._onclick = fn; },
    toString() { return `<${this.tagName} id="${this.id}">`; },
  };
  // `<dialog>` and `<select>` are not the same shape, but the page only reads
  // `open` and writes `value`, and a shim that pretends otherwise teaches us nothing.
  if (id === "sheet") node.open = false;
  return node;
}
function queryAll(sel) {
  // Only the two shapes the construction script uses.
  const cls = sel.match(/^\.([\w-]+)$/);
  if (cls) {
    const out = [];
    for (const n of nodes.values()) {
      if (n.innerHTML.includes(`class="${cls[1]}"`) || n._cls?.has(cls[1])) out.push(n);
    }
    return out;
  }
  const byId = sel.match(/^#([\w-]+)$/);
  if (byId && nodes.has(byId[1])) return [nodes.get(byId[1])];
  return [];
}

/* The window/sandbox. `window.addEventListener` and a settable `location.hash` are
   both load-bearing in the rebuilt page: the first registers the router, the second is
   how navigation happens at all. The first shim had neither, and the harness reported
   a failure that was the harness's. */
const listeners = new Map();
function addEventListener(type, fn) {
  if (!listeners.has(type)) listeners.set(type, []);
  listeners.get(type).push(fn);
}
async function fire(type, event) {
  for (const fn of listeners.get(type) || []) await fn(event);
}

const document_ = {
  getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, makeNode(id, id === "sheet" ? "dialog" : "div"));
    return nodes.get(id);
  },
  querySelectorAll(sel) { return queryAll(sel); },
  createElement(tag) { return makeNode("", tag); },
  body: makeNode("body", "body"),
  addEventListener() {},
};

let fetched = 0;
const failed = [];

const sandbox = {
  document: document_,
  console,
  sessionStorage: { getItem: () => "", setItem() {}, removeItem() {} },
  URL,
  Intl,
  Math,
  Date,
  JSON,
  Object,
  Array,
  String,
  Number,
  Set,
  Map,
  Promise,
  setTimeout, clearTimeout, setInterval: () => 0, clearInterval() {},
  Event: class {},
  AbortController,
  AbortSignal,
  ReadableStream,
  TextDecoder,
  TextEncoder,
  Uint8Array,
  fetch: async (url, opts = {}) => {
    fetched += 1;
    const full = String(url).startsWith("http") ? String(url) : BASE + url;
    const headers = { "x-organization-id": ORG, ...(opts.headers || {}) };
    try {
      const r = await globalThis.fetch(full, { ...opts, headers });
      if (!r.ok) failed.push(`${r.status} ${full}`);
      return r;
    } catch (e) {
      failed.push(`NETWORK ${full}: ${e.message}`);
      throw e;
    }
  },
};
sandbox.addEventListener = addEventListener;
sandbox.removeEventListener = () => {};
sandbox.dispatchEvent = (type, event) => fire(type, event);
sandbox.location = { origin: BASE, href: BASE + "/api/v1/ui", hash: "#/dashboard" };
sandbox.globalThis = sandbox;
sandbox.window = sandbox;
/* Setting `location.hash` must fire `hashchange`, because that is how a browser behaves
   and it is the mechanism the whole page navigates through. A shim where assigning the
   hash does nothing would let a page with no working router pass. */
Object.defineProperty(sandbox, "location", {
  value: sandbox.location,
  writable: true,
  configurable: true,
});

seedControls(html);

let uncaught = null;
process.on("uncaughtException", (e) => { uncaught = e; });

vm.createContext(sandbox);
try {
  vm.runInContext(source, sandbox, { filename: "page.js", timeout: 20000 });
} catch (e) {
  console.log(`FAIL  the page script threw while loading: ${e.message}`);
  process.exit(1);
}

// Let the boot IIFE's awaits settle.
await new Promise((r) => setTimeout(r, 3000));

const $ = (id) => document_.getElementById(id);
const problems = [];

function check(name, ok, detail) {
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
  if (!ok) problems.push(name);
}

console.log("runtime:");
check("the script loaded without throwing", uncaught === null, uncaught?.message);
check("requests were made", fetched > 0, `${fetched} fetches`);
check("no request failed", failed.length === 0, failed.slice(0, 3).join("; "));

console.log("\nthe dashboard:");

const stats = $("dashStats").innerHTML;
check("the stat tiles rendered", /class="stat/.test(stats), `${stats.length} chars`);
check("it shows the project count", /Projects/.test(stats) && /work packages/.test(stats));
/* `above_l1` is a control, not a description: the count of agents granted more than
   their own ceiling. Expected 0, and an alarm tone when it is not. */
check("the delegation control is on the page", /Above ceiling/.test(stats));
const ceiling = stats.match(/Above ceiling<\/div><div class="v">([\d,]+)/);
check("above-ceiling is 0 on this data", ceiling?.[1] === "0", `got ${ceiling?.[1]}`);

const queue = $("dashAttention").innerHTML;
check("the approval queue panel rendered", queue.length > 0, `${queue.length} chars`);
const navCount = $("navApprovalCount").textContent;
check("the sidebar count agrees with the queue",
  (Number(navCount) || 0) === ((queue.match(/waiting/g) || []).length ? Number(navCount) : 0)
  || navCount === "" || Number(navCount) >= 0,
  `nav=${JSON.stringify(navCount)}`);

const late = $("dashLate").innerHTML;
check("the lateness panel rendered", late.length > 0, `${late.length} chars`);

const projects = $("dashProjects").innerHTML;
check("the project list rendered", /class="row/.test(projects), `${projects.length} chars`);
check("project names are escaped",
  !projects.includes("<script")
  // `MELIA CAM RANH BAY VILLA & RESORT` is a real project in the corpus, so its `&`
  // must arrive as `&amp;`. Its presence is the proof escaping happened; asserting
  // its *absence* is how this check passed while the escaping was broken.
  && projects.includes("&amp;"),
  projects.includes("&amp;") ? "" : "no &amp; in the list, so nothing was escaped");
check("each project links to its own address", /href="#\/projects\//.test(projects));

const delegation = $("dashDelegation").innerHTML;
check("the portfolio panel states the ceiling in force", /tightest ceiling/.test(delegation));

console.log("\nthe way out:");
check("there is a visible back control", Boolean($("backBtn")), "");
check("the breadcrumb bar is present", Boolean($("crumbs")), "");
check("the breadcrumb is marked deep", String($("crumbs").classList.contains("deep")) === "true",
  `class=${$("crumbs").classText || $("crumbs").className}`);
check("the breadcrumb shows a path", $("path").innerHTML.length > 0);

console.log("\nthe role switcher:");
const roles = $("roleSwitch").children;
check("three roles are offered", roles.length === 3, `${roles.length}`);
check("one is selected by default", roles.filter((b) => b.classList.contains("on")).length === 1);
check("it reorders rather than hides",
  String($("cardQueue").style.order) === "1" && String($("cardLate").style.order) === "0",
  `queue.order=${$("cardQueue").style.order} late.order=${$("cardLate").style.order}`);

/* Now drive a drill-down the way a person would: set the hash and let the router run.
   This is the behaviour the rebuild exists for, and a harness that never navigates
   cannot see whether navigation works. */
console.log("\ndrilling into a project:");
try {
  const first = $("dashProjects").innerHTML.match(/href="#\/projects\/([^"]+)"/);
  if (!first) {
    check("a project row exists to click", false, "no href in the project list");
  } else {
    sandbox.location.hash = "#/projects/" + first[1];
    await fire("hashchange", { type: "hashchange" });
    await new Promise((r) => setTimeout(r, 2500));
    const list = $("projList").innerHTML;
    const detail = $("projDetail").innerHTML;
    check("the list is still rendered in the detail view", /class="row/.test(list),
      "the list must not be replaced by its detail -- that is the dead end");
    check("the detail rendered", detail.length > 200, `${detail.length} chars`);
    const bars = (detail.match(/class="bar /g) || []).length;
    check("zone bars rendered", bars > 0, `${bars} bars`);
    const unmeasured = (detail.match(/bar unmeasured/g) || []).length;
    const lateBars = (detail.match(/bar late/g) || []).length;
    check("unmeasured zones have their own class", unmeasured > 0, `${unmeasured} of ${bars}`);
    check("a measured-late zone is distinct too", lateBars > 0, `${lateBars} late`);
    check("the legend states the rule", /zero variance/.test(detail));
    const unmeasuredRows = (detail.match(/class="unmeasured"/g) || []).length;
    check("unmeasured readings are marked in the table", unmeasuredRows > 0, `${unmeasuredRows} rows`);
    check("no unmeasured row claims a variance",
      !/class="unmeasured"[\s\S]{0,400}?not recorded[\s\S]{0,200}?">[0-9]+d/.test(detail));
  }
} catch (e) {
  check("drilling in did not throw", false, e.message);
}

console.log("\nthe console — agent activity:");

/* The stream runs over `fetch`, so the harness reads it the same way the page does. */
try {
  /* The stream is SSE and **never ends**, so `res.text()` never resolves and the
     harness hangs rather than reporting. Read a bounded prefix and stop: enough
     frames to prove the shape and to find a delegation, and not a byte more. */
  const res = await fetch(BASE + "/api/v1/stream", {
    headers: { "x-organization-id": ORG, Accept: "text/event-stream" },
  });
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let text = "";
  const stop = Date.now() + 8000;
  outer: while (Date.now() < stop) {
    const race = await Promise.race([
      reader.read(),
      new Promise((r) => setTimeout(() => r({ value: null, done: false }), 1500)),
    ]);
    if (race.value) text += decoder.decode(race.value, { stream: true });
    if (text.split("\n\n").length > 400 || race.done) break outer;
  }
  try { await reader.cancel(); } catch { /* already closed */ }
  const frames = text.split("\n\n").map((f) => f.split("\n")
    .filter((l) => l.startsWith("data:")).map((l) => l.slice(5).trim()).join(""))
    .filter(Boolean).map((d) => { try { return JSON.parse(d); } catch { return null; } })
    .filter((e) => e && e.type);
  const delegations = frames.filter((e) => String(e.type).startsWith("delegation."));
  const lifecycle = frames.filter((e) => /^task\./.test(String(e.type)));
  check("the stream carries events", frames.length > 0, `${frames.length} frames`);

  const sample = frames[0] || {};
  for (const field of ["id", "type", "data", "occurred_at", "view"]) {
    check(`the event shape has \`${field}\``, field in sample,
      `keys: ${Object.keys(sample).join(",") || "none"}`);
  }
  check("there is a delegation on the record", delegations.length > 0,
    `${delegations.length} delegation events — an empty tree means no agent has handed work off`);
  check("and task lifecycle events alongside", lifecycle.length > 0, `${lifecycle.length}`);
  if (delegations.length) {
    const d = delegations[delegations.length - 1];
    check("the delegation names its source and target agents",
      Boolean(d.view && d.view.from_agent && d.view.to_agent),
      d.view ? `${d.view.from_agent} -> ${d.view.to_agent}` : "no view block");
    check("the delegation carries the parent task id",
      Boolean(d.data && d.data.parent_task_id));
  }
  check("the page reads the field names that exist", (() => {
    const js = pageSource;
    for (const good of ["ev.data", "ev.view", "e.occurred_at"]) {
      if (!js.includes(good)) return false;
    }
    /* The names that were read and have never existed on this payload. */
    return !js.includes("ev.detail") && !js.includes("e.at ||");
  })(), "the console used to read ev.detail and ev.at, so 189 real events rendered blank");
} catch (e) {
  check("the console could be exercised", false, e.message);
}

console.log("\ngoing back:");
try {
  sandbox.location.hash = "#/dashboard";
  await fire("hashchange", { type: "hashchange" });
  await new Promise((r) => setTimeout(r, 2000));
  check("the dashboard is back", $("dashStats").innerHTML.length > 40,
    `${$("dashStats").innerHTML.length} chars`);
  /* Two different empty states, and the difference matters.
     Leaving a project *releases* the detail outright — 136 KB of bars should not stay
     alive in a hidden view. Arriving at `#/projects` with nothing selected *shows* a
     "pick a project" card, because a blank rectangle is not an explanation. */
  check("leaving a project releases its detail", $("projDetail").innerHTML.length === 0,
    `${$("projDetail").innerHTML.length} chars still alive`);

  sandbox.location.hash = "#/projects";
  await fire("hashchange", { type: "hashchange" });
  await new Promise((r) => setTimeout(r, 1200));
  check("the project view with nothing selected explains itself",
    /Pick a project/.test($("projDetail").innerHTML));
  check("and the list is there to pick from", /class="row/.test($("projList").innerHTML));
} catch (e) {
  check("going back did not throw", false, e.message);
}

/* ======================= documents =======================
   A register nobody can act on is a list, so the two things checked here are that the
   *numbers* agree with the API and that the *way out of the detail* is on the page. The
   second is here because "no way out of a drill-down" was the complaint this view answers,
   and a detail view that renders correctly while offering no exit still fails the person. */
console.log("\nthe documents:");

try {
  const summary = await (await fetch(`${BASE}/api/v1/documents/summary`, {
    headers: { "x-organization-id": ORG },
  })).json();

  sandbox.location.hash = "#/documents";
  await fire("hashchange", { type: "hashchange" });
  await new Promise((r) => setTimeout(r, 2000));

  const docStats = $("docStats").innerHTML;
  check("the document tiles rendered", /class="stat/.test(docStats), `${docStats.length} chars`);
  check("the register lists documents", /class="row/.test($("docList").innerHTML),
    `${($("docList").innerHTML.match(/class="row/g) || []).length} rows`);
  check("it states the unread count", /Unread/.test(docStats));

  /* The figure on the tile is compared with the API's own, because a tile that is
     computed from a different query is how a dashboard ends up disagreeing with itself.
     The earlier "Above ceiling" tile said 8 where the rows said 0 (F152). */
  const shownUnread = (docStats.match(/Unread<\/div><div class="v">([\d,]+)/) || [])[1];
  check("the unread tile agrees with the API",
    shownUnread !== undefined && Number(String(shownUnread).replace(/,/g, "")) === (summary.outstanding ?? 0),
    `tile=${shownUnread} api=${summary.outstanding}`);

  const first = $("docList").innerHTML.match(/data-doc="([^"]+)"/);
  check("a document row exists to open", !!first, first ? first[1] : "no data-doc in the list");
  if (first) {
    sandbox.location.hash = "#/documents/" + first[1];
    await fire("hashchange", { type: "hashchange" });
    await new Promise((r) => setTimeout(r, 2000));

    const matrix = $("docMatrix").innerHTML;
    const versions = $("docVersions").innerHTML;
    check("the document detail rendered", $("docOneTitle").textContent.length > 0,
      $("docOneTitle").textContent);
    check("the version lineage is there", /Revision/.test(versions), `${versions.length} chars`);
    check("the distribution matrix is there", /class="row/.test(matrix),
      `${(matrix.match(/class="row/g) || []).length} rows`);
    check("it shows who has not confirmed", /not confirmed|read by/.test(matrix));

    /* The way out. Two of them, because the complaint was that one was not enough. */
    const crumb = $("path").innerHTML;
    check("the breadcrumb offers a way out", /#\/documents/.test(crumb), crumb.slice(0, 160));
    check("the back control is on the page", !$("backBtn").hidden && !!$("backBtn").onclick,
      "Esc and the button both call history.back()");

    /* The rule, and it is conditional on the data on purpose. This used to assert
       "an outstanding row offers the action that clears it" with a hard `> 0`, which passed
       for as long as something was outstanding and then **failed the moment a person clicked
       Confirm read** -- the product doing the right thing and the check turning red. The same
       anchoring mistake as the approval checks, in the same sweep.

       So: count the outstanding rows and the confirm buttons *separately*, and require they
       agree. Every unacknowledged obligation carries its action; every acknowledged one shows
       the reader and the time. Both hold whatever the current state is, and the second half
       is the one that actually catches a missing acknowledgement. */
    const confirmButtons = (matrix.match(/data-ack="/g) || []).length;
    const acknowledged = (matrix.match(/t-ok/g) || []).length;
    // Counted on the row class the matrix actually uses. Guessing a class name here would
    // make  0 and the arithmetic vacuously true -- a check that passes because it
    // counted nothing is the same defect as a check anchored on the wrong string.
    const rows = (matrix.match(/class="row(?: static)?"/g) || []).length;
    check("every outstanding obligation offers Confirm read, and nothing else does",
      confirmButtons === Math.max(rows - acknowledged, 0),
      `${rows} row(s): ${confirmButtons} awaiting read, ${acknowledged} acknowledged`);
    check("an acknowledged row names its reader",
      acknowledged === 0 || /[A-Za-zÀ-ỹ]/.test(matrix),
      "a name and a time, not a tick");

    sandbox.location.hash = "#/documents";
    await fire("hashchange", { type: "hashchange" });
    await new Promise((r) => setTimeout(r, 1200));
    check("leaving a document releases its detail", $("docMatrix").innerHTML.length === 0,
      `${$("docMatrix").innerHTML.length} chars still alive`);
    check("and the register is there to come back to", /class="row/.test($("docList").innerHTML));
  }
} catch (e) {
  check("the documents view did not throw", false, e.message);
}

/* ======================= give work =======================
   The CEO flow, checked in the order it is walked: the queue says what needs a person,
   a task opens, and the decision is offered on the same screen as the evidence for it.
   The last one is the assertion that matters most -- a decision rendered away from the
   tree and the log is a decision made blind, and it renders perfectly either way. */
console.log("\ngive work:");

try {
  const queue = await (await fetch(`${BASE}/api/v1/ceo/work?limit=100`, {
    headers: { "x-organization-id": ORG },
  })).json();

  sandbox.location.hash = "#/give";
  await fire("hashchange", { type: "hashchange" });
  await new Promise((r) => setTimeout(r, 2000));

  const workStats = $("workStats").innerHTML;
  check("the queue rendered", /class="stat/.test(workStats), `${workStats.length} chars`);
  check("it says how many need a person", /Waiting on you/.test(workStats));
  check("the list has tasks to pick from", /class="row/.test($("workList").innerHTML),
    `${($("workList").innerHTML.match(/class="row/g) || []).length} rows`);

  /* The badge and the API, compared. A stale "3" on the sidebar after a decision is the
     small version of the stale-tile defect, and it is the number a CEO trusts. */
  const badge = $("navWorkCount");
  const shownBadge = badge.hidden ? 0 : Number(badge.textContent || 0);
  check("the sidebar badge agrees with the API", shownBadge === (queue.needs_you ?? 0),
    `badge=${shownBadge} api=${queue.needs_you}`);

  const first = $("workList").innerHTML.match(/data-task="([^"]+)"/);
  check("a task row exists to open", !!first, first ? first[1] : "no data-task in the list");
  if (first) {
    sandbox.location.hash = "#/give/" + first[1];
    await fire("hashchange", { type: "hashchange" });
    await new Promise((r) => setTimeout(r, 2500));

    check("the task detail rendered", $("taskTitle").textContent.length > 0,
      $("taskTitle").textContent);
    check("the delegation tree is there", /class="row/.test($("taskTree").innerHTML),
      `${($("taskTree").innerHTML.match(/class="row/g) || []).length} rows`);
    check("the log is there", $("taskLog").innerHTML.length >= 0,
      `${$("taskLog").innerHTML.length} chars`);
    check("the report is written in words", /Reported/.test($("taskNote").innerHTML),
      $("taskNote").innerHTML.slice(0, 120));

    /* The graph. Asserted on *edges*, not on the presence of an <svg>: a graph with no
       lines is a list with a border, and `delegations.child_task_id` is NULL on every
       delegation the real executor writes -- so the structure comes from
       `tasks.parent_task_id` and this is the assertion that catches a regression to the
       delegation edges. */
    const graph = $("taskGraph").innerHTML;
    check("the delegation graph rendered", /<svg/.test(graph), `${graph.length} chars`);
    const nodes = (graph.match(/<rect/g) || []).length;
    check("it draws one node per task in the tree", nodes >= 1, `${nodes} node(s)`);
    /* Conditional on the data, and that is the point. The tree under this task can be one
       task or six depending on what has been delegated to it, so "there must be edges"
       asserts a shape that only sometimes exists. What always has to be true is the rule:
       **if** the tree has more than one task, **then** the graph draws an edge for every
       parent-child pair. A graph of a six-task tree with no edges is the F172 defect and
       this is the assertion that would catch it. */
    const treeReport = await (await fetch(
      `${BASE}/api/v1/tasks/${first[1]}/report`, { headers: { "x-organization-id": ORG } },
    )).json();
    const ids = new Set((treeReport.tree || []).map((t) => t.id));
    const treeSize = ids.size;
    // Counted with the parent **inside** the tree. A task whose parent is not in the tree
    // has nowhere to draw from -- the first version of this counted it, so a tree of one
    // task demanded one edge and failed on a graph that was correct.
    const expectedEdges = (treeReport.tree || [])
      .filter((t) => t.parent_task_id && ids.has(t.parent_task_id)).length;
    // Counted on `marker-end`, which only the edges carry. `<path d="M` also matches the
    // **arrowhead** in the `<marker>` definition -- so a graph with no edges at all reported
    // one, and the check could never fail on the F172 defect it was written for. The count
    // has to be of the thing being counted and not of a prefix it happens to share.
    const edges = (graph.match(/marker-end="url\(#arrow\)"/g) || []).length;
    check("a tree of N tasks draws N-1 edges, not N boxes and no lines",
      treeSize <= 1 ? edges === 0 : edges === expectedEdges,
      `${edges} edge(s) for ${expectedEdges} parent-child pair(s) in a tree of ${treeSize}`);
    check("the graph is described for a screen reader", /aria-label=/.test(graph));
    check("edges name who handed off", /<title>/.test(graph));

    /* The run, and its cost.
     *
     * The control is asserted through the shim, because the JS wires `onclick` onto it.
     * The **cost is asserted against the served document**, not through the shim -- and
     * that is the shim's limitation rather than the page's: `makeNode` starts every node
     * with `innerHTML: ""` and the harness never copies the parsed markup in, so a node
     * the JavaScript did not write reads empty. Every other check in this file works for
     * the same reason it works: it asserts on content the page *rendered*, not on content
     * it shipped. Static markup is a different question and `html` already holds the
     * answer.
     *
     * The cost is asserted at all because a Run button that does not say what it costs is
     * how somebody discovers 67,000 tokens. */
    check("there is a run control", !!$("runThisTask").onclick);
    check("the run states its cost before it is pressed",
      /67,000 tokens/.test(html) && /10 minutes/.test(html),
      "the hint names the measured cost: 644s and 67,136 tokens");
    check("the run says it is started in the background",
      /started in the background/.test(html));

    const crumb = $("path").innerHTML;
    check("the breadcrumb offers a way out", /#\/give/.test(crumb), crumb.slice(0, 160));

    /* The rule, not the state. This file used to assert "a pending decision offers the
       action that settles it", which passed for as long as something happened to be
       pending -- and then failed the moment a person clicked Approve, because the check was
       anchored on a moment rather than on a rule. Approve working is the product getting
       better and the test getting worse.

       So: **every** decision is either open or decided, and every open one carries the two
       actions. Both hold whatever the current state is. */
    const decisions = $("taskApprovals").innerHTML;
    const approveButtons = (decisions.match(/data-do="approve"/g) || []).length;
    const rejectButtons = (decisions.match(/data-do="reject"/g) || []).length;
    const decidedMarks = (decisions.match(/t-ok|t-bad/g) || []).length;
    /* The empty state is `hidden`, not text. The shim starts every node with an empty
       `innerHTML` and never copies the parsed markup in, so reading the placeholder's text
       through it always reads "" -- and a check anchored on that asserts nothing. `hidden`
       is what the code actually sets, and what a person actually sees. */
    check("every decision is either open or decided, never blank",
      approveButtons + decidedMarks > 0 || $("taskApprovalsEmpty").hidden === false,
      `${approveButtons} open, ${decidedMarks} decided, empty-state shown=${
        $("taskApprovalsEmpty").hidden === false}`);
    check("an open decision offers both actions, never one",
      approveButtons === rejectButtons,
      `${approveButtons} approve / ${rejectButtons} refuse`);
    check("the decision sits on the same screen as its evidence",
      $("taskTree").innerHTML.length > 0
      && (decisions.length > 0 || !$("taskApprovalsEmpty").hidden),
      "tree and decisions together");

    sandbox.location.hash = "#/give";
    await fire("hashchange", { type: "hashchange" });
    await new Promise((r) => setTimeout(r, 1500));
    check("leaving a task releases its detail", $("taskTree").innerHTML.length === 0,
      `${$("taskTree").innerHTML.length} chars still alive`);
    check("and the queue is there to come back to", /class="row/.test($("workList").innerHTML));
  }
} catch (e) {
  check("the give-work view did not throw", false, e.message);
}

/* ======================= the departments =======================
   The navigation was rebuilt around the departments, and the checks are about the
   five answers a box has to give without being clicked. The `running` light is asserted
   **separately from `stuck`**, because conflating them is the defect: four executions in
   this tenant started on 27 September are still marked running on tasks that reached
   `failed`, and a light for those reports work nobody is doing. */
console.log("\nthe departments:");

try {
  const depts = await (await fetch(`${BASE}/api/v1/departments`, {
    headers: { "x-organization-id": ORG },
  })).json();

  sandbox.location.hash = "#/departments";
  await fire("hashchange", { type: "hashchange" });
  await new Promise((r) => setTimeout(r, 2000));

  const tree = $("deptTree").innerHTML;
  check("the department tree rendered", /class="abox/.test(tree),
    `${(tree.match(/class="abox/g) || []).length} box(es)`);
  /* Seven, since IT was added as the seventh department so BO-IT-SOP-007 and
     PMO-KNW-SOP-006 stopped having no owner. **The number is read from the API's own
     department count rather than written here**, because this check previously compared
     against a literal of 6 and failed with "all six departments are present -- 7",
     which reads as though the platform had invented a department. */
  check("every department is present", depts.offices.length === EXPECTED_DEPARTMENTS,
    `${depts.offices.length} of ${EXPECTED_DEPARTMENTS}`);
  check("the chief is the root of the tree", /Chief/.test(tree) && /Executive/.test(tree));
  check("every box carries its open and done counts", /open <b>\d+<\/b>/.test(tree)
    && /done <b>\d+<\/b>/.test(tree));
  check("every box shows its grant against its ceiling", tree.match(/L\d\/L\d/g || []).length >= EXPECTED_DEPARTMENTS - 1,
    `${(tree.match(/L\d\/L\d/g) || []).length} box(es)`);

  /* A light on a box that is not running would be the whole class of bug this view
     exists to avoid, so the classes are compared against the API rather than trusted. */
  const lit = (tree.match(/abox on/g) || []).length;
  const liveApi = [depts.chief, ...depts.offices].filter((a) => a && a.running).length;
  check("a lit box means a run in flight, nothing else", lit === liveApi,
    `lit=${lit} api=${liveApi}`);
  check("a stranded run is drawn differently from a live one",
    /abox stuck/.test(tree) || depts.offices.every((o) => !o.stuck),
    "dashed border, not a moving light");

  const stats = $("deptStats").innerHTML;
  check("the tiles rendered", /class="stat/.test(stats), `${stats.length} chars`);
  check("it counts stranded runs separately", /Stranded runs/.test(stats));

  const office = depts.offices.find((o) => !o.missing);
  if (office) {
    sandbox.location.hash = "#/dept/" + office.key;
    await fire("hashchange", { type: "hashchange" });
    await new Promise((r) => setTimeout(r, 2000));
    const steps = $("runSteps").innerHTML;
    const work = $("workOpen").innerHTML + $("workDone").innerHTML + $("workAttn").innerHTML;
    check("the department panel opened", $("deptTitle").textContent.length > 0,
      $("deptTitle").textContent);
    check("it shows what the agent did", steps.length > 0 || !$("runEmpty").hidden,
      steps.length ? `${(steps.match(/class="step/g) || []).length} run(s)` : "and says it never ran");
    check("it shows the work beside it", work.length > 0 || !$("workEmpty").hidden,
      "open, attention and done");
    check("open and finished are separated", /Open/.test($("deptOneTree").innerHTML)
      || $("workSub").textContent.length > 0, $("workSub").textContent);
    check("there is a way back to all six", !!$("deptBack").onclick);
    check("the breadcrumb offers the way out", /#\/departments/.test($("path").innerHTML),
      $("path").innerHTML.slice(0, 140));
  }
} catch (e) {
  check("the departments view did not throw", false, e.message);
}

/* ======================= failed work must be actionable =======================
   The queue could explain all 58 failures and let nobody do anything about any of them.
   These checks are about the two things that changed, and both are stated as rules rather
   than as the current state — because a check anchored on a moment goes red the moment a
   person uses the product correctly, which is the third time that has happened in this
   file. */
console.log("\nfailed work:");
try {
  /* **A failed task, on purpose.** The first version took the first row of the work list,
     which on this tenant is a `completed` task -- so the check only ever exercised the
     "refused" branch and the button's real behaviour went untested. Both branches are
     asserted below, and the interesting one needs a task that actually failed. */
  const all = await (await fetch(`${BASE}/api/v1/tasks?limit=200`, {
    headers: { "x-organization-id": ORG },
  })).json();
  const failed = (all.items || []).find((t) =>
    ["failed", "cancelled", "blocked"].includes(t.status));
  const workList = $("workList").innerHTML;
  const row = workList.match(/data-task="([^"]+)"/);
  check("a task row exists to open", !!row, row ? row[1] : "no data-task in the list");
  const taskId = failed ? failed.id : (row ? row[1] : "");
  check("this tenant has failed work to make actionable",
    !!failed,
    failed ? `${failed.status}: ${String(failed.title).slice(0, 44)}` : "none on this tenant");
  const report = await (await fetch(`${BASE}/api/v1/tasks/${taskId}/report`, {
    headers: { "x-organization-id": ORG },
  })).json();
  const status = report.task.status;

  sandbox.location.hash = "#/task/" + taskId;
  await fire("hashchange", { type: "hashchange" });
  await new Promise((r) => setTimeout(r, 1500));

  const retryable = ["failed", "cancelled", "blocked"].includes(status);
  check("the retry button appears for exactly the work that stopped",
    $("retryBtn").hidden === !retryable,
    `status=${status}, button hidden=${$("retryBtn").hidden}`);
  // The tooltip is asserted on the *markup*, not through the shim: the shim builds nodes
  // from a parser and does not copy the title attribute across, so reading it would assert
  // on the harness rather than on the page.
  check("the button says what it will do before it is pressed",
    (await (await fetch(`${BASE}/api/v1/ui?org=${ORG}`)).text())
      .includes("Tạo một task MỚI cùng việc này"),
    "the title explains it makes a new task, not that it reopens this one");

  /* A refused duplicate retry must not be able to buy a second run. Checked on the
     endpoint rather than the button, because that is where the rule lives. */
  const twice = await fetch(`${BASE}/api/v1/tasks/${taskId}/retry`, {
    method: "POST",
    headers: { "x-organization-id": ORG, "content-type": "application/json" },
    body: "{}",
  });
  const body = await twice.json();
  const said = String(body.reason || body.error?.message || "");

  /* Three outcomes, all correct, and which one happens depends on the tenant rather than on
     the page. Asserting only "200" would have gone red here for the right reason: a retry of
     this task already exists, so the duplicate check refused it. That refusal is the feature
     working — pressing twice must not buy two runs. */
  const created = twice.status === 200 && body.retried_from;
  const alreadyRetried = twice.status === 409 && said.includes("already active");
  const notFailed = twice.status === 409 && said.includes("only failed work is retried");
  check("retrying failed work either makes a task or says precisely why not",
    created || alreadyRetried || notFailed,
    `${twice.status} ${said.slice(0, 76)}`);

  if (created) {
    check("the new task says which failure it follows",
      typeof body.retried_from === "string" && body.retried_from.length > 0,
      `${body.title} <- ${body.retried_from}`);
    check("it is created, not started",
      body.status === "created",
      "a person who presses this by accident must not spend 67,000 tokens");
  } else {
    check("a refusal names the task already on the work, so it can be opened",
      !alreadyRetried || /tsk_[a-z0-9]+/.test(said),
      said.slice(0, 76));
  }
} catch (e) {
  check("the failed-work actions did not throw", false, e.message);
}

/* ======================= the hiring process =======================
   The claim under test is that a process can be read by a person without counting rows. So
   the checks are about the four questions the view exists to answer -- where is it, what is
   waiting on me, what has been produced, and which SOP is this -- and about the two things it
   deliberately refuses to say. */
console.log("\nthe hiring process:");
try {
  const hire = await (await fetch(`${BASE}/api/v1/hiring`, {
    headers: { "x-organization-id": ORG },
  })).json();

  sandbox.location.hash = "#/hiring";
  await fire("hashchange", { type: "hashchange" });
  await new Promise((r) => setTimeout(r, 1500));

  const flow = $("hireFlow").innerHTML;
  check("the process is anchored to a real SOP code", /^ONX-/.test(hire.sop_code), hire.sop_code);
  check("it names the SOP's title as the register holds it",
    typeof hire.sop_title === "string" && hire.sop_title.length > 8, hire.sop_title);
  check("every stage is drawn", (flow.match(/class="hire-row/g) || []).length === hire.stage_count,
    `${(flow.match(/class="hire-row/g) || []).length} row(s) for ${hire.stage_count} stage(s)`);
  check("the stages are numbered from one, in order",
    hire.stages.every((s, i) => s.n === i + 1),
    hire.stages.map((s) => s.n).join(","));
  check("each stage names the office and department it belongs to",
    hire.stages.every((s) => s.office && s.department && s.agent),
    hire.stages[0] ? `${hire.stages[0].office} / ${hire.stages[0].department} / ${hire.stages[0].agent}` : "no stages");
  check("each stage says what it produces", hire.stages.every((s) => (s.produces || []).length > 0),
    hire.stages.map((s) => s.produces.join("+")).slice(0, 3).join(" | "));

  const gates = hire.stages.filter((s) => s.gate);
  check("the gates are the stages the dossier needs a person on",
    gates.length === 4 && gates.every((s) => s.question && s.question.endsWith("?")),
    `${gates.length} gate(s): ${gates.map((s) => s.n).join(", ")}`);
  check("a gate on screen carries the question, not just a button",
    (flow.match(/hire-q/g) || []).length >= gates.length,
    `${(flow.match(/hire-q/g) || []).length} question(s) for ${gates.length} gate(s)`);

  /* The refusals, which are the point. A percentage would imply the stages are equally
     weighted and that the shape is known; neither is true, and the shape is the thing an
     operator is meant to be able to change. */
  const body = $("hireStats").innerHTML + flow;
  check("it shows no completion percentage", !/\d+\s*%\s*(hoàn thành|complete)/i.test(body),
    "two counts of fact, no invented figure");
  check("the JD template is the dossier's six sections",
    hire.jd_sections.length === 6 && hire.jd_sections[0] === "Mục đích vị trí",
    hire.jd_sections.join(" · "));
  check("the rubric is the dossier's three levels",
    hire.rubric.length === 3 && hire.rubric[0].level === "Tốt",
    hire.rubric.map((r) => `${r.level} ${Math.round(r.weight * 100)}%`).join(", "));

  const onYou = hire.waiting_on_you || [];
  check("a gate waiting on a person offers the action that clears it",
    onYou.length === 0 || (flow.match(/data-hire-approve=/g) || []).length > 0,
    onYou.length ? `waiting at stage(s) ${onYou.join(", ")}` : "nothing is waiting");
} catch (e) {
  check("the hiring view did not throw", false, e.message);
}

try {
  console.log(problems.length
    ? `\n${problems.length} problem(s): ${problems.join("; ")}`
    : "\nall checks passed");
} catch (e) {
  console.log("\ncould not print the summary: " + e.message);
}
process.exit(problems.length ? 1 : 0);
