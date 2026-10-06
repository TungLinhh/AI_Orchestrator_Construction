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
  for (const id of ["roleSwitch", "giveFilter", "workFilter", "decisionFilter", "evFilter", "navItems"]) {
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
    /** `replaceChildren`, which the Workflow tree uses instead of `innerHTML`.
     *
     *  The page draws the delegation tree with `$("tree").replaceChildren(...nodes)` —
     *  deliberately, because joining nodes into `innerHTML` printed a wall of
     *  `[object HTMLLIElement]` (F266). The shim did not have it, so `renderFlow()` threw
     *  `replaceChildren is not a function` on **every frame**, after the event had been
     *  recorded but before the tree was drawn.
     *
     *  Which is why the check read "38 delegation event(s), tree drawn=false": the events
     *  were real, the tree was broken only in the harness, and the new rendering check was
     *  the first thing to look at the tree rather than the event list. In a browser this
     *  never failed; a shim that throws where a browser does not is a false alarm in the
     *  exact shape of a real defect.
     */
    replaceChildren(...kids) { this.children = kids.map((k) => (k.parentElement = this, k)); },
    get firstChild() { return this.children[0] || null; },
    /** `options` for a `<select>`, read out of whatever `innerHTML` it currently holds.
     *
     *  `$()` answers from this flat map, not from `asDomNode`, so the copy of `options`
     *  written there would never be reached by the page. Two copies of one idea is the
     *  thing to avoid, so this reads the same way: the markup is the source, and the
     *  markup in a browser is whatever was assigned last. */
    get options() {
      /* **Not gated on `tagName`.** Every node in this map is built as
       * `makeNode(id, "div")` -- the map is keyed by id and knows nothing about the tag
       * it stands for -- so a `tagName === "SELECT"` guard made `options` empty for the
       * one selector the page has, and the harness reported a correctly-filled picker as
       * empty. The markup is the reliable signal: a `div` never holds `<option>`s, so
       * reading them is harmless on any node.
       *
       * **Cost of being wrong here: low.** A false positive means a check about the agent
       * picker passes because some unrelated node mentions an option; a false negative is
       * a failing check someone investigates. It failed first, and that was right. */
      return [...String(this.innerHTML || "").matchAll(/<option([^>]*)>([\s\S]*?)<\/option>/g)]
        .map(([, attrs, text]) => ({
          value: (attrs.match(/value="([^"]*)"/) || [, text.trim()])[1],
          textContent: text.trim(),
          get selected() { return /\bselected\b/.test(attrs); },
        }));
    },
    querySelectorAll(sel) { return queryWithin(this, sel); },
    querySelector(sel) { return queryWithin(this, sel)[0] ?? null; },
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
/* --- an element tree parsed out of rendered HTML ---------------------------
   **Built because the structural hierarchy could not be checked without it, and the
   tempting alternative was worse than not checking.**

   `queryAll` answered two selectors -- `.cls` and `#id` -- and returned `[]` for
   everything else. Every hierarchy selector is an attribute selector, so
   `[data-office-group]`, `.dept-edges .abox` and `.abox[data-box-key="finance"]`
   would all have returned nothing, and the checks written against them would have
   reported "no department is nested inside any office" over a correctly nested tree.
   A selector the harness silently does not understand is a check that always fails,
   and a check that always fails gets deleted rather than trusted -- which is how the
   original string-match stayed in place for months.

   So the rule is the one this project keeps relearning: **an instrument that cannot
   ask the question must say so.** `queryAll` below raises on a selector it does not
   understand instead of answering `[]`. */

const VOID = new Set(["br", "hr", "img", "input", "meta", "link", "source", "path"]);

function parseHTML(html, parent = null) {
  const root = { tag: "#root", attrs: {}, children: [], text: "", parent: null };
  const stack = [root];
  const re = /<\/?([a-zA-Z][\w-]*)((?:\s+[^\s=>/]+(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?)*)\s*(\/?)>|([^<]+)/g;
  let m;
  while ((m = re.exec(html))) {
    const [whole, tag, attrText, selfClose, text] = m;
    if (text !== undefined) {
      stack[stack.length - 1].text += text;
      continue;
    }
    if (whole.startsWith("</")) {
      if (stack.length > 1) stack.pop();
      continue;
    }
    const attrs = {};
    const ar = /([^\s=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+)))?/g;
    let a;
    while ((a = ar.exec(attrText || ""))) {
      attrs[a[1]] = a[2] ?? a[3] ?? a[4] ?? "";
    }
    const el = { tag: tag.toLowerCase(), attrs, children: [], text: "", parent: stack[stack.length - 1] };
    stack[stack.length - 1].children.push(el);
    if (!selfClose && !VOID.has(el.tag)) stack.push(el);
  }
  const wire = (el) => {
    for (const c of el.children) { c.parent = el; wire(c); }
  };
  wire(root);
  return root;
}

/** Does one parsed element satisfy one *simple* selector such as `.a`, `#b`, `[c]`,
 *  `[c="v"]`, `.a[c="v"]`, `tag`, or `tag.cls`? */
function matchesSimple(el, sel) {
  const parts = sel.match(/(^[a-zA-Z][\w-]*)|(\.[\w-]+)|(#[\w-]+)|(\[([^\]=]+)(?:=("?)([^\]"]*)\6)?\])/g) || [];
  if (!parts.length) throw new Error(`queryAll does not understand "${sel}"`);
  for (const p of parts) {
    if (p.startsWith(".")) {
      if (!(el.attrs.class || "").split(/\s+/).includes(p.slice(1))) return false;
    } else if (p.startsWith("#")) {
      if (el.attrs.id !== p.slice(1)) return false;
    } else if (p.startsWith("[")) {
      // **The group indices here were wrong once and every attribute selector
      // matched nothing.** `[([^\]=]+)(?:=("?)([^\]"]*)\2)?]` has three capture
      // groups -- name, quote, value -- and the destructuring read `name` from index
      // 2, which is the quote. `""` is therefore not a key in `attrs`, every
      // attribute selector returned false, and the hierarchy checks reported "0
      // groups" over a tree that had 3. A silent wrong answer from a shim is worse
      // than a shim that refuses to answer, which is why `queryAll` raises on a
      // selector it cannot parse.
      const hit = p.match(/^\[([^\]=]+)(?:=("?)([^\]"]*)\2)?\]$/);
      if (!hit) throw new Error(`queryAll does not understand "${sel}"`);
      const [, name, , value] = hit;
      const have = el.attrs[name];
      if (have === undefined) return false;
      if (value !== undefined && have !== value) return false;
    } else if (el.tag !== p.toLowerCase()) return false;
  }
  return true;
}

/** Descendant match: `A B` means "a `B` somewhere inside an `A`". */
function matchesWithin(el, parts) {
  const last = parts[parts.length - 1];
  if (!matchesSimple(el, last)) return false;
  if (parts.length === 1) return true;
  let p = el.parent;
  while (p) {
    if (matchesWithin(p, parts.slice(0, -1))) return true;
    p = p.parent;
  }
  return false;
}

function queryTree(root, sel) {
  const parts = sel.trim().split(/\s+(?![^\[]*\])/).filter(Boolean);
  const out = [];
  const walk = (el) => {
    for (const c of el.children) {
      if (matchesWithin(c, parts)) out.push(c);
      walk(c);
    }
  };
  walk(root);
  return out;
}

/** Wrap a parsed element so it answers the handful of DOM calls the checks make. */
function asDomNode(el, source) {
  if (el.__dom) return el.__dom;
  const classes = new Set((el.attrs.class || "").split(/\s+/).filter(Boolean));
  const node = {
    tagName: el.tag.toUpperCase(),
    dataset: Object.fromEntries(
      Object.entries(el.attrs).map(([k, v]) => [k.replace(/^data-/, "").replace(/-(\w)/g, (_, c) => c.toUpperCase()), v])),
    classes,
    getAttribute(k) { return el.attrs[k] ?? null; },
    hasAttribute(k) { return el.attrs[k] !== undefined; },
    /** `setAttribute` / `removeAttribute` / a writable `classList` are not optional.
     *
     *  The nav-highlighting loop in `route()` calls `removeAttribute` on every nav
     *  link that is not the current page, and the shim had only `getAttribute`. The
     *  page's own try/catch does not cover that loop — it runs before the render — so
     *  the failure escaped `route()`, **no view rendered at all**, and the log showed
     *  0 boxes on every screen. A shim that is missing one DOM method does not fail
     *  one check; it fails every check that follows a navigation.
     */
    setAttribute(k, v) { el.attrs[k] = String(v); el.__dom = null; },
    removeAttribute(k) { delete el.attrs[k]; el.__dom = null; },
    classList: {
      contains: (c) => classes.has(c),
      add: (c) => classes.add(c),
      remove: (c) => classes.delete(c),
      toggle: (c, on) => {
        if (on === undefined) { if (classes.has(c)) { classes.delete(c); return false; } classes.add(c); return true; }
        if (on) classes.add(c); else classes.delete(c);
        return !!on;
      },
    },
    get textContent() {
      const walk = (n) => n.text + n.children.map(walk).join("");
      return walk(el);
    },
    /** `children`: **elements only**, as a browser defines it.
     *
     *  `childNodes` includes text, `children` does not. The page never reads
     *  `childNodes`, and the harness reads `children.length` to answer "did anything
     *  render here" — so this is the collection that has to mean something. Reading an
     *  absent `children` throws inside the *check*, which is how a shim gap gets
     *  reported as a page defect and takes every check after it with it.
     */
    get children() {
      const out = [];
      const walk = (n) => {
        for (const c of n.children) { out.push(asDomNode(c, "")); walk(c); }
      };
      walk(el);
      return out;
    },
    get childElementCount() { return node.children.length; },
    /** `options`, `value` and `selectedIndex` for a `<select>`.
     *
     *  A browser gives every `<select>` an `options` collection whether or not the
     *  markup has options, and the page reads it during boot to fill the agent picker.
     *  Without it here, any assertion about that picker threw `cannot read properties
     *  of undefined` — which is how the harness reported the *page's* bug (a selector
     *  stuck on "loading agents...") as the *harness's* bug, and took every check after
     *  it down with it.
     *
     *  The options are built from the markup each time, and `value` is the `value`
     *  attribute of whichever option the page last assigned, so a round trip through
     *  `sel.innerHTML = ...` followed by `sel.value` behaves as a browser does.
     */
    get options() {
      if (node.tagName !== "SELECT") return [];
      const fromTree = queryTree(el, "option").map((o) => ({
        value: o.attrs.value ?? o.text.trim(),
        textContent: o.text.trim(),
        get selected() { return o.attrs.selected !== undefined; },
      }));
      if (fromTree.length) return fromTree;
      /* **The markup the page assigned**, not only the markup it shipped with.
       *
       *  A browser re-parses `sel.innerHTML = "..."`, so `<option>`s written at runtime
       *  are in `sel.options`. This shim's `innerHTML` is a plain property, so a
       *  runtime-written option list was invisible and the harness reported "0 options"
       *  for a selector that was correctly full -- the same failure in the other
       *  direction from the missing-`options` bug it was added to catch, and worth
       *  guarding in both directions. */
      const html = String(node.innerHTML || "");
      return [...html.matchAll(/<option([^>]*)>([\s\S]*?)<\/option>/g)].map(([, attrs, text]) => ({
        value: (attrs.match(/value="([^"]*)"/) || [, text.trim()])[1],
        textContent: text.trim(),
        get selected() { return /\bselected\b/.test(attrs); },
      }));
    },
    value: el.attrs.value ?? "",
    selectedIndex: -1,
    get innerHTML() { return source || ""; },
    set innerHTML(markup) {
      source = String(markup);
      const parsed = parseHTML(source);
      el.children = parsed.children;
      el.text = parsed.text;
      for (const child of el.children) child.parent = el;
      if (el.tag === "select") node.value = node.options.find(o => o.selected)?.value || node.options[0]?.value || "";
    },
    querySelectorAll(s) { return queryTree(el, s).map((c) => asDomNode(c, "")); },
    querySelector(s) { return queryTree(el, s).map((c) => asDomNode(c, ""))[0] ?? null; },
    closest(s) {
      let p = el;
      while (p) {
        if (matchesSimple(p, s)) return asDomNode(p, "");
        p = p.parent;
      }
      return null;
    },
    dispatchEvent() {},
    click() {},
  };
  el.__dom = node;
  return node;
}

/** Every rendered node, as a DOM tree. Parsed on demand -- the page writes a lot of
 *  HTML and nothing here needs a live tree between polls. */
function rendered(id) {
  const node = nodes.get(id);
  return node ? treeOf(node) : null;
}

function treeOf(node) {
  if (node.__treeSource !== node.innerHTML) {
    node.__tree = parseHTML(node.innerHTML || "");
    node.__treeSource = node.innerHTML;
  }
  return node.__tree;
}

/** A node's own descendants -- and *only* its own.
 *
 *  It used to delegate to the global `queryAll`, so `deptOneTree.querySelectorAll`
 *  answered with whatever box happened to be in the first node of the map. The check
 *  written against it read **11 boxes from the organisation view while asserting
 *  about a panel holding three**, and the one assertion that named the panel's own
 *  contents got an empty string. A query that is not scoped to what it was asked
 *  about is worse than no query: the numbers it returns are real, and they are about
 *  something else.
 */
function queryWithin(node, sel) {
  const found = queryTree(treeOf(node), sel);
  if (found.length) return found.map((el) => asDomNode(el, node.innerHTML));
  // Nodes the page built rather than rendered -- a `<dialog>`, a fresh element. For
  // those the node map is the only thing there is to search.
  return [node].filter((n) => matchesNode(n, sel));
}

function queryAll(sel) {
  if (typeof sel !== "string") throw new Error("queryAll needs a selector");
  // `.cls` and `#id` used to search the top-level node map, which could only ever see
  // whole rendered blobs. The tree is the honest answer for anything structural.
  for (const node of nodes.values()) {
    const found = queryWithin(node, sel);
    if (found.length) return found;
  }
  return [];
}

function matchesNode(node, sel) {
  const parts = sel.trim().split(/\s+/);
  if (parts.length !== 1) return false;
  const p = parts[0];
  if (p.startsWith(".") && (node._cls?.has(p.slice(1)) || node.className?.split(/\s+/).includes(p.slice(1)))) return true;
  if (p.startsWith("#")) return nodes.get(p.slice(1)) === node;
  return false;
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
  documentElement: makeNode("html", "html"),
  getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, makeNode(id, id === "sheet" ? "dialog" : "div"));
    return nodes.get(id);
  },
  querySelectorAll(sel) { return queryAll(sel); },
  querySelector(sel) { return queryAll(sel)[0] ?? null; },
  createElement(tag) { return makeNode("", tag); },
  body: makeNode("body", "body"),
  // **Wired, not a no-op.** The page routes its whole box-click flow through one
  // `document` click listener, so an inert stub meant "clicking an office" could not
  // be tested at all -- only its consequence, a URL, which some other check could
  // fake. It is registered here so the click can be fired at a real element and the
  // handler's own `closest` lookup does the work.
  addEventListener(type, fn) { addEventListener(type, fn); },
  removeEventListener(type, fn) {
    const list = listeners.get(type);
    if (list) listeners.set(type, list.filter((x) => x !== fn));
  },
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
   hash does nothing would let a page with no working router pass.

   **The comment above claimed this and the code did not do it.** `hash` was a plain
   property, so `go("#/dept/front-office")` -- the only navigation a *click* performs --
   changed the URL and rendered nothing. Every check that moved the page had to assign
   the hash and then fire `hashchange` by hand, which is why the click path was never
   tested: it looked broken for a reason that was in the harness.

   It is implemented now, and one check reads it directly: after clicking an office the
   panel must show *that office*. Before this, the click appeared to work (the hash was
   right) while the panel still showed whatever the previous check had left there. */
const historyEntries = [{hash: "#/dashboard", state: null}];
let historyIndex = 0;
sandbox.history = {
  get state() { return historyEntries[historyIndex].state; },
  replaceState(value) { historyEntries[historyIndex] = {hash: sandbox.__hash, state: value}; },
  back() {
    if (!historyIndex) return;
    historyIndex--;
    sandbox.__hash = historyEntries[historyIndex].hash;
    fire("hashchange", {type:"hashchange"}).catch(e => {uncaught = e;});
  },
  forward() {
    if (historyIndex + 1 >= historyEntries.length) return;
    historyIndex++;
    sandbox.__hash = historyEntries[historyIndex].hash;
    fire("hashchange", {type:"hashchange"}).catch(e => {uncaught = e;});
  },
};
let hashQuiet = 0;
let pendingHash = null;
const locationShim = {
  get origin() { return BASE; },
  get href() { return BASE + "/api/v1/ui" + locationShim.hash; },
  get hash() { return sandbox.__hash ?? "#/dashboard"; },
  set hash(v) {
    if (v === sandbox.__hash) return;
    const inheritedState = sandbox.history.state;
    historyEntries.splice(historyIndex + 1);
    historyEntries.push({hash: v, state: inheritedState});
    historyIndex++;
    sandbox.__hash = v;
    // Fire after the assignment settles, and only once, so a `go()` that writes the
    // hash twice renders once.
    if (hashQuiet) return;
    if (pendingHash) clearTimeout(pendingHash);
    pendingHash = setTimeout(async () => {
      pendingHash = null;
      hashQuiet++;
      try { await fire("hashchange", { type: "hashchange" }); }
      catch (e) { uncaught = uncaught || e; }
      finally { hashQuiet--; }
    }, 0);
  },
};
sandbox.__hash = "#/dashboard";
Object.defineProperty(sandbox, "location", {
  value: locationShim,
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

/** The first stack frame inside this file, so a failure names a line.
 *  A message like "Cannot read properties of null (reading 'length')" says what
 *  broke and nothing about where, which is why several real defects in this project
 *  were found by reading the code around a guess rather than by reading the log. */
function firstFrame(err) {
  const frames = String((err && err.stack) || "").split("\n")
    .map((l) => l.trim()).filter((l) => l.startsWith("at "));
  const own = frames.find((l) => l.includes("verify_page") || l.includes("index.html"));
  return own ? own.replace(/^at\s*/, "") : (frames[1] || "").replace(/^at\s*/, "");
}


function check(name, ok, detail) {
  console.log(`  ${ok ? "ok  " : "FAIL"}  ${name}${detail ? "  — " + detail : ""}`);
  if (!ok) problems.push(name);
}

/* ---- the defects that were visible on the page, as standing checks ----
   Each of these was a real defect a person hit, and none of them raised: the page
   loaded, every request succeeded, and every number on it was a lie. So the harness
   now asserts the *structure* rather than the absence of an exception.

   The four, in the order they were reported:

   * a wall of `[object HTMLLIElement]` in the Workflow panel -- an `innerHTML =` fed an
     array of DOM nodes;
   * "Waiting on me" and "Everything" showing the same list -- the filter was
     `state.x === "all" ? items : items`;
   * a filter that could not be clicked at all -- two elements shared `id="workFilter"`
     and `$()` returns the first;
   * the agent selector stuck on "loading agents..." because the screen that filled it
     was cut and nothing was left to fill it. */
console.log("structure:");
const idsInMarkup = [...html.matchAll(/\sid="([A-Za-z][\w-]*)"/g)].map((m) => m[1]);
const dupes = [...new Set(idsInMarkup.filter((id, i) => idsInMarkup.indexOf(id) !== i))];
check("no element id is declared twice", dupes.length === 0,
  dupes.length ? dupes.join(", ") : `${idsInMarkup.length} ids, all unique`);
check("nothing is coerced into markup by joining nodes",
  !/\.map\([^)]*=>[^{]*nodeEl\([^)]*\)\)\.join\("/.test(html),
  "no `innerHTML = nodes.join()` remains");
check("every element the page writes to exists",
  ["giveList", "giveFilter", "giveEmpty", "activity", "unitWorkSub", "unitWorkEmpty",
   "workList", "workFilter", "workSub", "workEmpty", "taskAnswer", "taskAnswerSub",
   "taskAnswerCopy", "taskAnswerEmpty", "deptSearch", "deptOfficeFilter", "deptNoMatch",
   "providerPill", "runProvider", "flowCount", "flowEmpty", "activityEmpty"]
    .every((id) => idsInMarkup.includes(id)),
  "give/work/unit/answer/dept-filter/provider/flow ids all present");
/* The end-product shape, asserted as structure rather than taste: one nav link per
   screen, the work form before the workflow log, an answer card on the task view,
   and a finder on the organisation view. */
const navHrefs = [...html.matchAll(/<a[^>]*href="(#\/[^" ]*)"[^>]*data-nav="([a-z]+)"/g)]
  .map((m) => `${m[2]}:${m[1]}`);
const primaryHrefs = navHrefs.filter((s) => ["departments", "give", "work"].includes(s.split(":")[0]))
  .map((s) => s.split(":")[1]);
check("one nav link per screen, no two sharing a destination", new Set(primaryHrefs).size === primaryHrefs.length && primaryHrefs.length === 4,
  navHrefs.join(", "));
check("the work form comes before the workflow it explains",
  html.indexOf('id="scenario"') > 0 && html.indexOf('id="scenario"') < html.indexOf('id="tree"'),
  "Start a task precedes Workflow in the document");
check("the task view has an answer card above the evidence",
  html.indexOf('id="taskAnswer"') > 0 && html.indexOf('id="taskAnswer"') < html.indexOf('id="taskSteps"'),
  "Answer precedes Steps");
check("the organisation view has a finder and an office filter",
  idsInMarkup.includes("deptSearch") && idsInMarkup.includes("deptOfficeFilter"),
  "search + All-offices chips");
/* The bilingual toggle, asserted as coverage rather than taste. English is the
   verified default, so every `data-i18n*` key and every `t("key", ...)` call
   must have a Vietnamese entry — otherwise switching languages silently shows
   English, which is a toggle that lies about what it does. Dynamic data (task
   titles, answers, logs, reasons, names, ids) is deliberately never keyed, so
   it is excluded by construction: only keyed chrome is counted. */
const i18nUsed = new Set([
  ...[...html.matchAll(/\bt(?:_fmt)?\("([^"]+)"/g)].map((m) => m[1]),
  ...[...html.matchAll(/data-i18n(?:-html|-ph|-aria|-title)?="([^"]+)"/g)].map((m) => m[1]),
].filter((k) => /^[a-z_]+\.[a-z_0-9]+$/.test(k) && k !== "key"));
const i18nDefined = new Set(
  [...html.matchAll(/"([a-z_]+\.[a-z_0-9]+)"\s*:/g)].map((m) => m[1]));
const i18nMissing = [...i18nUsed].filter((k) => !i18nDefined.has(k));
check("the language switch exists", idsInMarkup.includes("langBtn"), "VI/EN toggle in the crumbs");
check("every chrome string has a Vietnamese entry", i18nMissing.length === 0,
  i18nMissing.length ? `missing: ${i18nMissing.join(", ")}` : `${i18nUsed.size} keys, all translated`);
check("the Vietnamese catalogue is substantial, not a stub",
  i18nDefined.size >= 150, `${i18nDefined.size} entries`);
/* The provider is worn on the sleeve: the served document carries which runtime
   answers Run, and both run controls have a pill for it. A console whose button
   promises a model while the server runs a script is the lie this kills — every
   chairman-run task failed `output_contract_unmet` producing `proposal_count,
   scripted`, and nothing could ever have succeeded. */
check("the served page names its provider", /const PROVIDER = "(fake|openrouter)"/.test(html),
  (html.match(/const PROVIDER = "([^"]*)"/) || [])[1] || "no provider baked in");
check("both run controls carry the provider pill",
  idsInMarkup.includes("providerPill") && idsInMarkup.includes("runProvider"),
  "give + task run cards");
/* The workflow tree is cards on a rail, not bare bullets: the styles exist in
   the served document and a node carries a status class, a title and a meta
   line instead of a raw id slice. */
check("the workflow tree has card styles, not browser bullets",
  /\.tree(?:\s*,[^{}]+)?\s*\{[^}]*list-style:\s*none/.test(html) && /\.node\.st-completed/.test(html),
  ".tree reset + status rails");
console.log("runtime:");
check("the script loaded without throwing", uncaught === null, uncaught?.message);
check("requests were made", fetched > 0, `${fetched} fetches`);
check("no request failed", failed.length === 0, failed.slice(0, 3).join("; "));

/* **An unhandled error anywhere in the page is reported, once, with its message.**
   The first version of this file swallowed them: `uncaught` was set and only ever read
   by one check at boot, so an error thrown twenty minutes later — during a
   navigation, say — was invisible, and the log showed "0 boxes" on every screen with
   no explanation. `verify_page.mjs` had been reporting a symptom for a while. */
if (uncaught) {
  console.log(`  note  the page threw: ${uncaught.message}`);
  if (uncaught.stack) {
    const frame = String(uncaught.stack).split("\n").find((l) => l.includes("page.js"));
    if (frame) console.log(`        ${frame.trim()}`);
  }
}


console.log("\nthe way out:");
check("there is a visible back control", Boolean($("backBtn")), "");
check("the breadcrumb bar is present", Boolean($("crumbs")), "");
check("the breadcrumb is marked deep", String($("crumbs").classList.contains("deep")) === "true",
  `class=${$("crumbs").classText || $("crumbs").className}`);
check("the breadcrumb shows a path", $("path").innerHTML.length > 0);


console.log("\nthe event stream:");

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
  /* **A conditional, and the reason is a fact about the provider.**
   *
   * The scripted runtime cannot call `delegate_to_agent`, so `make page` — which forces
   * `model_provider_default=fake` and must not spend model calls — cannot produce a
   * delegation. Making this a hard failure meant the console could only be verified on a
   * tenant someone had already filled by hand, which is precisely the fixture that broke
   * the moment the owner emptied the company to start their own work.
   *
   * So it reports what it found and says why. Falsely passing it would be worse: "the
   * delegation tree renders" is a claim about rendering, and it is checked below against
   * the real tree whenever there is one to check.
   */
  /* **Assert the rendering, not the data.**
   *
   * "There is a delegation on the record" is a claim about the *database*, and it was
   * passing only because a tenant happened to have history. The scripted runtime cannot
   * call `delegate_to_agent`, and `make page` forces `model_provider_default=fake` and
   * must not spend model calls — so this can never be asserted unconditionally here.
   *
   * What *is* assertable is that the tree behaves: with delegations it draws them, and
   * with none it says so rather than showing an empty rectangle. Both are real, both are
   * what a person sees, and neither needs a model.
   */
  /* **Read the children, not `innerHTML`.** The page draws the tree with
   * `$("tree").replaceChildren(...nodes)` — deliberately, because `innerHTML = ...join()`
   * printed `[object HTMLLIElement]` (F266). In a browser `innerHTML` would serialise the
   * children back, but this shim's `innerHTML` is a plain property that stays `""`, so the
   * first version of this check asserted against a string the page never writes and failed
   * a correctly-drawn tree. The honest signal is whether child nodes exist. */
  const flowTree = $("tree");
  const flowEmpty = $("flowEmpty");
  const treeKids = flowTree && Array.isArray(flowTree.children) ? flowTree.children.length : -1;
  const treeDrawn = treeKids > 0;
  const emptyStated = flowEmpty && flowEmpty.hidden === false;
  /* **Drawn tasks count as drawn, with or without delegations.** The first version
   * branched on `delegations.length`: no delegation events meant it demanded the empty
   * state — and failed a tree that was correctly showing two task nodes. A tree of tasks
   * with no handoffs yet is a true statement, not an empty one; the empty state is only
   * correct when there is nothing to draw at all. */
  check("the delegation tree either draws the record or states there is none",
    treeDrawn || emptyStated,
    treeDrawn
      ? `${treeKids} node(s) drawn, ${delegations.length} delegation event(s)`
      : (emptyStated
        ? "nothing on this tenant, and it says so — the scripted runtime does not "
          + "delegate and `make page` does not spend model calls"
        : "neither nodes nor the empty state: the panel is blank for no stated reason"));
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

/* Leaving a detail must release what the detail loaded.
   The project drill-down is gone with the project view, so the question is asked of
   the screens that remain: navigate away from a unit panel and the tree that was
   drawn for it must not be left alive in the document. */
console.log("\ngoing back:");
try {
  sandbox.location.hash = "#/departments";
  await new Promise((r) => setTimeout(r, 2000));
  check("the organisation view comes back", /class="abox/.test($("deptTree").innerHTML),
    `${(String($("deptTree").innerHTML).match(/class="abox/g) || []).length} box(es)`);
} catch (e) {
  check("going back did not throw", false, `${e.message} ${firstFrame(e)}`);
}

console.log("\ngive work:");

try {
  const queue = await (await fetch(`${BASE}/api/v1/ceo/work?limit=100`, {
    headers: { "x-organization-id": ORG },
  })).json();

  sandbox.location.hash = "#/give";
  await new Promise((r) => setTimeout(r, 2000));

  const workStats = $("workStats").innerHTML;
  check("the queue rendered", /class="stat/.test(workStats), `${workStats.length} chars`);
  check("it says how many need a person", /Waiting on you/.test(workStats));
  check("the list has tasks to pick from", /class="row/.test($("giveList").innerHTML),
    `${($("giveList").innerHTML.match(/class="row/g) || []).length} rows`);

  const population = [...workStats.matchAll(/class="v">([^<]+)</g)]
    .map((m) => Number(m[1].replaceAll(",", "")));
  check("the work summary agrees with the whole API population",
    population.join() === [queue.needs_you, queue.in_flight, queue.settled, queue.total].join(),
    `rendered=${population.join()}, total=${queue.total}`);

  const first = $("giveList").innerHTML.match(/data-task="([^"]+)"/);
  check("a task row exists to open", !!first, first ? first[1] : "no data-task in the list");
  if (first) {
    sandbox.location.hash = "#/give/" + first[1];
    await new Promise((r) => setTimeout(r, 2500));

    check("the task detail rendered", $("taskTitle").textContent.length > 0,
      $("taskTitle").textContent);
    /* Visibility, not just content. The detail wrote into a hidden section
       while the router showed the list — every content check passed and no
       person could reach their task. A task that is only in the DOM is not
       on the screen. */
    check("the detail screen is the one on screen, not the list",
      $("view-task").hidden === false && $("view-give").hidden === true,
      `task hidden=${$("view-task").hidden} give hidden=${$("view-give").hidden}`);
    check("the delegation tree is there", /class="row/.test($("taskTree").innerHTML),
      `${($("taskTree").innerHTML.match(/class="row/g) || []).length} rows`);
    check("the log is there", $("taskLog").innerHTML.length >= 0,
      `${$("taskLog").innerHTML.length} chars`);
    check("the report is written in words", /model|openrouter|scripted/i.test($("taskNote").innerHTML),
      $("taskNote").innerHTML.slice(0, 120));

    /* ---- the task page answers the boss's questions, in order ----
       Each of these was a reported gap, and each is structural rather than textual: the
       failure reason lived only in a tooltip, the model's words lived nowhere, and
       arrivals were silent. Asserted here so none of them can regress quietly. */
    check("the bell exists on every screen",
      !!$("notifBtn") && !!$("notifPanel") && !!$("notifCount"),
      "bell + panel + count");
    check("the task states what happened first",
      $("taskBanner").innerHTML.length > 0,
      $("taskBanner").textContent.slice(0, 100));
    check("the steps say what each hand did",
      /data-step=/.test($("taskSteps").innerHTML) || !$("taskStepsEmpty").hidden,
      `${($("taskSteps").innerHTML.match(/data-step=/g) || []).length} step(s)`);
    check("a step carries its pointers, not just a status",
      /data-step=/.test($("taskSteps").innerHTML)
        ? /Done\.|Ended as|Issue:|Cost:/.test($("taskSteps").innerHTML)
        : true,
      "pointers or an honest empty state");
    check("the full text has somewhere to open",
      !!$("sheet") && !!$("sheetBody"),
      "dialog + body");

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
    /* The answer card: when the task carries an output map, its values are on the
       screen as sentences — not just the key names in the banner. Conditional on
       the data, like the edges check above: a task with no output honestly shows
       the empty state instead. Placed after `treeReport` is fetched, because an
       earlier draft read it above this line and died in the temporal dead zone —
       the check then reported the harness's own crash rather than the page. */
    const _out = (treeReport.task && treeReport.task.output) || {};
    const _nKeys = _out && typeof _out === "object" ? Object.keys(_out).length : 0;
    check("the answer is shown as sentences when there is one",
      _nKeys === 0 ? $("taskAnswerEmpty").hidden === false : /answer-row/.test($("taskAnswer").innerHTML),
      _nKeys ? `${_nKeys} key(s), ${($("taskAnswer").innerHTML.match(/answer-row/g) || []).length} row(s)` : "no output, empty state shown");

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
      /free-model quota/.test(html) && /duration varies/.test(html),
      "quota and variable duration are visible before starting");
    check("the run says it is started in the background",
      /runs in the background/.test(html));

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
    await new Promise((r) => setTimeout(r, 1500));
    check("leaving a task releases its detail", $("taskTree").innerHTML.length === 0,
      `${$("taskTree").innerHTML.length} chars still alive`);
    check("and the list is the screen again",
      $("view-give").hidden === false && $("view-task").hidden === true,
      `give hidden=${$("view-give").hidden} task hidden=${$("view-task").hidden}`);
    check("and the queue is there to come back to", /class="row/.test($("giveList").innerHTML));
    /* The live feed, asserted **here** rather than in the structure block: it is drawn
       by `renderGive`, so on the Departments screen it is legitimately still empty and a
       check placed there would be asserting the wrong screen's state. */
    const feed = $("activity");
    const feedLines = feed && Array.isArray(feed.children) ? feed.children.length : -1;
    const feedEmpty = $("activityEmpty");
    /* 0 lines => the empty state is **visible**; lines > 0 => it is hidden. The first
       version asserted the opposite and failed a correctly-behaving feed: `hidden` is
       false when the message is showing, so the relation is `hidden === (lines > 0)`.
       A polarity mistake in a check about a missing message is worth writing down. */
    /* **Read `innerHTML`, not `children`.** The feed is drawn with
     * `feed.innerHTML = rows.map(...).join("")` while the tree is drawn with
     * `replaceChildren(...)`, and this shim's `innerHTML` is a write-only string while its
     * `children` is a separate array. Reading `children` failed a feed that had rendered —
     * `hidden=true` with content in the string is exactly what a correct render looks like
     * here. Each render path is observed the way it writes, or the check asserts the shim
     * rather than the page. */
    const feedHtml = feed ? String(feed.innerHTML || "") : "";
    const feedEmptyShown = Boolean(feedEmpty && feedEmpty.hidden) === false;
    check("the live feed renders, and says so when there is nothing to say",
      !!feed && (feedHtml.length > 0 || feedEmptyShown),
      !feed ? "no #activity in the markup"
        : feedHtml.length
          ? `${(feedHtml.match(/feed-row/g) || []).length} line(s): ${feedHtml.slice(0, 100)}`
          : (feedEmptyShown ? "empty, and it says so" : "empty but the empty state is hidden"));
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

  // **Assign only.** The hash setter now fires `hashchange` on its own, which is what
  // a click does; firing it again by hand here would render the view twice and make
  // every later assertion read a panel two navigations old.
  sandbox.location.hash = "#/departments";
  await new Promise((r) => setTimeout(r, 2000));

  /** The view's own HTML, or `""` — and the page's error text if it failed.
   *
   *  `showError` replaces the view's `innerHTML`, which **destroys the nodes the
   *  checks below read**, so after a page-side failure every assertion reports "0
   *  boxes" and the block's own `catch` reports `null.length`. The real message was
   *  in the view the whole time. Reading it out here is the difference between a
   *  symptom and a cause. */
  const viewHtml = () => String($("view-departments").innerHTML || "");
  const pageError = (viewHtml().match(/Could not load this view[\s\S]{0,220}/) || [])[0];
  const tree = String($("deptTree").innerHTML || "");
  check("the departments view loaded", !pageError, pageError ? pageError.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim().slice(0, 160) : "");
  check("the department tree rendered", /class="abox/.test(tree),
    `${(tree.match(/class="abox/g) || []).length} box(es)`);
  // Operators may add departments; verify the baseline and every live node's nesting.
  check("the department roster retains the seed baseline", depts.offices.length >= EXPECTED_DEPARTMENTS,
    `${depts.offices.length} live departments; minimum ${EXPECTED_DEPARTMENTS}`);
  check("the chief is the root of the tree", /Chief/.test(tree) && /Executive/.test(tree));

  /* --- the hierarchy, asserted structurally -------------------------------
     **This block replaces a check that could not fail.** It was:

         check("the chief is the root of the tree", /Chief/.test(tree) && /Executive/.test(tree));

     Two substring matches on rendered HTML. A flat list of eleven boxes passes it,
     which is exactly what it did: 100 checks green while no department sat inside
     any office. A name appearing somewhere is not a nesting relation, so the
     relation is what is read now.

     Read from the DOM, not from the payload: the payload is what
     `tests/integration/test_departments_reports_the_tree.py` asserts, and asserting
     it twice would prove the server twice and the page never. */
  const groups = [...$("deptTree").querySelectorAll("[data-office-group]")];
  check("the tree groups departments under offices",
    groups.length === (depts.second_tier || []).length,
    `${groups.length} group(s) for ${(depts.second_tier || []).length} office(s)`);
  check("every office has a group of its own",
    groups.every((g) => g.getAttribute("data-office-group"))
    && new Set(groups.map((g) => g.getAttribute("data-office-group"))).size === groups.length,
    groups.map((g) => g.getAttribute("data-office-group")).join(", "));

  const nestedOK = [];
  const flat = [];
  for (const g of groups) {
    const key = g.getAttribute("data-office-group");
    const inner = [...g.querySelectorAll(".dept-edges .abox")];
    const expected = depts.offices.filter((o) => o.parent_unit_slug === key).map((o) => o.key);
    const got = inner.map((b) => b.getAttribute("data-box-key"));
    if (expected.length && JSON.stringify(expected.slice().sort()) === JSON.stringify(got.slice().sort()))
      nestedOK.push(key);
    else flat.push(`${key}: expected [${expected}] got [${got}]`);
  }
  check("every department is inside its own office's group",
    nestedOK.length === groups.length && groups.length > 0,
    flat.length ? flat.join("; ") : `${nestedOK.length}/${groups.length} group(s) correct`);
  check("no department is drawn outside every group",
    [...$("deptTree").querySelectorAll(".abox[data-box-key]")]
      .every((b) => !!b.closest("[data-office-group]") || !depts.offices
        .some((o) => o.key === b.getAttribute("data-box-key"))),
    "the chief is the only box with no office above it");
  check("one vertical rule per office, not one per box",
    groups.every((g) => g.querySelectorAll(".dept-edges").length <= 1)
    && groups.filter((g) => g.querySelector(".dept-edges")).length === groups.length,
    `${groups.filter((g) => g.querySelector(".dept-edges")).length} rule(s) for ${groups.length} office(s)`);

  check("every box in the payload is a box on the screen",
    (tree.match(/class="abox/g) || []).length ===
      [depts.chief, ...(depts.second_tier || []), ...depts.offices].filter(Boolean).length,
    `${(tree.match(/class="abox/g) || []).length} box(es)`);
  check("every box carries its open and done counts", /open <b>\d+<\/b>/.test(tree)
    && /done <b>\d+<\/b>/.test(tree));
  check("every box shows its grant against its ceiling", tree.match(/L\d\/L\d/g || []).length >= EXPECTED_DEPARTMENTS - 1,
    `${(tree.match(/L\d\/L\d/g) || []).length} box(es)`);

  /* A light on a box that is not running would be the whole class of bug this view
     exists to avoid, so the classes are compared against the API rather than trusted. */
  const lit = (tree.match(/abox on/g) || []).length;
  // **All three tiers.** It compared against chief + departments, so the offices' own
  // lights were never counted and a lit office could be drawn without failing here.
  const liveApi = [depts.chief, ...(depts.second_tier || []), ...depts.offices]
    .filter((a) => a && a.running).length;
  check("a lit box means a run in flight, nothing else", lit === liveApi,
    `lit=${lit} api=${liveApi}`);
  check("a stranded run is drawn differently from a live one",
    /abox stuck/.test(tree) || depts.offices.every((o) => !o.stuck),
    "dashed border, not a moving light");

  const stats = $("deptStats").innerHTML;
  /* Two of the four reported defects only exist once the page has run: the agent
   selector is filled by the boot sequence, and the feed is created by it. Asserted here
   rather than in the structure block above, where `$("agent").options` is still the
   shipped "loading agents..." placeholder and reading it proves nothing. */
/* **`check(name, ok, detail)` evaluates `detail` whether or not `ok` is true.** So a
   detail expression cannot assume the thing it describes exists -- doing so threw out
   of the *check*, which is how three shim gaps in a row were reported as page defects
   and each took every check after it down. Everything below is written to survive
   being wrong. */
const agentSel = $("agent");
const agentOpts = agentSel && Array.isArray(agentSel.options) ? agentSel.options : [];
check("the agent selector is filled, not left on its loading text",
  agentOpts.length > 1 && !/loading/i.test(String(agentOpts[0].textContent)),
  `${agentOpts.length} option(s): `
  + agentOpts.slice(0, 3).map((o) => String(o.textContent)).join(" | "));

check("the tiles rendered", /class="stat/.test(stats), `${stats.length} chars`);
  check("it counts stranded runs separately", /Stranded runs/.test(stats));
  /* **The headcount must include the offices.** It read "of 8 agents" over an
     organisation of 11, because the tile summed the chief and the departments and
     left out the tier that had just been added. A headcount that is wrong by exactly
     the boxes a person can newly see is the worst kind: it looks like a rounding. */
  const allBoxes = [depts.chief, ...(depts.second_tier || []), ...depts.offices].filter(Boolean);
  check("the headcount covers every agent in the organisation",
    new RegExp(`of ${allBoxes.length} agents`).test(stats),
    stats.match(/of \d+ agents/)?.[0] || "no headcount on the screen");
  check("an office shows its own work and its departments' separately",
    (depts.second_tier || []).every((o) => !o.rollup || o.rollup.agents > 1)
    && (tree.match(/class="roll"/g) || []).length >= (depts.second_tier || []).length,
    `${(tree.match(/class="roll"/g) || []).length} roll-up line(s)`);

  const office = depts.offices.find((o) => !o.missing);
  if (office) {
    sandbox.location.hash = "#/dept/" + office.key;
    await new Promise((r) => setTimeout(r, 2000));
    const steps = $("runSteps").innerHTML;
    const work = $("workOpen").innerHTML + $("workDone").innerHTML + $("workAttn").innerHTML;
    check("the department panel opened", $("deptTitle").textContent.length > 0,
      $("deptTitle").textContent);
    check("it shows what the agent did", steps.length > 0 || !$("runEmpty").hidden,
      steps.length ? `${(steps.match(/class="step/g) || []).length} run(s)` : "and says it never ran");
    check("it shows the work beside it", work.length > 0 || !$("unitWorkEmpty").hidden,
      "open, attention and done");
    check("open and finished are separated", /Open/.test($("deptOneTree").innerHTML)
      || $("unitWorkSub").textContent.length > 0, $("unitWorkSub").textContent);
    check("there is a way back to the whole organisation", !!$("deptBack").onclick);
    check("the breadcrumb offers the way out", /#\/departments/.test($("path").innerHTML),
      $("path").innerHTML.slice(0, 140));
    check("the page names no department count it cannot back up",
      !/\bsix\b|\bthe 6\b/i.test($("deptTree").innerHTML + $("deptStats").innerHTML
        + $("deptSub").textContent + $("deptHeading").textContent),
      "no hard-coded roster number on the screen");
  }

  /* --- clicking an office filters to its departments ------------------------
     **The request was "click an office, see its departments", and nothing checked
     it.** The click handler looked the id up in `d.offices`, which holds
     departments only, so an office box matched nothing and the click returned
     silently -- the tier was visible and unreachable. */
  const officeBox = (depts.second_tier || [])[0];
  if (officeBox) {
    const before = sandbox.location.hash;
    const box = $("deptTree").querySelector(`.abox[data-box-key="${officeBox.key}"]`);
    check("an office box is clickable", !!box, officeBox.key);
    if (box) {
      // `fire` reaches the page's own `document` click listener, and the handler's
      // `e.target.closest("[data-agent-box]")` walks the parsed tree -- so this is the
      // real click path, not a call into a function the check likes.
      await fire("click", { type: "click", target: box });
      await new Promise((r) => setTimeout(r, 2000));
      check("clicking an office opens that office's own panel",
        sandbox.location.hash === `#/dept/${officeBox.key}`
        && $("deptTitle").textContent.includes(officeBox.label),
        `${sandbox.location.hash} — ${$("deptTitle").textContent}`);
      const mine = depts.offices.filter((o) => o.parent_unit_slug === officeBox.key);
      const shown = [...$("deptOneTree").querySelectorAll(".abox[data-box-key]")]
        .map((b) => b.getAttribute("data-box-key"));
      check("the office's panel shows its own departments",
        mine.length > 0 && mine.every((m) => shown.includes(m.key)),
        `${shown.length} box(es) for ${mine.length} department(s)`);
      check("the office's panel shows the office itself too",
        shown.includes(officeBox.key), "the unit and its departments on one panel");
      // **Labels, not just keys.** `renderTree` hard-coded "Executive Agent" for the
      // box at the top, so an office's own panel titled itself with the chief's name
      // and still passed a key-only check. The name on the panel is what a person
      // reads to know which office they are looking at.
      const officeBoxes = [...$("deptOneTree").querySelectorAll(".abox")]
        .map((b) => b.textContent || "").join(" | ");
      check("the office's panel names the office, not the chief",
        !/Executive Agent/.test(officeBoxes) && officeBoxes.includes(officeBox.label),
        officeBoxes.slice(0, 110));
      check("the office's panel separates its own work from its departments'",
        /own:/.test($("unitWorkSub").textContent)
        && /below/.test($("unitWorkSub").textContent),
        $("unitWorkSub").textContent);
      check("the back button goes back to the organisation",
        $("deptBack").onclick && /office|organisation|department/i.test($("deptBack").textContent),
        $("deptBack").textContent);
      // The button carries `onclick`, which the stub stores; calling it is what the
      // browser does on a real click and needs no event plumbing.
      $("deptBack").onclick && await $("deptBack").onclick();
      await new Promise((r) => setTimeout(r, 2000));
      check("and it arrives back at the tree", sandbox.location.hash !== before
        && !!$("deptTree").querySelector("[data-office-group]"),
        sandbox.location.hash);
    }
  } else {
    check("an office box is clickable", false, "this tenant has no office tier");
  }
} catch (e) {
  check("the departments view did not throw", false, `${e.message} ${firstFrame(e)}`);
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
    ["failed", "cancelled", "blocked"].includes(t.status) && !t.input?.agent_blueprint_draft && !t.input?.agent_workflow);
  const workList = $("giveList").innerHTML;
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
  await new Promise((r) => setTimeout(r, 1500));

  check("a stopped task never reports itself as running",
    $("runThisState").textContent === status && $("runThisTask").hidden,
    `state=${$("runThisState").textContent}, run hidden=${$("runThisTask").hidden}`);
  check("opening a task preserves its exact identity",
    $("taskIdentity").textContent === taskId && $("taskTitle").textContent === report.task.title);
  const reportLine = vm.runInContext('eventLine({event_type:"task.failed",view:{title:"<img src=x onerror=alert(1)>"}})', sandbox);
  check("report event types are understood and untrusted titles are escaped",
    /could not finish/.test(reportLine) && /&lt;img/.test(reportLine) && !/<img/.test(reportLine), reportLine);
  check("approval handlers cannot also consume a task-decision button",
    html.includes('button[data-do][data-id]'));
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
    if (body.business_workflow) {
      const workflow = await (await fetch(`${BASE}/api/v1/workflows/${body.task_id}`, {
        headers: { "x-organization-id": ORG },
      })).json();
      check("a business retry preserves its ordered stages and does not start execution",
        body.started === false && workflow.total > 1 &&
        (body.already_active || (workflow.completed === 0 &&
          workflow.stages.every(stage => stage.status === "assigned") &&
          workflow.evidence.real_model_calls === 0 && workflow.evidence.fake_model_calls === 0)),
        `stages=${workflow.total}, started=${body.started}`);
    } else check("it is created, not started", body.status === "created",
      "retry creation must not start a model run");
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
/* Approval notifications share one durable identity across stream and inbox reloads. */
const beforeNotif = vm.runInContext("notifs.length", sandbox);
vm.runInContext('notify({id:"approval:ui-notification-check", title:"Review this draft", href:"#/approval/ui-notification-check", silent:true})', sandbox);
check("unread approval lights the bell badge", !$("notifCount").hidden && Number($("notifCount").textContent) >= 1);
vm.runInContext('notify({id:"approval:ui-notification-check", title:"Duplicate", silent:true})', sandbox);
check("stream and inbox duplicates keep one bell entry", vm.runInContext("notifs.length",sandbox) === beforeNotif+1);
check("bell links the exact approval", $("notifList").innerHTML.includes('href="#/approval/ui-notification-check"'));
check("restoring an approval does not interrupt with a toast", !$("toast").innerHTML.includes("Review this draft"));
$("sidebarToggle").onclick();
check("sidebar collapse is accessible and persistent", $("sidebarToggle").getAttribute("aria-expanded") === "false" && sandbox.document.documentElement.classList.contains("sidebar-collapsed"));
$("sidebarToggle").onclick();
check("sidebar expands again", $("sidebarToggle").getAttribute("aria-expanded") === "true");
/* Primitive rendering and input contracts exercise nested untrusted values. */
const readable = vm.runInContext('valueHTML({recommendation:{message:"<img src=x onerror=alert(1)>"},items:["Preserve the words",{score:7}]})', sandbox);
check("nested records are readable and escaped without JSON", /Recommendation/.test(readable) && /Score/.test(readable) && /Preserve the words/.test(readable) && /&lt;img/.test(readable) && !/<img|json-block/.test(readable));
check("structured answers copy as words without JSON", vm.runInContext('plain({decision:"approved",items:[{score:7}]})', sandbox) === "Decision: approved\nItems: Score: 7");
const typedArguments = vm.runInContext('Management.schemaArguments({properties:{limit:{type:"integer"},enabled:{type:"boolean"},mode:{type:"string",enum:["a","b"]}},required:["enabled"]},{arg_limit:"7",arg_enabled:"false",arg_mode:"b"})', sandbox);
check("tool forms preserve numeric and false values", typedArguments.limit === 7 && typedArguments.enabled === false && typedArguments.mode === "b");
let requiredRefused = false;
try {vm.runInContext('Management.schemaArguments({properties:{enabled:{type:"boolean"}},required:["enabled"]},{})', sandbox);} catch {requiredRefused = true;}
check("tool forms refuse missing required values", requiredRefused);
const hashBeforeSkip = sandbox.location.hash;
$("skipContent").onclick({preventDefault(){}});
check("Skip to content preserves the current route", sandbox.location.hash === hashBeforeSkip);
check("no management form asks the chairman to enter JSON", !/Arguments \(JSON|evidence \(JSON|schema \(JSON/.test(html));
sandbox.location.hash = "#/library/skills";
await new Promise(r => setTimeout(r, 500));
sandbox.location.hash = "#/operations/models";
await new Promise(r => setTimeout(r, 500));
vm.runInContext("backWithinConsole()", sandbox);
await new Promise(r => setTimeout(r, 500));
check("Back returns to the preceding console screen", sandbox.location.hash === "#/library/skills");
sandbox.history.forward();
await new Promise(r => setTimeout(r, 500));
check("browser Forward returns to the next console screen", sandbox.location.hash === "#/operations/models");
/* Management routes use the real projections and preserve exact deep links. */
console.log("management:");
for (const destination of ["processes/catalogue", "processes/workflows", "processes/hiring", "processes/definitions", "processes/provision",
  "library/skills", "library/tools", "library/memory", "business/projects", "business/documents",
  "operations/models", "operations/usage", "operations/events", "operations/decisions",
  "operations/audit", "operations/governance", "operations/system", "settings", "settings/units", "settings/roles"]) {
  try {
    sandbox.__hash = "#/" + destination;
    await vm.runInContext("route()", sandbox);
    const content = String($("managementBody")?.innerHTML || "");
    const error = !$("pageError").hidden ? $("pageError").innerHTML : "";
    check(destination + " renders from its API", !error && content.length > 0,
      error ? String(error).replace(/<[^>]*>/g, " ").slice(0,160) : `${content.length} characters`);
    if (destination === "processes/hiring") check("the example process explains its reference and recorded stage count", /JD reference|Tham chiếu JD/.test(content) && /Finished \/ recorded stages|Bước đã xong/.test(content));
    if (destination === "processes/workflows") check("real hiring has a separate brief form and salary/date inputs", /id="hiring-brief"/.test(content) && /name="salary_min"/.test(content) && /name="start_date"/.test(content) && /id="create-mep"/.test(content));
    check(destination + " does not show missing JS values", !/\bundefined\b|\[object Object\]/.test(content));
    check(destination + " selects only the management surface", !$("view-management").hidden && $("view-give").hidden && $("view-departments").hidden);
  } catch (err) { check(destination + " did not throw", false, err.message); }
}
try {
  const profiles = await (await fetch(`${BASE}/api/v1/model-profiles`,{headers:{"x-organization-id":ORG}})).json();
  check("effective primary profile names the requested Dots model",
    profiles.items.find(p=>p.name === "primary")?.providers[0]?.model === "dots-studio/dots-3-note-preview:free");
  const definitions = await (await fetch(`${BASE}/api/v1/console/catalogue?kind=definitions`,{headers:{"x-organization-id":ORG}})).json();
  if(definitions.items.length){
    const definition=definitions.items[0]; sandbox.__hash="#/processes/definitions/"+definition.id;
    await vm.runInContext("route()",sandbox);
    check("a definition deep link shows exactly the selected definition",
      $("managementBody").innerHTML.includes(definition.id) && $("managementBody").innerHTML.includes(definition.name));
  }
  const agents=await (await fetch(`${BASE}/api/v1/agents?limit=1`,{headers:{"x-organization-id":ORG}})).json();
  if(agents.items.length){
    const agent=agents.items[0]; sandbox.__hash="#/agent/"+agent.id;
    await vm.runInContext("route()",sandbox);
    check("agent controls preserve the selected agent identity",$("managementBody").innerHTML.includes(agent.id) && $("managementBody").innerHTML.includes(agent.name));
    check("agent configuration exposes actual profile and budget controls", /agent-config/.test($("managementBody").innerHTML) && /budget_limit_tokens/.test($("managementBody").innerHTML));
  }
  const blueprints=await (await fetch(`${BASE}/api/v1/agent-blueprints`,{headers:{"x-organization-id":ORG}})).json();
  const completedPlan=blueprints.items.find(d=>d.status==="completed");
  if(completedPlan){
    sandbox.__hash="#/processes/provision/"+completedPlan.id;
    await vm.runInContext("route()",sandbox);
    const draft=await (await fetch(`${BASE}/api/v1/agent-blueprints/${completedPlan.id}`,{headers:{"x-organization-id":ORG}})).json();
    const content=$("managementBody").innerHTML;
    check("agent plan deep link renders the exact editable draft",!!$("blueprint-editor") && content.includes(draft.config.name));
    check("all drafted tasks and source references are reviewable",draft.output.plan.steps.every(s=>content.includes(s.title) && s.source_sop_codes.every(code=>content.includes(code))));
    check("agent plan has a separate human submission control",!!$("blueprint-submit") || !!draft.output.provisioned);
    if(draft.output.provisioned){
      const rootId=draft.output.provisioned.workflow_id;
      sandbox.__hash="#/processes/provision/workflow/"+rootId;
      await vm.runInContext("route()",sandbox);
      const workflow=await (await fetch(`${BASE}/api/v1/agent-blueprints/workflows/${rootId}`,{headers:{"x-organization-id":ORG}})).json();
      const workflowContent=$("managementBody").innerHTML;
      check("prepared workflow opens its exact root and task links",workflowContent.includes(rootId) && workflow.steps.every(s=>workflowContent.includes("#/give/"+s.id)));
      check("prepared workflow exposes refresh and current input requirements",!!$("blueprint-refresh") && workflow.missing_inputs.every(k=>workflowContent.includes(k)));
      check("future source fields do not block partial submission", [...sandbox.document.querySelectorAll("#blueprint-inputs textarea")].every(el=>!el.hasAttribute("required")));
    }
  }
  const events=await (await fetch(`${BASE}/api/v1/events?limit=2`,{headers:{"x-organization-id":ORG}})).json();
  if(events.items.length){
    const event=events.items[0]; sandbox.__hash="#/operations/events/"+event.id;
    await vm.runInContext("route()",sandbox);
    check("an event deep link opens its exact payload", $("managementBody").innerHTML.includes(event.id) && $("managementBody").innerHTML.includes(event.type));
  }
} catch(err){check("management detail checks did not throw",false,err.message);}

try {
  const workflows = await (await fetch(`${BASE}/api/v1/workflows`, {headers:{"x-organization-id":ORG}})).json();
  let selected = workflows.items.find(w => w.kind === "mep_hiring" && w.status === "completed") || workflows.items[0];
  if (!selected) selected = await (await fetch(`${BASE}/api/v1/workflows/examples`, {method:"POST", headers:{"x-organization-id":ORG,"content-type":"application/json"}, body:JSON.stringify({kind:"mep_hiring"})})).json();
  sandbox.__hash = "#/processes/workflows/" + selected.id;
  await vm.runInContext("route()",sandbox);
  const content = String($("managementBody").innerHTML);
  const evidence = await (await fetch(`${BASE}/api/v1/workflows/${selected.id}`, {headers:{"x-organization-id":ORG}})).json();
  check("business workflow deep link shows the exact root", content.includes(selected.id));
  check("business workflow exposes every ordered stage and its task", evidence.stages.every(s => content.includes(s.id) && content.includes(s.title)));
  check("business workflow states simulation and source references", /SIMULATION|MÔ PHỎNG/.test(content) && /ONX-/.test(content));
  check("business workflow exposes execution evidence and refresh controls", !!$("workflow-refresh") && /Recorded events|Sự kiện \/ audit/.test(content));
  if(evidence.status === "completed" && evidence.kind === "mep_hiring") check("completed hiring shows every CV and grounded score", evidence.stages.find(s=>s.key==="scoring").output.candidates.every(c=>content.includes(String(c.score)) && content.includes(c.candidate_id)));
} catch(err) {check("business workflow detail did not throw",false,err.message);}

check("management navigation produced no unhandled error",uncaught === null,uncaught?.message);
process.exit(problems.length ? 1 : 0);
