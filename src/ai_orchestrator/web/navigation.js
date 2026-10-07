/* ==================================================================
   6. Routing.

   `#/projects`, `#/projects/:id`, `#/agents`, `#/agents/:id`, …

   The breadcrumb is derived from the route, so it cannot disagree with
   where you are. And `Esc` is bound on the document, so there is one
   predictable way out from anywhere in the product.
   ================================================================== */
const MANAGEMENT_VIEWS = new Map();
const ROUTES = {
  give: { label: "Work" },
  task: { label: "Task" },
  console: { label: "Work" },
  work: { label: "Issues and approvals" },
  approval: { label: "Approval" },
  departments: { label: "Departments" },
  dept: { label: "Department" },
  agent: { label: "Agent controls" },
  processes: { label: "Processes" },
  library: { label: "Library" },
  operations: { label: "Operations" },
  business: { label: "Business records" },
  settings: { label: "Settings" },
};

function decodeRoutePart(value) {
  try {
    return decodeURIComponent(value);
  } catch {
    return value;
  }
}
function parseHash() {
  const raw = (location.hash || "#/give").replace(/^#\/?/, "");
  const parts = raw.split("/").filter(Boolean);
  return {
    name: ROUTES[parts[0]] ? parts[0] : "give",
    arg: parts[1] ? decodeRoutePart(parts[1]) : null,
    section: parts[2] || "overview",
    focus: parts[3] ? decodeRoutePart(parts[3]) : null,
    eventOffset:
      parts[2] === "log" && parts[3] === "page" && /^\d+$/.test(parts[4] || "")
        ? Number(parts[4])
        : 0,
  };
}

function go(hash) {
  location.hash = hash;
}

function renderCrumbs(route) {
  const items = [];
  const add = (label, href, here) =>
    items.push(
      href
        ? `<a href="${href}">${esc(label)}</a>`
        : `<span class="here">${esc(label)}</span>`,
    );
  switch (route.name) {
    case "approval":
      add(tr("nav.needs", "Needs you"), "#/work/approvals");
      items.push('<span class="sep">/</span>');
      add(tr("crumb.decide", "Decide"), null, true);
      break;
    case "work":
      add(
        route.arg === "approvals"
          ? tr("need.approvals", "Approvals")
          : tr("need.issues", "Issues"),
        null,
        true,
      );
      break;
    case "dept":
      add(tr("nav.departments", "Departments"), "#/departments");
      items.push('<span class="sep">/</span>');
      add(
        $("deptTitle").textContent || tr("crumb.dept", "Department"),
        null,
        true,
      );
      break;
    case "departments":
      add(tr("nav.departments", "Departments"), null, true);
      break;
    case "task":
      add(tr("nav.give", "Work"), "#/give");
      items.push('<span class="sep">/</span>');
      add(taskName(route.arg), null, true);
      break;
    case "give":
      if (route.arg) {
        add(tr("nav.give", "Work"), "#/give");
        items.push('<span class="sep">/</span>');
        add(taskName(route.arg), null, true);
      } else {
        add(tr("nav.give", "Work"), null, true);
      }
      break;
    default: {
      const meta = ROUTES[route.name] || ROUTES.give;
      add(
        tr("nav." + route.name, meta.label),
        route.arg ? `#/${route.name}` : null,
        true,
      );
      if (route.arg) {
        items.push('<span class="sep">/</span>');
        const sections = {
          models: ["Models", "Model"],
          usage: ["Usage", "Sử dụng"],
          events: ["Event log", "Nhật ký sự kiện"],
          decisions: ["Decisions", "Quyết định"],
          audit: ["Audit", "Kiểm toán"],
          governance: ["Governance", "Kiểm soát"],
          system: ["System", "Hệ thống"],
          skills: ["Skills", "Kỹ năng"],
          tools: ["Tools", "Công cụ"],
          memory: ["Memory", "Bộ nhớ"],
          projects: ["Projects", "Dự án"],
          documents: ["Documents", "Tài liệu"],
          catalogue: ["Procedure catalogue", "Danh mục quy trình"],
          hiring: ["Recruitment example", "Quy trình tuyển dụng mẫu"],
          workflows: ["Business workflows", "Workflow nghiệp vụ"],
          definitions: ["Agent definitions", "Định nghĩa agent"],
          provision: ["Provision agent", "Tạo agent"],
          organization: ["Organization", "Tổ chức"],
          units: ["Units", "Đơn vị"],
          roles: ["Roles", "Vai trò"],
        };
        const label = sections[route.arg];
        add(label ? label[state.lang === "vi" ? 1 : 0] : route.arg, null, true);
        const resource =
          route.section !== "overview" && route.section !== "page"
            ? route.section
            : null;
        if (resource && MANAGEMENT_VIEWS.has(route.name)) {
          items.push('<span class="sep">/</span>');
          add(resource, null, true);
        }
      }
    }
  }
  setHTML("path", items.join(" "));
  // `.deep` is sticky for the rest of the session: once a person has drilled in
  // anywhere, the way out is on the page forever. There is no state in which
  // somebody is "somewhere" with no visible route back.
  $("crumbs").classList.add("deep");
}

let visitedRoutes = 0;
let lastRouteHash = null;
const consoleRoot = location.href.split("#")[0];
function rememberNavigation() {
  if (
    typeof history !== "undefined" &&
    typeof history.replaceState === "function"
  ) {
    const saved = history.state;
    visitedRoutes =
      saved?.consoleRoot === consoleRoot &&
      saved.consoleHash === location.hash &&
      Number.isInteger(saved.consolePosition)
        ? saved.consolePosition
        : visitedRoutes + 1;
    history.replaceState(
      {
        ...(saved || {}),
        consoleRoot,
        consolePosition: visitedRoutes,
        consoleHash: location.hash,
      },
      "",
    );
  } else if (location.hash !== lastRouteHash) visitedRoutes++;
  lastRouteHash = location.hash;
}
function backWithinConsole() {
  if (visitedRoutes > 1) {
    history.back();
    return;
  }
  const r = parseHash();
  go(
    r.name === "agent" || r.name === "dept"
      ? "#/departments"
      : r.name === "approval"
        ? "#/work/approvals"
        : MANAGEMENT_VIEWS.has(r.name) && r.arg
          ? `#/${r.name}`
          : "#/give",
  );
}
$("backBtn").onclick = backWithinConsole;
document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if ($("sheet").open) {
    $("sheet").close();
    return;
  }
  if (!/^(INPUT|TEXTAREA|SELECT)$/.test(e.target?.tagName || ""))
    backWithinConsole();
});

/* ---------------- the router ---------------- */
let renderToken = 0;
async function route() {
  const r = parseHash();
  rememberNavigation();
  if (r.name !== "approval" && $("sheet").open) $("sheet").close();
  $("pageError").hidden = true;
  // Leaving a task stops watching it. The poller belongs to the detail screen, and a
  // request every four seconds for a task nobody is looking at is the kind of quiet cost
  // that shows up as load nobody can explain.
  if ((r.name !== "give" && r.name !== "task") || !r.arg) {
    if (typeof watchTimer !== "undefined" && watchTimer) {
      clearInterval(watchTimer);
      watchTimer = null;
    }
  }
  // The register poller belongs to the Give work list; anywhere else it is
  // requests spent proving nothing.
  if (r.name !== "give") {
    if (typeof givePoll !== "undefined" && givePoll) {
      clearInterval(givePoll);
      givePoll = null;
    }
  }
  const mine = ++renderToken;
  for (const name of Object.keys(ROUTES)) {
    const el = $(
      "view-" +
        (MANAGEMENT_VIEWS.has(name)
          ? "management"
          : name === "approval"
            ? "work"
            : name),
    );
    if (el) el.hidden = true;
  }
  $("view-management").hidden = true;
  const target = MANAGEMENT_VIEWS.has(r.name)
    ? "management"
    : { approval: "work", task: "give", dept: "departments", console: "give" }[
        r.name
      ] || r.name;
  const view = $("view-" + target);
  if (view) view.hidden = false;
  /* Release the previous project's data when leaving the project view.

     Without this a project stays alive in the DOM after you navigate away -- 136 KB
     of bars and 288 table rows for the real corpus, invisible because the view is
     hidden but never released. Found by `verify_page.mjs`, which navigated to a
     project and back and found the detail still populated. It is not a visible bug
     and it is still a bug: memory that only grows, and a `#/projects` visit that
     could briefly show the last project's bars before its own render lands. */
  if (target !== "projects") {
    state.workspace = null;
    setHTML("projDetail", "");
  }
  for (const a of document.querySelectorAll("[data-nav]")) {
    const on =
      a.dataset.nav ===
        (r.name === "agent"
          ? "departments"
          : target === "management"
            ? r.name
            : target) &&
      (target !== "work" ||
        a.getAttribute("href") === `#/work/${r.arg || "issues"}`);
    if (on) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  renderCrumbs(r);
  window.UIShell?.indicator();
  try {
    if (mine !== renderToken) return; // a newer navigation won
    await render(r, mine);
    renderCrumbs(parseHash());
    paintProvider();
  } catch (err) {
    if (mine !== renderToken) return;
    showError(err);
  }
}
window.addEventListener("hashchange", route);

function showError(err) {
  const el = $("pageError");
  el.hidden = false;
  el.innerHTML = `<b>${tr("err.load", "Could not load this view.")}</b> ${esc(err.message || String(err))}
    <div class="btn-row"><button class="btn sm" onclick="location.reload()">${tr("err.reload", "Reload")}</button>
    <a class="btn sm" href="#/give">${tr("nav.give", "Work")}</a></div>`;
}

async function render(r, mine) {
  if (MANAGEMENT_VIEWS.has(r.name)) {
    await MANAGEMENT_VIEWS.get(r.name)(r, {
      current: () => mine === renderToken,
    });
    return;
  }
  switch (r.name) {
    case "work":
      await renderWork();
      break;
    case "approval":
      await renderWork();
      await renderApproval(r.arg);
      break;
    case "departments":
    case "dept":
      await renderDepartments(r.arg);
      break;
    // `task` is the readable name for a task, and `view-task` is the section that holds it.
    // There was no case for it, so `#/task/<id>` was a **dead route**: the view stayed
    // hidden and nothing rendered. Found because the retry handler linked to it -- the page
    // linking to a URL of its own that does not work is the same defect as a drill-down with
    // no way out, pointed the other way.
    case "task":
    case "give":
    case "console":
      await renderGive(r.arg);
      break;
    default:
      await renderGive();
  }
  if (mine !== renderToken) return;
}
