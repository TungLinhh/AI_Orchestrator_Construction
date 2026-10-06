/* ==================================================================
   7. Views.
   ================================================================== */
function statTile(k, v, note, tone) {
  return (
    `<div class="stat${tone ? " " + tone : ""}"><div class="k">${esc(k)}</div>` +
    `<div class="v">${esc(v)}</div>` +
    (note ? `<div class="n">${esc(note)}</div>` : "") +
    `</div>`
  );
}

function depthOf(items, node) {
  let d = 0,
    cur = node,
    guard = 0;
  const byId = new Map(items.map((i) => [i.id, i]));
  while (cur && cur.parent_id && guard++ < 8) {
    cur = byId.get(cur.parent_id);
    d++;
  }
  return d;
}

/* ---------------- issues: what went wrong, with the reason ---------------- */
let issueFilter = "";

/** Escape and wrap for a `<pre>` block, so a multi-line reason reads as a paragraph. */
function reasonBlock(text) {
  return `<pre class="reason">${esc(String(text))}</pre>`;
}

/**
 * Render the issues list from the register, every row expandable.
 *
 * **The reason is in the row, collapsed, and full when opened.** A list of `failed`
 * without its cause is not an issues list: the person has to open each task to find out
 * what happened, which is what they were trying to avoid. So the first line carries a
 * trimmed reason, and `<details>` carries the whole of it.
 *
 * `<details>`/`<summary>` rather than a click handler because it is keyboard-accessible
 * and works without JavaScript wiring — the same reason it is used for the tooltip.
 */
function renderIssues(q) {
  const all = (q.items || []).filter(
    (t) =>
      ["failed", "blocked", "canceled", "cancelled"].includes(t.status) ||
      (t.waiting_on === "you" && t.status === "created"),
  );
  const rows = !issueFilter
    ? all
    : all.filter((t) =>
        issueFilter === "waiting"
          ? t.status === "created"
          : t.status === issueFilter,
      );

  const failed = all.filter((t) => t.status === "failed").length;
  const blocked = all.filter((t) => t.status === "blocked").length;
  const unowned = all.filter((t) => t.status === "created").length;
  setHTML(
    "needStats",
    statTile(
      tr("need.you", "Needs you"),
      num(q.needs_you ?? 0),
      tr("need.you_sub", "a decision, an owner, or a judgement"),
      (q.needs_you ?? 0) ? "warn" : "good",
    ) +
      statTile(
        tr("need.failed", "Failed"),
        num(failed),
        tr("need.failed_sub", "the company could not finish them"),
        failed ? "alarm" : "good",
      ) +
      statTile(
        tr("need.blocked", "Blocked"),
        num(blocked),
        tr("need.blocked_sub", "waiting on something"),
        blocked ? "warn" : "good",
      ) +
      statTile(
        tr("need.approvals_t", "Approvals"),
        num(state.inbox ? state.inbox.length : 0),
        tr("need.approvals_sub", "pending a person"),
        (state.inbox || []).length ? "warn" : "good",
      ),
  );

  $("issueSub").textContent = all.length
    ? t_fmt("need.look", "{n} to look at", { n: all.length })
    : tr(
        "need.issues_empty",
        "Nothing has gone wrong. Every task either finished or is still being worked on.",
      );
  $("issueEmpty").hidden = rows.length > 0;
  setHTML(
    "issueList",
    rows
      .map((it) => {
        const short = it.last_error
          ? String(it.last_error).slice(0, 150)
          : tr("need.noreason_short", "no reason recorded");
        return `
    <details class="row issue" data-task="${esc(it.id)}">
      <summary class="grow">
        <div class="t"><span class="pill bad">${esc(it.status)}</span> ${esc(it.title)}</div>
        <div class="s">${esc(it.task_type || tr("need.task", "task"))}${
          it.owner_name
            ? t_fmt("row.held", " · held by {n}", { n: esc(it.owner_name) })
            : tr("row.noowner", " · nobody holds it")
        }
          ${it.children ? t_fmt("row.subs", " · {n} subtask(s)", { n: it.children }) : ""}</div>
        <div class="s err">${esc(short)}${String(it.last_error || "").length > 150 ? "…" : ""}</div>
      </summary>
      <div class="issue-body">
        ${
          it.last_error
            ? reasonBlock(it.last_error)
            : `<div class="s">${tr("need.noreason", "No reason was recorded for this outcome.")}</div>`
        }
        <div class="btn-row">
          <a class="btn sm" href="#/give/${encodeURIComponent(it.id)}/log">${tr("need.open", "Open it")}</a>
          <button class="btn sm" data-retry="${esc(it.id)}">${tr("need.retry", "Run it again")}</button>
        </div>
      </div>
    </details>`;
      })
      .join(""),
  );
}

/* ---------------- work: the queue you can actually act on ---------------- */
async function renderWork() {
  const routeKey = location.hash;
  const selected = parseHash().name === "work" ? parseHash().arg : null;
  $("issuesCard").hidden = selected === "approvals";
  $("approvalsCard").hidden = selected === "issues";
  $("needsHeading").textContent =
    selected === "issues"
      ? tr("need.issues", "Issues")
      : selected === "approvals"
        ? tr("need.approvals", "Approvals")
        : tr("nav.needs", "Needs you");
  // **The issues list and the approvals list come from one question**, so they are loaded
  // together and rendered together. `/ceo/work` is the register — statuses, owners, and the
  // reason on anything that stopped — and `/approvals/inbox` is what a person must decide.
  // Both counts go into the sidebar, because a badge a person cannot reach is decoration.
  let register = {};
  register = await loadRegister();
  // **The filter does something, which it did not.**
  //
  // It used to read `const shown = state.workFilter === "all" ? d.items : d.items;` --
  // both branches the same array, so "All" showed exactly what "Waiting" showed. And
  // `state.workFilter` defaulted to `"pending"` while the buttons carried
  // `data-f="pending"` / `data-f="all"` and **nothing was bound to them at all**, so the
  // filter could not be changed from the page under any circumstances. Both defects are
  // silent: the list is always a plausible list of approvals, just never the other one.
  //
  // `/approvals/inbox` returns every *pending* approval and nothing else, so there is no
  // status to send and the split is made here. The two sets are genuinely different:
  // **actionable** -- a person can still answer -- against **expired**, which the inbox
  // lists deliberately ("nothing is filtered out... a queue that silently omits rows
  // cannot be reconciled") and which renders with no buttons at all. A person who wants
  // to see the backlog needs both; a person deciding needs the first.
  const d = await apiGet("/approvals/inbox");
  if (location.hash !== routeKey) return;
  state.inbox = d.items;
  const live = d.items.filter((a) => a.actionable !== false && !a.expired);
  const all = state.approvalFilter === "all";
  const shown = all ? d.items : live;
  $("workSub").textContent = d.count
    ? all
      ? t_fmt("need.sub_all", "{c} pending · {l} still answerable", {
          c: d.count,
          l: live.length,
        })
      : t_fmt("need.sub_live", "{l} waiting on you of {c} pending", {
          l: live.length,
          c: d.count,
        })
    : tr("need.clear", "clear");
  $("workEmpty").hidden = shown.length > 0;
  // An expired request gets **no buttons**.
  //
  // It used to get the same three as a live one, and clicking Approve answered
  // "approval apr_… expired at 2026-09-29T15:46:06" — the only place the deadline
  // was ever visible. A queue that offers an action the platform will refuse is a
  // queue that spends a person's click to teach them something the queue already
  // knew.
  //
  // The task title is shown for the same reason: two pending rows for
  // `hr.open_headcount` on two different tasks rendered as two identical lines,
  // which reads as the same question asked twice rather than two questions.
  setHTML(
    "workList",
    shown
      .map((a) => {
        const live = a.actionable !== false && !a.expired;
        const buttons = live
          ? `
      <div class="btn-row" data-actions="${esc(a.approval_id)}">
        <button class="btn primary sm" data-do="approve" data-id="${esc(a.approval_id)}">${tr("appr.approve", "Approve")}</button>
        <button class="btn sm" data-do="ask" data-id="${esc(a.approval_id)}">${tr("appr.ask", "Ask")}</button>
        <button class="btn danger sm" data-do="reject" data-id="${esc(a.approval_id)}">${tr("appr.reject", "Reject")}</button>
      </div>`
          : `
      <div class="btn-row">
        <span class="tag wait">${tr("appr.expired", "expired")}</span>
        <span class="s">${tr("appr.reraise", "needs re-raising — nobody can answer it now")}</span>
      </div>`;
        return `
    <div class="row static" data-approval="${esc(a.approval_id)}" style="flex-wrap:wrap">
      <div class="grow" style="min-width:240px">
        <div class="t"><a href="#/approval/${encodeURIComponent(a.approval_id)}">${esc(a.action_type)}</a>
          <span class="tag ${a.risk_level === "high" ? "wait" : ""}">${esc(a.effect_class || "—")}</span>
          ${a.risk_level === "high" ? `<span class="tag wait">${tr("appr.highrisk", "high risk")}</span>` : ""}
        </div>
        <div class="s">${a.task_title ? esc(a.task_title) + " · " : ""}${t_fmt("appr.asked", "asked by {n}", { n: esc(a.requested_by || tr("appr.unknown", "unknown")) })}
          ${a.assigned_approver_id ? t_fmt("appr.routed", "· routed to {n}", { n: esc(a.assigned_approver_id) }) : tr("appr.queue", "· the queue")}
          ${t_fmt("appr.waiting", "· waiting {n}d", { n: esc(a.waiting_days) })}</div>
      </div>
      ${buttons}
    </div>`;
      })
      .join(""),
  );
  renderIssues(register);
  const issueBadge = $("navIssueCount");
  const issues = (register.items || []).filter((t) =>
    ["failed", "blocked", "canceled", "cancelled"].includes(t.status),
  );
  if (issueBadge) {
    issueBadge.textContent = issues.length ? String(issues.length) : "";
    issueBadge.hidden = !issues.length;
  }

  setHTML(
    "workNote",
    tr(
      "need.note",
      "Oldest first, because an approval queue has no urgency field and a person working " +
        "it by insertion order is working it by accident. <b>Ask</b> sends the task back to " +
        "the agent with your question instead of ending it — usually the right answer when " +
        "you are unsure.",
    ),
  );
  setHTML("navApprovalCount", d.count ? String(d.count) : "");
  const _nbA = $("navApprovalCount");
  if (_nbA) _nbA.hidden = !d.count;
  /* The sidebar shows one number for this screen: issues plus decisions waiting.
     Two writers share it — renderGive writes the register's `needs_you` — and
     either is the number a boss is accountable for; the last render wins. */
  const _nbW = $("navWorkCount");
  if (_nbW) {
    const _n = issues.length + live.length;
    _nbW.textContent = _n ? String(_n) : "";
    _nbW.hidden = !_n;
  }
}

/* ---------------- retry, once, for every place that offers it ---------------- */
/**
 * Run a failed piece of work again, as a **new** task that remembers the first attempt.
 *
 * `POST /tasks/{id}/retry`, not `POST /tasks/{id}/run`. Two reasons, both from the API:
 * a failed task is terminal and reusing the row would make the audit trail lie about what
 * was attempted; and a retry is a new task linked by `task_dependencies`, so the attempt
 * graph is real rather than a column somebody updates.
 *
 * **One function for two buttons**, because the task-detail Retry button was rendered,
 * titled and shown — and had no handler at all. `grep retryBtn` found a `hidden` line, a
 * `title` line and nothing else: a control that appeared exactly when it was useful and
 * did nothing when pressed.
 */
async function retryTask(id, button) {
  const label = button ? button.textContent : "";
  if (button) {
    button.disabled = true;
    button.textContent = tr("run.starting", "Starting…");
  }
  try {
    const made = await apiPost(`/tasks/${encodeURIComponent(id)}/retry`, {});
    const newId = made.task_id || made.id;
    if (newId) {
      toast(
        made.started === false
          ? tr("retry.queued", "Queued for another attempt.")
          : tr("retry.running", "Running the same work again as a new task."),
      );
      go(
        made.business_workflow
          ? `#/processes/workflows/${encodeURIComponent(newId)}`
          : `#/give/${encodeURIComponent(newId)}`,
      );
      return;
    }
    toast(
      t_fmt("retry.couldnot", "Could not start it again: {e}", {
        e: made.reason || made.error || tr("retry.unknown", "unknown"),
      }),
      true,
    );
  } catch (err) {
    // A refusal here is usually "an equivalent task is already active" — the platform
    // protecting itself from doing the same work twice. Reported, not swallowed, because
    // the alternative is a button that appears to work.
    toast(
      t_fmt("retry.couldnot", "Could not start it again: {e}", {
        e: err.message,
      }),
      true,
    );
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = label;
    }
  }
}
$("retryBtn").onclick = () => retryTask(state.taskId, $("retryBtn"));

document.addEventListener("click", async (e) => {
  const ans = e.target.closest("button[data-answertext]");
  if (ans) {
    const i = Number(ans.dataset.answertext);
    const text = answerTexts[i] ?? "";
    openSheet(
      tr("sheet.full_answer", "The full answer"),
      text
        ? `<pre class="reason" style="max-height:60vh">${esc(String(text).slice(0, 60000))}</pre>`
        : `<div class="s">${tr("sheet.answer_empty", "This answer is empty.")}</div>`,
    );
    return;
  }
  const full = e.target.closest("button[data-fulltext]");
  if (full) {
    const i = Number(full.dataset.fulltext);
    const text = stepTexts[i] ?? "";
    openSheet(
      tr("sheet.agent_said", "What the agent said"),
      text
        ? `<pre class="reason" style="max-height:60vh">${esc(text.slice(0, 60000))}</pre>`
        : `<div class="s">${tr("step.nooutput", "This run left no written output.")}</div>`,
    );
    return;
  }
  const retry = e.target.closest("button[data-retry]");
  if (retry) {
    // A `<details>` row must not also navigate: the click lands on the summary too.
    e.preventDefault();
    e.stopPropagation();
    await retryTask(retry.dataset.retry, retry);
    return;
  }
  const btn = e.target.closest("button[data-do][data-id]");
  if (!btn) return;
  const { do: action, id } = btn.dataset;
  if (action === "ask") {
    const question = window.prompt(
      tr("appr.ask_q", "What should the agent answer before you decide?"),
    );
    if (!question) return;
    await decide(id, "request-information", {
      needs_information: true,
      note: question,
    });
    return;
  }
  const label =
    action === "approve"
      ? tr("appr.approve_v", "Approve")
      : tr("appr.reject_v", "Reject");
  if (!window.confirm(t_fmt("appr.confirm", "{a} this request?", { a: label })))
    return;
  await decide(id, action, {});
});

async function decide(approvalId, action, extra) {
  try {
    await apiPost(
      `/approvals/${encodeURIComponent(approvalId)}/${action}`,
      extra,
    );
    if ($("sheet").open) $("sheet").close();
    toast(
      action === "approve"
        ? tr("appr.approved", "Approved.")
        : action === "reject"
          ? tr("appr.rejected", "Rejected.")
          : tr("appr.sentback", "Sent back with your question."),
    );
    await refreshAfterAction();
  } catch (err) {
    /* Said plainly, in the page. An approval that silently does nothing is worse
       than one that says it failed. */
    toast(
      t_fmt("appr.couldnot_action", "Could not {a}: {e}", {
        a: action,
        e: err.message || err,
      }),
      true,
    );
  }
}

/* ---------------- agents: the roster and the switch ---------------- */
/* The kill switch, in the page, as a control. `kill_switch` existed for several
   tranches with constraints around it and no code path that could set it. */
document.addEventListener("click", async (e) => {
  const btn = e.target.closest("button[data-kill]");
  if (!btn) return;
  const { id } = btn.dataset;
  let control;
  try {
    control = await apiGet(`/agents/${encodeURIComponent(id)}/control`);
  } catch (err) {
    toast(
      t_fmt("agent.couldnot_read", "Could not read that agent: {e}", {
        e: err.message,
      }),
      true,
    );
    return;
  }
  if (control.kill_switch) {
    await revive(id);
    return;
  }
  const reason = window.prompt(
    tr(
      "agent.why_stop",
      "Why is this agent being stopped?\n\nThe reason is required — the database refuses a " +
        "kill without one, and it is written to the decision log.",
    ),
  );
  if (reason === null) return;
  if (!reason || reason.trim().length < 3) {
    toast(
      tr(
        "agent.need_reason",
        "A reason is required. A kill with no reason is indistinguishable from an accident.",
      ),
      true,
    );
    return;
  }
  try {
    await apiPost(`/agents/${encodeURIComponent(id)}/kill`, {
      reason: reason.trim(),
    });
    toast(tr("agent.stopped", "Stopped, and the decision logged."));
    await refreshAfterAction();
  } catch (err) {
    toast(
      t_fmt("agent.couldnot_stop", "Could not stop that agent: {e}", {
        e: err.message,
      }),
      true,
    );
  }
});

async function revive(id) {
  if (
    !window.confirm(
      tr(
        "agent.revive_q",
        "Start it again? It comes back at L1 — the old grant is not restored.",
      ),
    )
  )
    return;
  try {
    await apiPost(`/agents/${encodeURIComponent(id)}/revive`, {});
    toast(tr("agent.running_l1", "Running again, at L1."));
    await refreshAfterAction();
  } catch (err) {
    toast(
      t_fmt("agent.couldnot_revive", "Could not revive that agent: {e}", {
        e: err.message,
      }),
      true,
    );
  }
}

document.addEventListener("click", async (e) => {
  const box = e.target.closest("[data-agent-box]");
  if (box) {
    const d = await loadDepartments();
    if (!d) return;
    // **Every tier routes by the key the box carries.** This used to search
    // `d.offices` for departments and then special-case the chief, so an office box
    // matched neither and the click did nothing at all -- an office could be seen
    // and not opened. `findByKey` is the same lookup the route uses, so a box and
    // the address it opens can no longer disagree.
    const all = [d.chief, ...(d.second_tier || []), ...d.offices].filter(
      Boolean,
    );
    const o = all.find((x) => x.id === box.dataset.agentBox);
    if (!o || !o.key) {
      toast(tr("agent.nopanel", "That agent has no panel."), true);
      return;
    }
    go(`#/dept/${encodeURIComponent(o.key)}`);
    return;
  }
  const dept =
    e.target.closest("[data-key]") && e.target.closest("a[data-key]");
  if (dept) {
    deptSel = null;
  }
});

/* `renderChiefPanel` is gone.
   ------------------------------------------------------------------
   It was a private copy of the panel for the chief, reached two ways: by clicking
   the box, and by arriving at `#/dept/chief`. The click rendered it and the
   navigation immediately replaced it with "No such department" -- the click appeared
   to do nothing. It was also the only place that styled the chief's title, so a fix
   to the shared panel would have left the chief showing the old one.

   There is one panel now, reached by the same route for every tier, and the chief is
   found by `findByKey`. A fourth tier is a row in that function, not a copy of this
   function. */

/* The departments view's own state.
   **Kept beside the functions that use it, on purpose.** These three lived above the
   whole departments section, and cutting the sections around them removed the
   declarations while leaving the uses -- so the view failed with `deptCache is not
   defined`, which names a cache and not the thing that was actually wrong. */
let deptCache = null;
let deptSel = null;
let deptPoll = null;
/* What the organisation screen is filtered to. Search text plus one office key;
   both filter what is drawn, never what is fetched, and both survive re-renders
   because the tree is redrawn on every poll. */
let deptQuery = "";
let deptOffice = "";
let deptFilterWired = false;

function buildDeptOfficeChips(secondTier) {
  const seg = $("deptOfficeFilter");
  if (!seg) return;
  const offices = secondTier || [];
  const have = new Set([""]);
  for (const b of seg.querySelectorAll("button[data-office]"))
    have.add(b.dataset.office);
  const want = new Set(["", ...offices.map((o) => o.key)]);
  if ([...have].sort().join("|") === [...want].sort().join("|")) return;
  seg.innerHTML =
    `<button class="on" data-office="">${tr("dept.alloffices", "All offices")}</button>` +
    offices
      .map(
        (o) =>
          `<button data-office="${esc(o.key)}">${esc(o.label || o.name)}</button>`,
      )
      .join("");
  for (const b of seg.querySelectorAll("button[data-office]")) {
    b.classList.toggle("on", (b.dataset.office || "") === deptOffice);
  }
}

/** Hide boxes that do not match the search or the office chip. Groups whose every
    department is hidden are hidden too; the chief is never hidden by the office
    chip, only by the search. An empty result states so rather than drawing air. */
function applyDeptFilter() {
  const tree = $("deptTree");
  if (!tree) return;
  const q = deptQuery.trim().toLowerCase();
  let shown = 0;
  /* `box.style` is guarded, not assumed: the verification harness answers
     `querySelectorAll` with parsed wrappers that carry no `style` object, and an
     unguarded assignment throws inside the render — taking the whole departments
     view down in exactly the environment that guards it. In a browser `style`
     exists and the filter hides; under the harness the boxes stay visible, which
     is the correct degraded reading rather than a failed view. */
  for (const box of tree.querySelectorAll(".abox")) {
    const text = (box.textContent || "").toLowerCase();
    const okQ = !q || text.includes(q);
    const key = (box.getAttribute && box.getAttribute("data-box-key")) || "";
    const group = box.closest ? box.closest("[data-office-group]") : null;
    const inOffice =
      !deptOffice ||
      !group ||
      (group.getAttribute &&
        group.getAttribute("data-office-group") === deptOffice) ||
      key === deptOffice;
    const show = okQ && inOffice;
    if (box.style) box.style.display = show ? "" : "none";
    else if (!show && box.setAttribute)
      box.setAttribute("data-hidden-by-filter", "1");
    else if (show && box.removeAttribute)
      box.removeAttribute("data-hidden-by-filter");
    if (show) shown += 1;
  }
  for (const g of tree.querySelectorAll("[data-office-group]")) {
    const anyShown = [...g.querySelectorAll(".abox")].some((b) => {
      if (b.style) return b.style.display !== "none";
      return !(b.getAttribute && b.getAttribute("data-hidden-by-filter"));
    });
    if (g.style) g.style.display = anyShown ? "" : "none";
  }
  const noMatch = $("deptNoMatch");
  if (noMatch) noMatch.hidden = shown > 0;
}

function wireDeptFilter() {
  if (deptFilterWired) return;
  deptFilterWired = true;
  const input = $("deptSearch");
  if (input)
    input.addEventListener("input", () => {
      deptQuery = input.value || "";
      applyDeptFilter();
    });
  const seg = $("deptOfficeFilter");
  if (seg)
    seg.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-office]");
      if (!b) return;
      deptOffice = b.dataset.office || "";
      for (const x of seg.querySelectorAll("button[data-office]")) {
        x.classList.toggle("on", x === b);
      }
      applyDeptFilter();
    });
}

async function loadDepartments() {
  if (deptCache) return deptCache;
  try {
    deptCache = await apiGet("/departments");
  } catch (err) {
    showError(err);
    deptCache = null;
  }
  return deptCache;
}

async function renderDepartments(key) {
  const routeKey = location.hash;
  const d = await loadDepartments();
  if (!d || location.hash !== routeKey) return;

  if (key) return renderOneDepartment(d, key);

  // **Every agent in the payload, all three tiers.** It used to be
  // `[chief, ...departments]`, which skipped the offices and then said
  // "of 8 agents" over an organisation of 11 -- a number that was wrong by exactly
  // the tier that had just been added. The offices are boxes a person can see, so
  // they are counted like any other box.
  const boxes = [d.chief, ...(d.second_tier || []), ...d.offices].filter(
    Boolean,
  );
  const running = boxes.filter((b) => b.running).length;
  const stranded = boxes.reduce((n, b) => n + (b.stuck || 0), 0);
  const openTasks = boxes.reduce((n, b) => n + ((b.counts || {}).open || 0), 0);
  const attention = boxes.reduce(
    (n, b) => n + ((b.counts || {}).attention || 0),
    0,
  );
  // **No number in this sentence is typed.** It used to read "across the six" while
  // seven departments were drawn underneath it. A count that is correct in the same
  // sentence as a hard-coded one is a bug waiting for the seventh.
  const nOffices = (d.second_tier || []).length;
  // **The heading counts, too.** It read "The six departments" while seven were
  // drawn underneath it. A heading is the most-read sentence on a screen and it was
  // the one number on the page no test could reach, because it is not in the payload.
  $("deptHeading").textContent = t_fmt(
    "dept.heading",
    "The chief, {o} office(s) and {d} department(s)",
    { o: nOffices, d: d.offices.length },
  );
  setHTML(
    "deptStats",
    statTile(
      tr("tile.working", "Working now"),
      num(running),
      t_fmt("tile.of_agents", "of {n} agents", { n: num(boxes.length) }),
      running ? "good" : "",
    ) +
      statTile(
        tr("tile.open", "Open work"),
        num(openTasks),
        t_fmt("tile.across", "across {n} department(s)", {
          n: num(d.offices.length),
        }),
        openTasks ? "warn" : "good",
      ) +
      statTile(
        tr("tile.attention", "Needs attention"),
        num(attention),
        tr("tile.attention_sub", "failed or refused"),
        attention ? "alarm" : "good",
      ) +
      statTile(
        tr("tile.stranded", "Stranded runs"),
        num(stranded),
        tr("tile.stranded_sub", "started, never closed"),
        stranded ? "warn" : "good",
      ),
  );

  $("deptSub").textContent = t_fmt(
    "dept.sub",
    "{o} offices · {d} departments · {e} delegation(s) on record",
    { o: num(nOffices), d: num(d.offices.length), e: num(d.edges.length) },
  );
  $("deptEmpty").hidden = boxes.length > 0;
  $("deptTree").hidden = boxes.length === 0;
  setHTML(
    "deptTree",
    renderTree("deptTree", d.offices, d.chief, deptSel, d.second_tier) +
      unassignedNote(d),
  );
  buildDeptOfficeChips(d.second_tier);
  wireDeptFilter();
  applyDeptFilter();

  watchDepartments();
}

async function refreshAfterAction() {
  /* The nav count and whatever view is on screen both change, so both are refreshed
     rather than guessing which one the person is looking at. */
  try {
    const inbox = await apiGet("/approvals/inbox");
    state.inbox = inbox.items;
    setHTML("navApprovalCount", inbox.count ? String(inbox.count) : "");
    const _nbA2 = $("navApprovalCount");
    if (_nbA2) _nbA2.hidden = !inbox.count;
  } catch {
    /* the view refresh below will surface it */
  }
  await route();
}

function agentBox(a, sel) {
  const cls =
    "abox" +
    (sel ? " sel" : "") +
    (a.running ? " on" : "") +
    (!a.running && a.stuck ? " stuck" : "");
  const c = a.counts || {};
  const r = a.rollup || null;
  return `<div class="${cls}" data-agent-box="${esc(a.id)}" data-agent-name="${esc(a.name)}"
      data-box-key="${esc(a.key || "")}"
      role="button" tabindex="0" aria-label="${esc(a.name)}">
    <div class="n">${esc(a.label || a.name)}</div>
    ${
      // The subtitle only earns its place when it says something the title does
      // not. The chief has no `label`, so its title falls back to its name -- and
      // printing the name again underneath made the one box on the page that is
      // not a department the one box that looked like a mistake.
      a.label && a.label !== a.name
        ? `<div class="r">${esc(a.name)}${a.killed ? " · stopped" : ""}</div>`
        : ""
    }
    <div class="c">
      <span>${tr("box.own_open", "own open")} <b>${c.open ?? 0}</b></span>
      <span>${tr("box.own_done", "own done")} <b>${c.done ?? 0}</b></span>
      ${c.attention ? `<span>${tr("box.stuck", "stuck")} <b>${c.attention}</b></span>` : ""}
      ${a.stuck ? `<span>${tr("box.stranded", "stranded")} <b>${a.stuck}</b></span>` : ""}
      <span>${esc(a.granted || "L1")}/${esc(a.ceiling || "L1")}</span>
    </div>
    ${
      // **Own and rolled-up, both on the box and both named.**
      //
      // An office that only shows its own counts reads as idle: it delegates, so its
      // own open count is zero while its departments are busy. Showing only the
      // roll-up would be the opposite error -- a chief that reports 11 agents but
      // cannot say it did nothing itself. So the two lines stay separate and are
      // labelled "own" and "below".
      //
      // `r.agents - 1` is the number under it, and it is zero for a leaf, so a
      // department shows no roll-up line at all rather than a line about nothing.
      r && r.agents > 1
        ? `<div class="roll">${t_fmt(
            "box.roll",
            "{n} below · open <b>{o}</b> · done <b>{d}</b>{att} · <b>{n}</b> agent(s) in total under here",
            {
              n: num(r.agents - 1),
              o: num(r.open - (c.open ?? 0)),
              d: num(r.done - (c.done ?? 0)),
              att:
                r.attention > (c.attention ?? 0)
                  ? t_fmt("box.roll_att", " · needs attention <b>{a}</b>", {
                      a: num(r.attention - (c.attention ?? 0)),
                    })
                  : "",
            },
          )}</div>`
        : ""
    }
  </div>`;
}

function renderTree(target, offices, chief, selId, secondTier) {
  const parts = [];
  if (chief) {
    parts.push(
      `<div class="dept-tier">${esc(
        chief.key === "chief"
          ? tr("tier.chief", "Chief")
          : chief.label || chief.name,
      )}</div>`,
    );
    parts.push(
      agentBox(
        { ...chief, label: chief.label || chief.name },
        selId === chief.id,
      ),
    );
  }
  const offices2 = secondTier || [];
  const depts = offices || [];
  if (offices2.length) {
    parts.push(
      `<div class="dept-tier">${tr("tier.offices", "Offices & their departments")}</div>`,
    );
    for (const o of offices2) {
      // Only departments this office actually owns. An empty group is still drawn --
      // an office with nothing under it is a real state, and hiding it would make a
      // tier disappear whenever its departments happened to be idle.
      const mine = depts.filter((d) => d.parent_unit_slug === o.key);
      parts.push(`<div class="dept-group" data-office-group="${esc(o.key)}">`);
      parts.push(agentBox({ ...o, label: o.label || o.name }, selId === o.id));
      if (mine.length) {
        parts.push(`<div class="dept-edges">`);
        for (const d of mine) parts.push(agentBox(d, selId === d.id));
        parts.push(`</div>`);
      }
      parts.push(`</div>`);
    }
  } else if (depts.length) {
    // No office tier in this payload: the departments still have to be drawn, or the
    // page loses them. Falling back to a flat list here is worse than the nested view,
    // but it is better than an empty panel.
    parts.push(
      `<div class="dept-tier">${tr("tier.departments", "Departments")}</div>`,
    );
    for (const d of depts) {
      if (d.missing) {
        parts.push(`<div class="abox stuck"><div class="n">${esc(d.label)}</div>
          <div class="r">${t_fmt("dept.noagent", "no agent named {n}", { n: esc(d.name) })}</div></div>`);
        continue;
      }
      parts.push(agentBox(d, selId === d.id));
    }
  }
  return parts.join("");
}

function unassignedNote(d) {
  const extra = (d.tree && d.tree.unassigned) || [];
  if (!extra.length) return "";
  return `<div class="note" style="margin-top:10px">
    ${t_fmt(
      "dept.unassigned",
      "<b>{n} department(s) name no office</b> and are not in the tree above: {who}. They are counted in the totals.",
      { n: num(extra.length), who: esc(extra.map((x) => x.label).join(", ")) },
    )}
  </div>`;
}

function findByKey(d, key) {
  if (d.chief && d.chief.key === key) return d.chief;
  const inOffices = (d.second_tier || []).find((x) => x.key === key);
  if (inOffices) return inOffices;
  return d.offices.find((x) => x.key === key) || null;
}

function tierOf(d, box) {
  if (d.chief && d.chief.id === box.id) return "chief";
  if ((d.second_tier || []).some((x) => x.id === box.id)) return "office";
  return "department";
}

async function renderOneDepartment(d, key) {
  const o = findByKey(d, key);
  if (!o) {
    toast(tr("dept.nosuch", "No such department."), true);
    go("#/departments");
    return;
  }
  const tier = tierOf(d, o);
  deptSel = o.id;
  $("deptControlLink").hidden = Boolean(o.missing);
  $("deptControlLink").href = "#/agent/" + encodeURIComponent(o.id);
  $("deptTitle").textContent = o.missing ? o.label : o.label + " — " + o.name;
  $("deptSub2").textContent = o.missing
    ? tr("dept.noagent", "no agent registered")
    : t_fmt(
        "dept.panel_sub",
        "{g} granted, ceiling {c} · model {m} · {r} run(s)",
        { g: o.granted, c: o.ceiling, m: o.model, r: o.run_count },
      );
  // One label for every tier, because it goes to the same place: the whole
  // organisation. It used to read "All six" on a page that shows seven, and a
  // tier-specific wording ("All offices") is wrong in a different way -- it suggests
  // an office-only view exists behind the button, and it does not.
  $("deptBack").textContent = tr("dept.back", "Back to the organisation");
  $("deptBack").onclick = () => go("#/departments");
  // Its own departments, drawn under it. An office is a group, so its panel shows
  // the group; showing a single box on a panel whose subject is a group of five
  // agents would understate what the office is.
  const own =
    tier === "office"
      ? d.offices.filter((x) => x.parent_unit_slug === o.key)
      : [];
  setHTML("deptOneTree", renderTree("deptOneTree", own, o, o.id, []));

  if (o.missing) {
    setHTML("runSteps", "");
    $("runEmpty").hidden = false;
    $("workOpen").innerHTML =
      $("workAttn").innerHTML =
      $("workDone").innerHTML =
        "";
    $("deptNote").textContent = "";
    return;
  }

  $("runSub").textContent = t_fmt("dept.runs", "{n} recent run(s)", {
    n: (o.runs || []).length,
  });
  $("runEmpty").hidden = (o.runs || []).length > 0;
  setHTML(
    "runSteps",
    (o.runs || [])
      .map(
        (r) => `
    <details class="step${r.running ? " live" : ""}">
      <summary>${esc(statusName(r.status))} · ${r.started_at ? esc(ago(r.started_at)) : ""} · ${t_fmt("dept.tok", "{n} tok", { n: num(r.tokens) })}</summary>
      ${r.task_id ? `<a href="#/give/${encodeURIComponent(r.task_id)}/steps/${encodeURIComponent(r.id)}">${tr("dept.open_run", "Open this execution")}</a>` : ""}
      <div class="h"><span>${esc(r.status)}</span>
        <span>${esc(r.model_used || "—")}</span>
        <span>${t_fmt("dept.tok", "{n} tok", { n: num(r.tokens) })}</span>
        <span>${r.duration_ms != null ? t_fmt("dept.secs", "{n}s", { n: Math.round(r.duration_ms / 1000) }) : ""}</span>
        <span>${r.started_at ? esc(ago(r.started_at)) : ""}${r.stuck ? tr("dept.stranded_flag", " · stranded") : ""}</span>
      </div>
      <div class="b">${readable(r.summary)}</div>
      ${r.error_message ? `<div class="b" style="color:var(--bad)">${esc(r.error_message.slice(0, 300))}</div>` : ""}
    </details>`,
      )
      .join(""),
  );

  const c = o.counts || {};
  const r = o.rollup || null;
  // **Own and rolled-up, both shown.** A unit above the leaves delegates rather than
  // executes, so its own open count is normally zero while its departments hold the
  // work. Showing only `own` makes an office look idle; showing only `rollup` would
  // hide that the chief itself did nothing this hour. Both, labelled.
  $("unitWorkSub").textContent =
    r && r.agents > 1
      ? t_fmt(
          "dept.roll_sub",
          "own: {co} open · {ca} need attention · {cd} done — with {n} below: {o} open · {a} need attention · {d} done",
          {
            co: c.open ?? 0,
            ca: c.attention ?? 0,
            cd: c.done ?? 0,
            n: num(r.agents - 1),
            o: num(r.open),
            a: num(r.attention),
            d: num(r.done),
          },
        )
      : t_fmt("dept.own_sub", "{co} open · {ca} need attention · {cd} done", {
          co: c.open ?? 0,
          ca: c.attention ?? 0,
          cd: c.done ?? 0,
        });
  const row = (
    t,
    kind,
    who,
  ) => `<a class="taskitem ${kind}" href="#/give/${encodeURIComponent(t.id)}">
      <span class="st">${esc(t.status)}</span>
      <span class="grow">${who ? `${esc(who)} — ${esc(t.title)}` : esc(t.title)}</span>
      ${t.error ? `<span class="s">${esc(String(t.error).slice(0, 90))}</span>` : ""}
    </a>`;
  // For a unit with departments, the work lists include its departments' work too, or
  // the panel answers "what is this unit doing" with a list that is empty by design --
  // an office delegates, so its own open tasks are usually zero while all the work is
  // one tier down. The owning department is named in each row, because "its own" and
  // "its departments'" are different claims and merging them silently would make the
  // number above mean something it does not.
  const take = (field, kind) =>
    own.flatMap((x) => (x[field] || []).map((t) => row(t, kind, x.label)));
  setHTML(
    "workOpen",
    (o.open_tasks || []).map((t) => row(t, "open")).join("") +
      take("open_tasks", "open").join(""),
  );
  setHTML(
    "workAttn",
    (o.attention || []).map((t) => row(t, "attn")).join("") +
      take("attention", "attn").join(""),
  );
  setHTML(
    "workDone",
    (o.done_tasks || []).map((t) => row(t, "done")).join("") +
      take("done_tasks", "done").join(""),
  );
  $("unitWorkEmpty").hidden =
    (o.open_tasks || []).length +
      (o.done_tasks || []).length +
      (o.attention || []).length +
      own.reduce(
        (n, x) =>
          n +
          (x.open_tasks || []).length +
          (x.attention || []).length +
          (x.done_tasks || []).length,
        0,
      ) >
    0;
  $("deptNote").innerHTML = o.last_run
    ? t_fmt(
        "dept.last_run",
        "<b>Last run:</b> {s} on {m} — {n} tokens. The summary above is the same string a workflow branches on, so the panel cannot say one thing and the engine another.",
        {
          s: esc(o.last_run.status),
          m: esc(o.last_run.model_used || "—"),
          n: num(o.last_run.tokens),
        },
      )
    : tr(
        "dept.never_report",
        "It has not run yet, so there is nothing to report.",
      );
}

/* Poll while the page is open, so a light that comes on is noticed without a reload.
   Ten seconds: a run is measured at 644 seconds, so a faster poll would spend requests
   to watch nothing happen, and a slower one would make the light feel broken. */
function watchDepartments() {
  if (deptPoll) clearInterval(deptPoll);
  deptPoll = setInterval(async () => {
    if (parseHash().name !== "departments" && parseHash().name !== "dept")
      return;
    // **All three tiers, or the office tier never refreshes.** It compared the chief
    // and the departments only, so an office whose light came on kept polling and
    // never redrew -- the poller reporting nothing changed about a tier it was not
    // watching.
    const lights = (p) =>
      JSON.stringify(
        [p.chief?.running, p.chief?.stuck]
          .concat((p.second_tier || []).map((o) => [o.running, o.stuck]))
          .concat((p.offices || []).map((o) => [o.running, o.stuck])),
      );
    const before = lights(deptCache || {});
    const fresh = await apiGet("/departments").catch(() => null);
    if (!fresh) return;
    const after = lights(fresh);
    if (before === after) return;
    deptCache = fresh;
    await renderDepartments(parseHash().arg);
  }, 10000);
}

/* ====================== give work ======================
   The CEO's flow, in the order it happens: pick a task, watch it be handed out, decide,
   then read the account of it. The tree and the log are on the same screen as the
   decisions, because a decision taken away from the evidence of what it is deciding about
   is a decision made blind. */
let giveFilter = "";
const taskNames = new Map();

function taskName(id) {
  return taskNames.get(id) || "Task";
}

function waitingTone(w) {
  return w === "you" ? "warn" : w === "an_agent" ? "" : "good";
}

/* The delegation graph, drawn from the tree.
   ------------------------------------------------------------------
   Laid out by `depth`, which is the only ordering the data actually
   supports: `delegations.depth` is set by the executor, and guessing
   the shape from timestamps would put a parent to the left of its
   child the moment two were delegated in the same second.

   An SVG rather than nested divs, because a delegation graph is a
   graph: the edges are the evidence, and a list of edges is a table
   of a tree. `role="img"` with a label, so a screen reader gets the
   shape rather than eight empty rectangles. */
function delegationGraph(root, tree, delegations) {
  if (!root) return "";
  /* The **structure** comes from `tasks.parent_task_id`, not from
     `delegations.child_task_id` -- because `child_task_id` is NULL on every
     delegation the real executor has written. Measured: 7 delegations, 7 nulls.
     The first version built the graph from the delegation edges and drew a
     flat list of boxes with no lines between them, which is a table wearing
     a graph's clothes.

     The delegations are still used, for what they know that the task tree
     does not: *which* edge, to whom, and whether it was accepted or refused. */
  const children = new Map();
  for (const t of tree) {
    if (!t.parent_task_id) continue;
    if (!children.has(t.parent_task_id)) children.set(t.parent_task_id, []);
    children.get(t.parent_task_id).push(t);
  }
  const byId = new Map(tree.map((t) => [t.id, t]));
  // parent -> the delegation that describes the handoff, matched on the parent
  // and the objective's leading words, because there is no child id to match on.
  const edgeOf = new Map();
  for (const dg of delegations) {
    edgeOf.set(
      dg.parent_task_id + "|" + String(dg.objective || "").slice(0, 40),
      dg,
    );
  }
  const W = 900,
    ROW = 58,
    PAD = 26;
  const placed = new Map();

  // Depth from the root, walking down `children`. A node the walk does
  // not reach is still drawn -- at the bottom, unlinked -- because a
  // delegation whose parent is missing is a fact, not a reason to
  // draw less.
  (function place(id, depth) {
    if (placed.has(id)) return;
    placed.set(id, { depth, x: PAD + depth * 300 });
    const y = 40 + (placed.size - 1) * ROW;
    placed.get(id).y = y;
    for (const kid of children.get(id) || []) place(kid.id, depth + 1);
  })(root, 0);

  const edgeMarkup = [...placed.entries()]
    .filter(([id]) => {
      const t = byId.get(id);
      return t && t.parent_task_id && placed.has(t.parent_task_id);
    })
    .map(([id, at]) => {
      const child = byId.get(id);
      const a = placed.get(child.parent_task_id);
      const dg =
        edgeOf.get(
          child.parent_task_id + "|" + String(child.title || "").slice(0, 40),
        ) || null;
      const x1 = a.x + 236,
        y1 = a.y + 18,
        x2 = at.x,
        y2 = at.y + 18;
      const mid = (x1 + x2) / 2;
      const refused =
        dg && dg.status !== "accepted" && dg.status !== "completed";
      const who = dg
        ? (dg.from_agent || "the agent") +
          " to " +
          (dg.to_agent || "a department")
        : "handed off";
      return `<path d="M${x1},${y1} C${mid},${y1} ${mid},${y2} ${x2},${y2}"
      fill="none" stroke="${refused ? "var(--bad)" : "var(--accent)"}"
      stroke-width="1.6" stroke-dasharray="${refused ? "4 3" : "none"}"
      opacity="0.65" marker-end="url(#arrow)"
      ><title>${esc(who)}${dg ? ": " + esc(dg.status) : ""}</title></path>`;
    })
    .join("");

  const nodeMarkup = [...placed.entries()]
    .map(([id, at]) => {
      const t = byId.get(id);
      const done = t && t.status === "completed";
      const failed = t && t.status === "failed";
      const title = t ? t.title : id;
      return `<a href="#/give/${encodeURIComponent(id)}"><g transform="translate(${at.x},${at.y})">
      <rect width="236" height="36" rx="7"
        fill="${failed ? "var(--bad-soft)" : done ? "var(--ok-soft)" : "var(--panel)"}"
        stroke="${failed ? "var(--bad)" : done ? "var(--ok)" : "var(--line)"}"/>
      <text x="10" y="15" class="g-t">${esc(String(title).slice(0, 30))}</text>
      <text x="10" y="28" class="g-s">${esc(t ? t.status : "no task row")}</text>
      ${t && t.owner_name ? `<text x="226" y="28" class="g-s" text-anchor="end">${esc(String(t.owner_name).slice(0, 18))}</text>` : ""}
      <title>${esc(title)}</title>
    </g></a>`;
    })
    .join("");

  const h = 56 + placed.size * ROW;
  return `<svg viewBox="0 0 ${W} ${h}" width="100%" height="${h}" role="img"
      aria-label="${t_fmt("task.graph_aria", "Delegation graph: {t} tasks, {d} delegations", { t: placed.size, d: delegations.length })}">
    <defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5"
      markerWidth="6" markerHeight="6" orient="auto-start-reverse">
      <path d="M0,0 L10,5 L0,10 z" fill="var(--accent)" opacity="0.6"/></marker></defs>
    <text x="${PAD}" y="22" class="g-h">${esc(root)}</text>
    ${edgeMarkup}${nodeMarkup}
  </svg>`;
}

let registerSnapshot = null;
async function renderGive(id, refresh = true) {
  // **The detail screen is a screen, not a fill.** `renderOneTask` wrote into
  // `view-task` while the router had shown `view-give`, so every task opened
  // from the register showed the global list under a breadcrumb naming the
  // task — the workflow tree full of unrelated work, the task itself nowhere.
  // The checks read the nodes either way and stayed green; a person could not
  // reach their task at all. Visibility is asserted now, not just content.
  if (id) {
    $("view-give").hidden = true;
    $("view-task").hidden = false;
    return renderOneTask(id);
  }
  $("view-task").hidden = true;
  $("view-give").hidden = false;

  // Leaving a task releases its detail -- the same rule the project and document views
  // hold, and `verify-page` checks it on each. A stale panel under a fresh list is how a
  // person reads one thing and believes another.
  setHTML("taskStats", "");
  setHTML("taskTree", "");
  setHTML("taskLog", "");
  setHTML("taskApprovals", "");
  setHTML("taskNote", "");
  setHTML("taskGraph", "");
  setHTML("taskBanner", "");
  setHTML("taskSteps", "");
  stepTexts = [];
  setHTML("taskAnswer", "");
  setHTML("taskAnswerSub", "");
  const _ac = $("taskAnswerCopy");
  if (_ac) _ac.hidden = true;
  const _ae = $("taskAnswerEmpty");
  if (_ae) _ae.hidden = true;
  $("taskTitle").textContent = "Task";
  $("taskSub").textContent = "";
  $("runThisState").textContent = "";

  let q = {};
  try {
    q = refresh || !registerSnapshot ? await loadRegister() : registerSnapshot;
    registerSnapshot = q;
  } catch (err) {
    showError(err);
  }

  if (parseHash().arg || !["give", "console"].includes(parseHash().name))
    return;
  const needsYou = q.needs_you ?? 0;
  setHTML(
    "workStats",
    statTile(
      tr("reg.waiting", "Waiting on you"),
      num(needsYou),
      tr("reg.waiting_sub", "need a decision or an owner"),
      needsYou ? "warn" : "good",
    ) +
      statTile(
        tr("reg.agent", "With an agent"),
        num(q.in_flight ?? 0),
        tr("reg.agent_sub", "running or assigned"),
      ) +
      statTile(
        tr("reg.settled", "Settled"),
        num(q.settled ?? 0),
        tr("reg.settled_sub", "finished and reported"),
      ) +
      statTile(
        tr("reg.total", "Total"),
        num(q.total ?? 0),
        tr("reg.total_sub", "tasks on the register"),
      ),
  );

  const badge = $("navWorkCount");
  if (badge) {
    badge.textContent = needsYou ? String(needsYou) : "";
    badge.hidden = true;
  }

  const all = q.items || [];
  for (const t of all) taskNames.set(t.id, t.title);
  const filtered = !giveFilter
    ? all
    : all.filter((t) =>
        giveFilter === "you"
          ? t.waiting_on === "you"
          : giveFilter === "in_flight"
            ? t.waiting_on === "an_agent"
            : t.waiting_on === "nobody",
      );
  const query = $("taskSearch").value.trim().toLocaleLowerCase();
  const rows = filtered.filter(
    (t) =>
      !query ||
      [t.title, t.owner_name, t.id].some((v) =>
        String(v || "")
          .toLocaleLowerCase()
          .includes(query),
      ),
  );
  $("registerCount").textContent = `${num(rows.length)} / ${num(q.total || 0)}`;

  $("giveEmpty").hidden = rows.length > 0;
  $("giveList").hidden = rows.length === 0;
  // Dựng feed ngay khi mở màn hình, không chờ khi có frame. Nếu không, một người mở
  // Give work lúc không ai làm việc gì thấy trống trơn rồi tưởng hỏng — và nếu chờ
  // frame thì lúc đang có việc thì mới thấy, tức là feed chỉ hữu ích đúng lúc
  // không cần đến nó nhất.
  renderActivity();
  setHTML(
    "giveList",
    rows
      .map(
        (it) => `
    <a class="row" data-task="${esc(it.id)}" href="#/give/${encodeURIComponent(it.id)}">
      <div class="grow">
        <div class="t">${esc(it.title)}</div><div class="task-id">${esc(it.id)}</div>
        <div class="s">${esc(it.task_type)} · ${esc(statusName(it.status))}
          ${it.owner_name ? t_fmt("row.held", " · held by {n}", { n: esc(it.owner_name) }) : tr("row.noowner", " · nobody holds it")}
          ${it.delegated ? t_fmt("row.handed", " · handed to {n}", { n: it.delegated }) : ""}
          ${it.children ? t_fmt("row.subs", " · {n} subtask(s)", { n: it.children }) : ""}</div>
        ${it.last_error ? `<div class="s">${t_fmt("row.refused", "refused: {e}", { e: esc(String(it.last_error).slice(0, 120)) })}</div>` : ""}
      </div>
      <div class="r">
        ${it.approvals_waiting ? `<span class="pill bad">${t_fmt("row.todecide", "{n} to decide", { n: it.approvals_waiting })}</span>` : ""}
        <span class="t-${waitingTone(it.waiting_on) || "muted"}">${esc(it.waiting_on === "you" ? tr("row.you", "you") : it.waiting_on === "an_agent" ? tr("row.agent", "an agent") : tr("row.settled", "settled"))}</span>
      </div>
    </a>`,
      )
      .join(""),
  );
  // Keep watching while the list is on screen; route() stops it on leave.
  watchGive();
}

/* ================= task banner, steps, and the full-text sheet =================
   Three things the task page was missing, in the order a boss asks: what happened (the
   banner), what did each hand do (the steps), and what exactly did it say (the sheet).
   ================================================================== */

/* Poll the register while Give work is open, so progress arrives without a
   reload. Ten seconds: runs are minutes long, and the stream already redraws
   the tree and the feed on every frame — this covers the register and the
   tiles, which only a fetch refreshes. Stops the moment the screen is left. */
let givePoll = null;
function watchGive() {
  if (givePoll) return;
  givePoll = setInterval(async () => {
    if (parseHash().name !== "give" || parseHash().arg) return;
    try {
      await renderGive();
    } catch {
      /* the next tick retries */
    }
  }, 10000);
}

/** One status banner for the open task. The failure reason used to live only in a list
    truncation and a button tooltip; here it is the first thing on the screen. */
function renderAnswer(t) {
  /* The task's own answer, read as sentences rather than as key names. `output` is a
     `{"verdicts": "...", "reason": "..."}` map a boss cannot act on; each value is
     shown under its plain name, long values truncate with the full text one click
     away in the existing sheet dialog, and the copy button takes the whole answer. */
  const el = $("taskAnswer");
  if (!el) return;
  const out = t.output && typeof t.output === "object" ? t.output : null;
  const keys = out
    ? Object.keys(out).filter((k) => out[k] !== null && out[k] !== "")
    : [];
  const sub = $("taskAnswerSub");
  const copy = $("taskAnswerCopy");
  const empty = $("taskAnswerEmpty");
  if (!keys.length) {
    setHTML("taskAnswer", "");
    if (sub)
      sub.textContent =
        t.status === "completed"
          ? tr("task.finished_empty", "finished with no written answer")
          : tr("task.nothing_yet", "nothing written yet");
    if (copy) copy.hidden = true;
    if (empty) empty.hidden = false;
    el.hidden = true;
    return;
  }
  el.hidden = false;
  if (empty) empty.hidden = true;
  if (sub)
    sub.textContent = t_fmt("task.points", "{n} point(s) answered", {
      n: keys.length,
    });
  if (copy) {
    copy.hidden = false;
    copy.onclick = async () => {
      const text = keys.map((k) => `${k}: ${plain(out[k])}`).join("\n\n");
      try {
        await navigator.clipboard.writeText(text);
        toast(tr("task.copied", "Answer copied."));
      } catch {
        toast(tr("task.copyfail", "Could not copy."), true);
      }
    };
  }
  answerTexts = keys.map((k) => plain(out[k]));
  setHTML(
    "taskAnswer",
    `<dl class="answer">` +
      keys
        .map(
          (k, i) => `
    <div class="answer-row">
      <dt>${esc(humanKey(k))}</dt>
      <dd>${esc(String(plain(out[k])).slice(0, 600))}${
        String(plain(out[k])).length > 600
          ? ` <button class="btn ghost sm" data-answertext="${i}">${tr("task.readall", "Read all")}</button>`
          : ""
      }</dd>
    </div>`,
        )
        .join("") +
      `</dl>`,
  );
}

/** Full answer texts, by key index — same rule as `stepTexts`: stored, not embedded. */
let answerTexts = [];

/** Answer text and clipboard content retain all fields as readable words. */
function plain(v) {
  return valueText(v);
}

/** `proposed_changes` reads as "Proposed changes". Keys are identifiers; the screen
    is not the place to mint them. */
function humanKey(k) {
  return String(k)
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}
function renderBanner(t, s) {
  const el = $("taskBanner");
  if (!el) return;
  const status = t.status || "unknown";
  if (
    ["failed", "blocked", "canceled", "cancelled", "expired"].includes(status)
  ) {
    const kind = t.failure_category || "failed";
    const explanation =
      kind === "model_error"
        ? tr(
            "task.model_unavailable",
            "No model could answer this attempt. Open the error details for each provider's reason.",
          )
        : tr(
            "task.stopped",
            "This attempt stopped. Read the reason and recorded steps before retrying.",
          );
    setHTML(
      "taskBanner",
      `<div class="card alarm"><div class="body">
      <div class="t"><span class="pill bad">${esc(statusName(status))}</span> ${explanation}</div>
      ${t.last_error ? `<details style="margin-top:8px"><summary style="cursor:pointer;color:var(--text-2)">${tr("task.error_details", "Error details")}</summary>${reasonBlock(t.last_error)}</details>` : ""}
      <a href="#/give/${encodeURIComponent(t.id)}/steps">${tr("task.read_steps", "Read the recorded steps")}</a>
    </div></div>`,
    );
    return;
  }
  if (status === "completed") {
    const keys =
      t.output && typeof t.output === "object" ? Object.keys(t.output) : [];
    setHTML(
      "taskBanner",
      `
      <div class="card good">
        <div class="body">
          <div class="t"><span class="pill ok">finished</span>
            <span style="font-weight:650">${esc(t.title || "This task")}</span></div>
          <div class="s">${
            keys.length
              ? tr("task.answered_with", "Answered with: ") +
                keys.map((k) => `<code>${esc(k)}</code>`).join(", ")
              : tr(
                  "task.finished_steps",
                  "Finished. The steps below say what each hand did.",
                )
          }</div>
        </div>
      </div>`,
    );
    return;
  }
  setHTML(
    "taskBanner",
    `
    <div class="card">
      <div class="body">
        <div class="t"><span class="pill">${esc(status)}</span>
          <span style="font-weight:650">${esc(t.title || "This task")}</span></div>
        <div class="s">${t.owner_name ? t_fmt("task.held", "Held by {n}. ", { n: esc(t.owner_name) }) : ""}
          ${
            (s.in_flight ?? 0)
              ? tr(
                  "task.inflight",
                  "Work is in flight — this page keeps watching. ",
                )
              : status === "waiting_for_approval"
                ? tr(
                    "task.waiting",
                    "Waiting for a human decision. Open Decisions to read the draft.",
                  )
                : tr("task.idle", "Ready to start when an owner is assigned.")
          }</div>
      </div>
    </div>`,
  );
}

/** Full texts of step summaries, by index. Stored rather than embedded because a 10KB
    summary inside an attribute is a quoting bug waiting to happen. */
let stepTexts = [];

/** Open the sub-screen popup with a full model output. Reuses the existing `<dialog>`,
    which until now had no caller — a control with no path to it. */
function openSheet(title, bodyHtml) {
  setHTML(
    "sheetBody",
    `<h3 style="margin:0 0 8px">${esc(title)}</h3>` + bodyHtml,
  );
  setHTML(
    "sheetFoot",
    `<button class="btn sm" id="sheetClose">${tr("sheet.close", "Close")}</button>`,
  );
  const dlg = $("sheet");
  if (dlg && !dlg.open) dlg.showModal();
  const c = $("sheetClose");
  if (c) c.onclick = () => dlg.close();
}

/** One step's pointers: what was done, any issue, what it cost. Derived from the
    execution record — never generated prose, so nothing here can hallucinate. */
function stepPointers(x) {
  const bits = [];
  bits.push(
    x.status === "completed"
      ? tr("step.done", "Done.")
      : t_fmt("step.ended", "Ended as {s}.", { s: x.status }),
  );
  if (x.error_message)
    bits.push(
      t_fmt("step.issue", "Issue: {e}", {
        e: String(x.error_message).slice(0, 160),
      }),
    );
  else if (x.error_kind)
    bits.push(t_fmt("step.issuekind", "Issue kind: {k}.", { k: x.error_kind }));
  // **Sum numbers, then format.** `num()` returns a formatted *string*, so adding two
  // of them concatenates ("120" + "80" = "12080"). The first version of this line did
  // exactly that and the cost read ten times too high on every step.
  const tokens = (x.input_tokens ?? 0) + (x.output_tokens ?? 0);
  const cost = [
    x.model_used ? String(x.model_used) : "",
    x.input_tokens != null || x.output_tokens != null
      ? `${fmtTokens(tokens)} tokens`
      : "",
    x.duration_ms != null ? `${Math.round(x.duration_ms / 1000)}s` : "",
  ]
    .filter(Boolean)
    .join(" · ");
  if (cost) bits.push(t_fmt("step.cost", "Cost: {c}.", { c: cost }));
  if (x.artifacts && x.artifacts.length)
    bits.push(
      t_fmt("step.files", "{n} file(s) kept.", { n: x.artifacts.length }),
    );
  return bits;
}

function fmtTokens(n) {
  if (n == null) return "—";
  return Number(n).toLocaleString("en-US");
}

/** The steps: every execution under the task, in order, each expandable to its log and
    details. A step is a `<details>` so it is keyboard-operable; the summary line is the
    pointers and the body is the evidence. */
function renderSteps(d) {
  const steps = d.tree_executions || [];
  stepTexts = steps.map((x) => String(x.summary || ""));
  $("taskStepsSub").textContent = steps.length
    ? t_fmt("step.sub", "{n} hand(s) · {f} finished", {
        n: steps.length,
        f: steps.filter((x) => x.status === "completed").length,
      })
    : "";
  $("taskStepsEmpty").hidden = steps.length > 0;
  const events = d.events || [];
  setHTML(
    "taskSteps",
    steps
      .map((x, i) => {
        const related = events.filter(
          (e) =>
            (e.view && e.view.task_id && e.view.task_id === x.task_id) ||
            e.subject === x.task_id,
        );
        const when = x.started_at ? ago(x.started_at) : "";
        return `
    <details class="row issue" data-step="${esc(x.id)}">
      <summary class="grow">
        <div class="t"><span class="pill ${x.status === "completed" ? "ok" : x.status === "failed" ? "del" : ""}">${esc(statusName(x.status))}</span>
          ${esc(x.agent_name || "an agent")} <span class="s">on ${esc(x.task_title || x.task_id)}</span></div>
        <div class="s">${esc(stepPointers(x).join(" "))}</div>
        <div class="s">${when ? esc(when) : ""}${x.duration_ms != null ? ` · ${Math.round(x.duration_ms / 1000)}s` : ""}${
          x.input_tokens != null
            ? ` · ${fmtTokens((x.input_tokens ?? 0) + (x.output_tokens ?? 0))} tokens`
            : ""
        }</div>
      </summary>
      <div class="issue-body">
        ${x.error_message ? reasonBlock(tr("step.stopped", "Stopped with: ") + x.error_message) : ""}
        ${
          x.summary
            ? `<div class="s">${esc(String(x.summary).slice(0, 400))}${String(x.summary).length > 400 ? "…" : ""}</div>
          <div class="btn-row"><button class="btn sm" data-fulltext="${i}">${tr("step.readall", "Read the whole thing")}</button></div>`
            : `<div class="s">${tr("step.nooutput", "This run left no written output.")}</div>`
        }
        ${
          related.length
            ? `<div class="dept-tier" style="margin:8px 0 4px">${t_fmt("step.logfor", "Log for this step ({n})", { n: related.length })}</div>` +
              related
                .map(
                  (e) =>
                    `<div class="s" style="font-size:12px">· ${eventLine(e)}</div>`,
                )
                .join("")
            : ""
        }
        <div class="s" style="margin-top:6px">${tr("step.model", "Model:")} ${esc(x.model_used || "—")}${x.model_profile ? ` · ${tr("step.profile", "Profile:")} ${esc(x.model_profile)}` : ""}${
          x.finished_at
            ? tr("step.finished", " · finished ") + esc(ago(x.finished_at))
            : ""
        }</div>
      </div>
    </details>`;
      })
      .join(""),
  );
}

function statusName(status) {
  return tr(`status.${status}`, String(status || "—").replaceAll("_", " "));
}
async function loadRegister() {
  const first = await apiGet("/ceo/work", { limit: 200 });
  const items = [...(first.items || [])];
  while (items.length < first.total) {
    const next = await apiGet("/ceo/work", {
      limit: 200,
      offset: items.length,
    });
    if (!next.items?.length) break;
    items.push(...next.items);
  }
  return { ...first, items };
}
function renderTaskSections(id) {
  const r = parseHash();
  const sections = [
    ["overview", "task.answer", "Answer"],
    ["steps", "task.steps", "Steps"],
    ["assignment", "task.assignment", "Assignment"],
    ["decisions", "task.decisions", "Decisions"],
    ["log", "task.log", "Log"],
  ];
  const selected = sections.some(([key]) => key === r.section)
    ? r.section
    : "overview";
  setHTML(
    "taskTabs",
    sections
      .map(
        ([
          key,
          label,
          fallback,
        ]) => `<a href="#/give/${encodeURIComponent(id)}/${key}"
    ${key === selected ? 'aria-current="page"' : ""}>${tr(label, fallback)}</a>`,
      )
      .join(""),
  );
  for (const panel of document.querySelectorAll("[data-task-panel]"))
    panel.hidden = panel.dataset.taskPanel !== selected;
  if (r.focus && selected === "steps") {
    const step = [...document.querySelectorAll("[data-step]")].find(
      (el) => el.dataset.step === r.focus,
    );
    if (step) {
      step.open = true;
      step.scrollIntoView?.({ block: "center" });
    }
  }
}
function renderTaskLog() {
  const all = taskReport?.events || [];
  const query = $("logSearch").value.trim().toLocaleLowerCase();
  const kind = $("logKind").value;
  const events = all.filter(
    (e) =>
      (!kind || String(e.type || e.event_type).includes(kind)) &&
      (!query || JSON.stringify(e).toLocaleLowerCase().includes(query)),
  );
  $("taskLogSub").textContent =
    `${num(events.length)} / ${num(all.length)} ${tr("task.events", "events")} · ${num(taskReport?.event_total ?? all.length)} ${tr("reg.total", "Total")}`;
  const offset = taskReport?.event_offset || 0;
  const limit = taskReport?.event_limit || 200;
  const total = taskReport?.event_total ?? all.length;
  const pageLink = (next, label) =>
    `<a class="btn sm" href="#/give/${encodeURIComponent(state.taskId)}/log/page/${next}">${label}</a>`;
  setHTML(
    "logPages",
    (offset > 0
      ? pageLink(
          Math.max(0, offset - limit),
          tr("log.previous", "Previous events"),
        )
      : "") +
      (offset + all.length < total
        ? pageLink(offset + limit, tr("log.next", "Next events"))
        : ""),
  );
  $("taskLogEmpty").hidden = events.length > 0;
  setHTML(
    "taskLog",
    events
      .map(
        (e) => `<details class="log-entry" data-event="${esc(e.id)}">
    <summary><span class="feed-dot ${feedKind(e)}"></span><time datetime="${esc(e.occurred_at || "")}">${esc(e.occurred_at ? new Date(e.occurred_at).toLocaleString(state.lang === "vi" ? "vi-VN" : "en-GB") : "—")}</time>
    <div class="grow">${eventLine(e)}<span class="event-type">${esc(e.type || e.event_type || "—")}</span></div></summary>
    <div class="event-body">${e.view?.task_id && e.view.task_id !== state.taskId ? `<a href="#/give/${encodeURIComponent(e.view.task_id)}/log">${esc(e.view.title || e.view.task_id)}</a>` : ""}
    ${valueHTML({ id: e.id, actor: e.actor_id, subject: e.subject, data: e.data })}</div></details>`,
      )
      .join(""),
  );
}
async function renderApproval(id) {
  if (!id) return;
  const approval = await apiGet(`/approvals/${encodeURIComponent(id)}`);
  if (parseHash().name !== "approval" || parseHash().arg !== id) return;
  const live =
    approval.status === "pending" &&
    (!approval.expires_at || Date.parse(approval.expires_at) > Date.now());
  openSheet(
    `${tr("need.approvals", "Approval")} · ${approval.action_type}`,
    `<div class="task-meta"><code>${esc(approval.id)}</code><span class="pill">${esc(approval.status)}</span></div>
    ${reasonBlock(approval.reason || "")}
    <h3>${tr("appr.draft", "The exact draft requested for review")}</h3>${valueHTML(approval.action_payload || {})}
    ${approval.task_id ? `<a class="btn" href="#/give/${encodeURIComponent(approval.task_id)}/decisions">${tr("need.open", "Open task")}</a>` : ""}
    ${live ? `<div class="btn-row" style="margin-top:16px"><button class="btn primary" data-do="approve" data-id="${esc(id)}">${tr("appr.approve", "Approve")}</button><button class="btn danger" data-do="reject" data-id="${esc(id)}">${tr("appr.reject", "Reject")}</button></div>` : ""}`,
  );
}
$("newTaskBtn").onclick = () => {
  $("newTaskForm").open = true;
  $("goal").focus?.();
};
$("taskSearch").oninput = () => renderGive(null, false);
$("logSearch").oninput = renderTaskLog;
$("logKind").onchange = renderTaskLog;
$("skipContent").onclick = (e) => {
  e.preventDefault();
  $("mainContent").focus?.();
};

let taskRenderRequest = 0;
let taskReport = null;
async function renderOneTask(id) {
  const request = ++taskRenderRequest;
  if (state.taskId !== id) {
    taskReport = null;
    $("taskTitle").textContent = tr("task.loading", "Loading task…");
    for (const key of [
      "taskSub",
      "taskIdentity",
      "taskBanner",
      "taskStats",
      "taskNote",
      "taskParentLinks",
      "taskTabs",
      "runThisState",
    ])
      setHTML(key, "");
    for (const panel of document.querySelectorAll("[data-task-panel]"))
      panel.hidden = true;
    $("retryBtn").hidden = true;
    $("runThisTask").hidden = true;
    $("cancelTask").hidden = true;
    $("runThisHint").hidden = true;
  }
  let d = null;
  const eventOffset = parseHash().eventOffset || 0;
  try {
    d = await apiGet(`/tasks/${encodeURIComponent(id)}/report`, {
      event_offset: eventOffset,
    });
  } catch (err) {
    if (request === taskRenderRequest && parseHash().arg === id) showError(err);
    return;
  }
  if (
    request !== taskRenderRequest ||
    parseHash().arg !== id ||
    !["give", "task", "console"].includes(parseHash().name)
  )
    return;
  if (state.taskId !== id) {
    $("logSearch").value = "";
    $("logKind").value = "";
  }
  taskReport = d;
  const t = d.task || {};
  $("taskIdentity").textContent = t.id || "";
  $("taskRequest").textContent = t.goal || "";
  setHTML(
    "taskParentLinks",
    [
      t.parent_task_id
        ? `<a href="#/give/${encodeURIComponent(t.parent_task_id)}/assignment">${tr("task.parent", "Parent task")}</a>`
        : "",
      t.root_task_id && t.root_task_id !== t.id
        ? `<a href="#/give/${encodeURIComponent(t.root_task_id)}">${tr("task.root", "Whole goal")}</a>`
        : "",
    ]
      .filter(Boolean)
      .join(" · "),
  );
  $("runThisTask").hidden = !["created", "assigned"].includes(t.status);
  $("cancelTask").hidden = !(d.available_actions || []).includes("cancel");
  if (t.workflow_id && t.workflow_id !== t.id) $("cancelTask").hidden = true;
  $("runThisHint").hidden = $("runThisTask").hidden;
  setHTML(
    "runThisHint",
    esc(tr("task.runhint", "Uses model quota; work runs in the background.")),
  );
  if (t.workflow_id) {
    $("runThisTask").hidden = true;
    $("runThisHint").hidden = false;
    setHTML(
      "runThisHint",
      `<a href="#/processes/workflows/${esc(t.workflow_id)}">${esc(state.lang === "vi" ? "Mở workflow để chạy đúng thứ tự và duyệt từng cổng" : "Open the workflow to run its ordered stages and reviews")}</a>`,
    );
  }
  $("runThisState").textContent = statusName(t.status);
  taskNames.set(t.id, t.title);
  state.taskId = t.id || null;

  /* The retry button appears **only** where it applies: a task that failed, was cancelled or
     was blocked. A task that has not failed has nothing to repeat — it needs running or
     deciding, and offering "run it again" there would invite a duplicate of work already in
     flight. Hidden by default in the markup too, so it cannot flash before the report lands. */
  const retryable = t.workflow_id
    ? t.workflow_id === t.id && t.status === "failed"
    : ["failed", "canceled", "cancelled", "blocked", "expired"].includes(
        t.status,
      );
  const rb = $("retryBtn");
  rb.hidden = !retryable;
  if (retryable) {
    rb.title = t.last_error
      ? `Tạo một task MỚI cùng việc này. Lý do đã fail: ${String(t.last_error).slice(0, 160)}`
      : "Tạo một task MỚI cùng việc này.";
  }

  const s = d.summary || {};
  setHTML(
    "taskStats",
    statTile(
      tr("task.outcome", "Outcome"),
      statusName(t.status),
      t.task_type || "",
      t.status === "completed" ? "good" : t.status === "failed" ? "alarm" : "",
    ) +
      statTile(
        tr("task.handed", "Handed to"),
        num(s.delegated_to ?? 0),
        tr("task.handed_sub", "departments or agents"),
      ) +
      statTile(
        tr("task.ran", "Ran"),
        num(s.executed ?? 0),
        t_fmt("task.ran_sub", "{n} failed", { n: s.failed ?? 0 }),
        s.failed ? "alarm" : "good",
      ) +
      statTile(
        tr("task.inflight_t", "In flight"),
        num(s.in_flight ?? 0),
        t_fmt("task.inflight_sub", "of {n} attempt(s)", { n: s.attempts ?? 0 }),
        (s.in_flight ?? 0) ? "warn" : "good",
      ) +
      statTile(
        tr("task.decisions_t", "Decisions"),
        num(s.approvals_total ?? 0),
        t_fmt("task.decisions_sub", "{n} waiting on a person", {
          n: s.approvals_pending ?? 0,
        }),
        s.approvals_pending ? "warn" : "good",
      ),
  );

  $("taskTitle").textContent = t.title || "Task";
  $("taskSub").textContent = [t.owner_name, statusName(t.status), t.task_type]
    .filter(Boolean)
    .join(" · ");

  // **Banner first, steps second: the order the questions are asked in.**
  // What happened, then what each hand did. Both are rendered from the same report
  // the rest of the page reads, so they cannot disagree with the tiles below.
  const _s = d.summary || {};
  renderBanner(t, _s);
  renderAnswer(t);
  renderSteps(d);

  /* The tree, parents before children, so the first row is the thing that was asked for
     and each delegation is visibly underneath it. A tree that lists children first makes
     a reader reconstruct the shape themselves. */
  setHTML(
    "taskTree",
    (d.tree || [])
      .map(
        (n) => `
    <a class="row" href="#/give/${encodeURIComponent(n.id)}" style="margin-left:${n.parent_task_id ? 18 : 0}px">
      <div class="grow">
        <div class="t">${n.parent_task_id ? "↳ " : ""}${esc(n.title)}</div>
        <div class="s">${esc(n.status)}${n.owner_name ? " · " + esc(n.owner_name) : ""}</div>
      </div>
    </a>`,
      )
      .join("") +
      (d.delegations || [])
        .map(
          (g) => `
    <div class="row static">
      <div class="grow">
        <div class="t">d${g.depth} ${esc(g.from_agent || "?")} → ${esc(g.to_agent || "?")}</div>
        <div class="s">${esc(g.objective || "")}</div>
        ${g.denial_reason ? `<div class="s">refused: ${esc(String(g.denial_reason).slice(0, 140))}</div>` : ""}
      </div>
      <div class="r"><span class="pill">${esc(g.status)}</span></div>
    </div>`,
        )
        .join(""),
  );

  /* The graph first, because it is the answer to "who did what" and a
     list of rows answers it only after you have read every one. */
  const tree = d.tree || [];
  $("taskGraphSub").textContent = t_fmt(
    "task.graph_sub",
    "{t} task(s), {d} delegation(s)",
    { t: tree.length, d: (d.delegations || []).length },
  );
  $("taskGraphEmpty").hidden = tree.length > 0;
  $("taskGraph").hidden = tree.length === 0;
  if (tree.length)
    setHTML("taskGraph", delegationGraph(t.id, tree, d.delegations || []));

  const appr = d.approvals || [];
  $("taskApprovalsSub").textContent = t_fmt(
    "task.appr_sub",
    "{n} on this task",
    { n: appr.length },
  );
  $("taskApprovalsEmpty").hidden = appr.length > 0;
  $("taskApprovals").hidden = appr.length === 0;
  setHTML(
    "taskApprovals",
    appr
      .map(
        (ap) => `
    <div class="row${ap.status !== "pending" ? " static" : ""}">
      <div class="grow">
        <div class="t"><a href="#/approval/${encodeURIComponent(ap.id)}">${esc(ap.action_type)}</a> <span class="pill">${esc(ap.risk_level || "")}</span></div>
        <div class="s">${esc(ap.reason || "")}</div>
        ${
          ap.decided_at
            ? `<div class="s">${t_fmt("task.decided", "{d} by {who} · {when}", {
                d: esc(ap.decision || ap.status),
                who: esc(ap.decided_by || tr("task.someone", "someone")),
                when: esc(ago(ap.decided_at)),
              })}</div>`
            : ""
        }
      </div>
      <div class="btn-row">
        ${
          ap.status === "pending"
            ? `<button class="btn sm" data-appr="${esc(ap.id)}" data-do="approve">${tr("appr.approve", "Approve")}</button>
             <button class="btn ghost sm" data-appr="${esc(ap.id)}" data-do="reject">${tr("task.refuse", "Refuse")}</button>`
            : `<span class="t-${ap.decision === "approved" ? "ok" : "bad"}">${esc(ap.decision || ap.status)}</span>`
        }
      </div>
    </div>`,
      )
      .join(""),
  );

  // **The same sentences as the live feed, not raw event types.**
  //
  // This rendered `esc(e.event_type)` plus the raw `actor_id` — `task.delegation_accepted
  // · agt_01m3…` — a type name and an opaque id, for the same log the feed had already
  // read as "Procurement Agent asked the Executive Agent to do the work". One log, two
  // readings, and the refresh disagreed with the live view; the complaint was "I cannot
  // see anything the agents are doing".
  //
  // `eventLine` reads `ev.view`, which the report now carries because it runs the same
  // `enrich` projection as the stream. An event the report predates (no `view`) falls
  // through to its type name, which is honest and still readable.
  renderTaskLog();
  renderTaskSections(t.id);

  /* The report, in words, at the end -- because "finish then report to me" is the last
     step of the flow and it should arrive as a sentence, not as a table to read. */
  const models = (s.models || []).filter(Boolean);
  $("taskNote").innerHTML = models.length
    ? `${tr("step.model", "Model:")} <code>${esc(models.join(", "))}</code>`
    : tr(
        "task.no_model_recorded",
        "No model name is recorded in this task report.",
      );

  // **Watch an unfinished task, however it was started.** Only the detail-page Run button
  // started the watcher, so pressing Run on the *form* landed on a static page: the task
  // ran in the background and nothing on screen moved. A person who pressed Run to watch
  // something happen watched a page that never changed, which is the same complaint as
  // "I cannot see anything the agents are doing" one screen later.
  //
  // Terminal statuses end the watch by omission: a task that cannot change needs no
  // polling, and polling it would be requests spent proving nothing.
  if (
    ![
      "completed",
      "failed",
      "canceled",
      "cancelled",
      "expired",
      "blocked",
      "waiting_for_approval",
    ].includes(t.status)
  )
    watchRun(t.id);
  else stopWatch();
  renderCrumbs(parseHash());
}

$("cancelTask").onclick = () => {
  const id = parseHash().arg;
  if (!id) return;
  openSheet(
    tr("task.cancel", "Cancel task"),
    `<p>${tr("task.cancel_explain", "Cancel this exact task and signal its workflow to stop. Its history remains available.")}</p><code>${esc(id)}</code><div class="btn-row"><button class="btn" data-confirm-cancel>${tr("task.cancel_confirm", "Confirm cancellation")}</button></div>`,
  );
  const button = $("sheetBody").querySelector("[data-confirm-cancel]");
  button.onclick = async () => {
    button.disabled = true;
    try {
      await apiPost(`/tasks/${encodeURIComponent(id)}/cancel`, {});
      $("sheet").close();
      if (parseHash().arg === id) await renderOneTask(id);
    } catch (err) {
      toast(err.message, true);
      button.disabled = false;
    }
  };
};

$("runThisTask").onclick = async () => {
  const id = parseHash().arg;
  if (!id) {
    toast(tr("run.openfirst", "Open a task first."), true);
    return;
  }
  const btn = $("runThisTask");
  btn.disabled = true;
  btn.textContent = tr("run.starting", "Starting…");
  $("runThisState").textContent = "";
  try {
    const h = await apiPost(`/tasks/${encodeURIComponent(id)}/run`, {});
    if (!h.started) {
      // Refusals are the interesting case and get the whole line, not a generic failure.
      $("runThisState").innerHTML =
        `<span class="t-bad">${tr("run.notstarted", "Not started.")}</span> ${esc(h.reason)}`;
      toast(
        t_fmt("run.notstarted_r", "Not started: {r}", { r: h.reason }),
        true,
      );
      return;
    }
    if (parseHash().arg !== id) return;
    $("runThisState").textContent = tr(
      "run.started_now",
      "Started in the background. Duration and quota depend on the provider; this page will keep watching.",
    );
    toast(tr("run.started_short", "Task started in the background."));
    watchRun(id);
  } catch (err) {
    $("runThisState").innerHTML =
      `<span class="t-bad">${tr("run.couldnot", "Could not start.")}</span> ${esc(err.message)}`;
    toast(
      t_fmt("run.couldnot_r", "Could not start: {e}", { e: err.message }),
      true,
    );
  } finally {
    btn.disabled = false;
    btn.textContent = tr("task.runnow", "Run this task now");
  }
};

/* Watch, do not wait. A run depends on the provider, so the page polls the
   report every few seconds and re-renders -- the alternative is a
   spinner for ten minutes, which reads as a broken product. */
let watchTimer = null;
let watchedTask = null;
function stopWatch() {
  if (watchTimer) clearInterval(watchTimer);
  watchTimer = null;
  watchedTask = null;
}
function watchRun(id) {
  if (watchTimer && watchedTask === id) return;
  stopWatch();
  watchedTask = id;
  let pending = false;
  watchTimer = setInterval(async () => {
    if (
      !["give", "task"].includes(parseHash().name) ||
      parseHash().arg !== id
    ) {
      stopWatch();
      return;
    }
    if (pending) return;
    pending = true;
    try {
      await renderOneTask(id);
    } catch (err) {
      showError(err);
    } finally {
      pending = false;
    }
  }, 4000);
}

// **Bound to `#giveFilter`, the register's own segment.**
//
// It was bound to `$("workFilter")`, and there were **two** elements with that id --
// this screen's four-button register filter and the `Needs you` screen's two-button
// approvals filter. `$()` returns the first in the document, so this handler was
// attached to the approvals segment and to nothing else: the four buttons a person
// actually clicks did nothing at all, and neither did they re-render. Measured on the
// page: `workFilter ×2, workList ×2, workEmpty ×3, workSub ×2` -- and both render paths
// wrote to the *first* `#workList`, so the register panel in `Give work` was never
// written to and read as permanently empty while its own tile counted 176 tasks.
//
// The two panels are genuinely different lists -- pending approvals with buttons, and
// the task register with filters -- so they get different ids rather than one of them
// being deleted.
// **This screen's own filter, which had no handler at all.** The markup declared
// `data-f="pending"` / `data-f="all"` and nothing listened, so a person could click
// "All" a hundred times and see the pending list forever.
$("issueFilter").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-i]");
  if (!b) return;
  issueFilter = b.dataset.i || "";
  for (const x of $("issueFilter").children) x.classList.toggle("on", x === b);
  await renderWork();
});

$("workFilter").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-f]");
  if (!b) return;
  state.approvalFilter = b.dataset.f || "pending";
  for (const x of $("workFilter").children) x.classList.toggle("on", x === b);
  await renderWork();
});

$("giveFilter").addEventListener("click", async (e) => {
  const b = e.target.closest("button[data-w]");
  if (!b) return;
  giveFilter = b.dataset.w || "";
  for (const x of $("giveFilter").children) x.classList.toggle("on", x === b);
  await renderGive();
});

document.addEventListener("click", async (e) => {
  const row = e.target.closest("[data-task]");
  if (
    row &&
    row.dataset.task &&
    row.tagName !== "DETAILS" &&
    !e.target.closest("a,button,summary")
  ) {
    go(`#/give/${encodeURIComponent(row.dataset.task)}`);
    return;
  }
  const btn = e.target.closest("button[data-appr]");
  if (!btn) return;
  const doing = btn.dataset.do;
  const note =
    doing === "approve"
      ? null
      : window.prompt(
          tr(
            "appr.whyrefused",
            "Why is this refused?\n\nThe reason is kept with the decision, so the next reader can " +
              "see what was considered. An approval with no note is a tick in a box.",
          ),
        );
  if (doing !== "approve" && (note === null || !note.trim())) return;
  btn.disabled = true;
  try {
    await apiPost(
      `/approvals/${encodeURIComponent(btn.dataset.appr)}/${doing}`,
      doing === "approve" ? {} : { note: note.trim() },
    );
    toast(
      doing === "approve"
        ? tr("appr.decided", "Decided, and written to the log.")
        : tr("appr.refused_note", "Refused, with your reason."),
    );
    await renderOneTask(parseHash().arg);
    // The queue's count changes the moment a decision is made, so the badge must follow.
    try {
      const q = await apiGet("/ceo/work", { limit: 1 });
      const b = $("navWorkCount");
      if (b) {
        b.textContent = q.needs_you ? String(q.needs_you) : "";
        b.hidden = !q.needs_you;
      }
    } catch {
      /* the badge is not worth a second error */
    }
  } catch (err) {
    btn.disabled = false;
    toast(
      t_fmt("appr.couldnot", "Could not record that: {e}", { e: err.message }),
      true,
    );
  }
});

/* ====================== documents ======================
   Two views, one route. `#/documents` lists; `#/documents/{id}` is the detail, and the
   breadcrumb plus the Back button are how a person leaves it — the complaint that this
   section answers was "no way out of a drill-down", and a detail view with no crumb is
   exactly that. */
/* ==================================================================
   8. The delegation tree.
   ------------------------------------------------------------------
   It used to live in a `console` view whose main content was a log, so the
   only place the tree could be seen was a side panel of something else.
   It is beside the form that starts the work now: a person hands over a task
   and watches what it became.
   ================================================================== */ /* ==================================================================
   8. The console. Unchanged in behaviour, kept because it works and
   because it is the only place the delegation tree is visible.
   ================================================================== */
/* ---------------- what is happening, in words ----------------
   The complaint this answers is "I pressed Run and cannot see anything the agents are
   doing". The evidence was already arriving: the SSE frame carries `view.from_agent`,
   `view.to_agent` and `view.title` — **names, resolved server-side**, precisely so a
   reader never renders two opaque ids. It was accumulated into `state.events` and then
   never drawn.

   So this is not a new data source. It is the existing feed, written in sentences, with
   the department names in it. A line like

       Procurement Agent asked the Executive Agent for "run the tender"  ·  40s ago

   says who is doing what; `evt_… agt_01m3…` says nothing a person can act on, which is
   what the raw payload was.

   The types are grouped rather than enumerated exhaustively: an unrecognised event falls
   through to its own name, which is honest and still readable, instead of being hidden. */
const EVENT_LINES = [
  [
    /fail|error/i,
    (v) =>
      t_fmt("feed.failed", "{n} could not finish it", {
        n: esc(
          v.from_agent || v.to_agent || tr("feed.platform", "the platform"),
        ),
      }),
  ],
  [/reject/i, () => tr("feed.rejected", "the request was refused")],
  [/expired/i, () => tr("feed.expired", "the request expired")],
  [/cancell?ed/i, () => tr("feed.cancelled", "the task was cancelled")],
  [/created/i, () => tr("feed.created", "the task was created")],
  [/started/i, () => tr("feed.started", "work started")],
  [/approved/i, () => tr("feed.approved", "the request was approved")],
  [
    /refus/i,
    (v) =>
      v.from_agent
        ? t_fmt("feed.refused", "{a} was refused{r}", {
            a: esc(v.from_agent),
            r: v.reason
              ? t_fmt("feed.refused_r", ": {r}", {
                  r: esc(String(v.reason).slice(0, 120)),
                })
              : "",
          })
        : tr("feed.refused_bare", "a delegation was refused"),
  ],
  [
    /delegat/i,
    (v) =>
      v.from_agent && v.to_agent
        ? t_fmt("feed.delegated", "{a} asked {b} to do the work", {
            a: esc(v.from_agent),
            b: esc(v.to_agent),
          })
        : tr("feed.handed", "work was handed to another agent"),
  ],
  [
    /approv/i,
    (v) =>
      t_fmt("feed.decision", "a decision is waiting on a person{a}", {
        a: v.to_agent
          ? t_fmt("feed.decision_at", " at {n}", { n: esc(v.to_agent) })
          : "",
      }),
  ],
  [
    /complet/i,
    (v) =>
      v.to_agent || v.from_agent
        ? t_fmt("feed.done", "{n} finished and reported", {
            n: esc(v.to_agent || v.from_agent),
          })
        : tr("feed.done_bare", "the task finished"),
  ],
  [
    /execut/i,
    (v) =>
      v.to_agent || v.from_agent
        ? t_fmt("feed.ran", "{n} ran the task", {
            n: esc(v.to_agent || v.from_agent),
          })
        : tr("feed.ran_bare", "the task ran"),
  ],
  [
    /review|escalat/i,
    () =>
      tr(
        "feed.review",
        "a reviewer looked at the result and asked for another pass",
      ),
  ],
  [
    /lesson|procedure|skill/i,
    () => tr("feed.learned", "the agent wrote down what it learned"),
  ],
];

/** The dot color for a feed row: failure red, finish green, refusal amber. */
