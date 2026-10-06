function feedKind(ev) {
  const type = String(ev.type || ev.event_type || "");
  if (/fail|error/i.test(type)) return "bad";
  if (/complet/i.test(type)) return "ok";
  if (/refus|reject|escalat/i.test(type)) return "warn";
  return "";
}

function eventLine(ev) {
  const v = ev.view || {};
  const at = ev.occurred_at ? ago(ev.occurred_at) : "";
  const what = v.title ? esc(String(v.title).slice(0, 90)) : "";
  for (const [match, say] of EVENT_LINES) {
    if (match.test(ev.type || ev.event_type || "")) {
      return `${say(v)}${what ? ` — <b>${what}</b>` : ""}`;
    }
  }
  return `${esc(ev.type || ev.event_type || tr("feed.unknown", "Unknown event"))}${what ? ` — <b>${what}</b>` : ""}${
    at ? ` <span class="s">${esc(at)}</span>` : ""
  }`;
}

function renderActivity() {
  const feed = $("activity");
  if (!feed) return;
  const recent = state.events.slice(-40).reverse();
  $("activityEmpty").hidden = recent.length > 0;
  feed.innerHTML = recent
    .map((ev) => {
      const at = ev.occurred_at ? ago(ev.occurred_at) : "";
      const v = ev.view || {};
      return `<a class="row feed-row" href="#/give/${encodeURIComponent(v.task_id || ev.subject || "")}/log">
      <span class="feed-dot ${feedKind(ev)}"></span>
      <div class="grow"><div class="s" style="font-size:12px">${eventLine(ev)}</div></div>
      <div class="r"><span class="s">${esc(at)}</span>
        ${v.task_id ? `<span class="pill">${esc(String(v.task_id).slice(-6))}</span>` : ""}</div>
    </a>`;
    })
    .join("");
}

function renderFlow() {
  const roots = [...state.tasks.values()].filter((n) => !n.parent_task_id);
  $("flowCount").textContent = roots.length
    ? t_fmt("flow.count", "{n} tasks", { n: state.tasks.size })
    : "";
  $("flowEmpty").hidden = roots.length > 0;
  // **`replaceChildren`, not `innerHTML = ....join("")`.**
  //
  // `nodeEl` builds and returns an `HTMLElement`, and joining an array of elements
  // calls `String()` on each one. What the Workflow panel actually rendered, on the
  // page, was a wall of:
  //
  //     [object HTMLLIElement][object HTMLLIElement][object HTMLLIElement]...
  //
  // — every root task and every child, spelled out as the *type* of the object rather
  // than as markup. So the one panel whose entire job is to show how work was handed
  // out showed a JavaScript type name, and the delegation tree was invisible. Nothing
  // raised: `String()` on an element is well-defined, it just produces this.
  //
  // The `.map((n, i) => ...)` also computed an `isLast` flag that nothing read.
  // Children are drawn as nested `<ul>`s and the connector CSS uses `:last-child`, so
  // the flag had no effect even when the markup was right.
  $("tree").replaceChildren(...roots.map((n) => nodeEl(n)));
}
function nodeEl(n) {
  const el = document.createElement("li");
  const div = document.createElement("a");
  div.setAttribute("href", `#/give/${encodeURIComponent(n.id)}`);
  const st = n.lifecycle_status || "pending";
  div.className = "node st-" + st;
  const pill =
    st === "completed"
      ? "ok"
      : st === "failed"
        ? "bad"
        : st === "rejected"
          ? "wait"
          : st === "delegated"
            ? "delegated"
            : "";
  const kids = (n.children || []).length;
  div.innerHTML =
    `<div class="t">${esc(n.goal || n.title || tr("flow.untitled", "untitled work"))}</div>` +
    `<div class="m"><span class="pill ${pill}">${esc(st)}</span>` +
    (n.when ? `<span>${esc(ago(n.when))}</span>` : "") +
    (kids
      ? `<span>${esc(tr_fmt("flow.hands", "{n} hand(s) below", { n: kids }))}</span>`
      : "") +
    `</div>`;
  el.appendChild(div);
  const kidObjs = (n.children || [])
    .map((c) => state.tasks.get(c))
    .filter(Boolean);
  if (kidObjs.length) {
    const ul = document.createElement("ul");
    kidObjs.forEach((k) => ul.appendChild(nodeEl(k)));
    el.appendChild(ul);
  }
  return el;
}

/* ---------------- streaming ---------------- */
function setConn(name, key, fallback) {
  state.connection = [name, key, fallback];
  $("dot").className = "dot " + name;
  $("connText").textContent = tr(key, fallback);
}

/* SSE over `fetch` rather than `EventSource`: `EventSource` cannot set an
   `Authorization` header, and the two ways round that are a token in the query
   string or a cookie. Both are worse. `fetch` sends the header, `ReadableStream`
   gives the frames, and the token never leaves the header. */
async function stream() {
  for (let attempt = 0; ; attempt++) {
    try {
      setConn("wait", "conn.connecting", "connecting");
      const r = await fetch(API + "/stream", {
        headers: authHeaders({ Accept: "text/event-stream" }),
        signal: (state.es || new AbortController()).signal,
      });
      if (!r.ok || !r.body) throw new Error(String(r.status));
      setConn("ok", "conn.live", "live");
      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      let buf = "";
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const frame = buf.slice(0, i);
          buf = buf.slice(i + 2);
          for (const line of frame.split("\n")) {
            if (line.startsWith("data:")) handleFrame(line.slice(5).trim());
          }
        }
      }
      throw new Error("stream closed");
    } catch (err) {
      if (err && err.name === "AbortError") return;
      setConn("bad", "conn.retry", "reconnecting");
      await new Promise((r) =>
        setTimeout(r, Math.min(15000, 1000 * 2 ** Math.min(attempt, 4))),
      );
    }
  }
}

/* The event shape, measured against a live stream rather than remembered:

     { id, type, subject, source, actor_id, data, occurred_at, view }

   `data` carries the **ids** — `parent_task_id`, `source_agent_id`,
   `target_agent_id`, `depth`, `delegation_id`. `view` carries the **names** —
   `from_agent`, `to_agent`, `title`, `parent_title`, `task_id` — because the
   projection resolves the ids so a reader never renders two opaque strings.

   The previous version read `ev.detail` and `ev.at`, neither of which has ever
   existed on this payload, so the console rendered an empty feed and an empty tree
   over 189 real events. The tests that guarded it asserted the *names* were present
   in the file, which they were — in a comment.
*/
function handleFrame(payload) {
  let msg;
  try {
    msg = JSON.parse(payload);
  } catch {
    return;
  }
  for (const ev of msg.events || [msg]) {
    if (!ev || !ev.id || state.backfill.has(ev.id)) continue;
    state.backfill.add(ev.id);
    state.events.push(ev);
    if (state.events.length > 600)
      state.events.splice(0, state.events.length - 600);
    const d = ev.data || {};
    const v = ev.view || {};
    for (const name of [v.from_agent, v.to_agent]) {
      if (name) state.agents.set(name, (state.agents.get(name) || 0) + 1);
    }
    const childId = v.task_id || d.child_task_id;
    const parentId = d.parent_task_id;
    if (childId && parentId) {
      const child = state.tasks.get(childId) || { id: childId, children: [] };
      child.parent_task_id = parentId;
      child.goal = v.title || child.goal || "";
      child.when = ev.occurred_at || child.when || "";
      child.lifecycle_status = ev.type.includes("completed")
        ? "completed"
        : ev.type.includes("rejected")
          ? "rejected"
          : ev.type.includes("failed")
            ? "failed"
            : "delegated";
      state.tasks.set(childId, child);
      const parent = state.tasks.get(parentId) || {
        id: parentId,
        children: [],
        goal: v.parent_title || "",
        lifecycle_status: "running",
      };
      parent.when = parent.when || ev.occurred_at || "";
      if (!parent.children.includes(childId)) parent.children.push(childId);
      state.tasks.set(parentId, parent);
    } else if (v.task_id) {
      const node = state.tasks.get(v.task_id) || {
        id: v.task_id,
        children: [],
      };
      node.goal = v.title || node.goal || "";
      node.when = ev.occurred_at || node.when || "";
      node.lifecycle_status = ev.type.endsWith("completed")
        ? "completed"
        : ev.type.endsWith("failed")
          ? "failed"
          : "running";
      state.tasks.set(v.task_id, node);
    }
  }
  // The tree **and** the feed, on every frame. Drawing only the tree is why the page
  // showed a delegation tree and nothing else: the events describing *how* work moved
  // were arriving on the same socket and being thrown away.
  //
  // **And the notifications, on the same frames.** A stream that only draws is a stream a
  // person has to be watching: an approval that arrives while they are on another screen
  // waits silently until they navigate. `notifFor` decides per event, `notify` pops up
  // and files it in the bell, and the badges refresh on a debounce — so the header always
  // knows, even when the screen does not.
  // Fresh events only: the loop above already skipped anything in `state.backfill`,
  // and `notify` dedupes on `"notified:" + id` regardless, so a reconnect can never
  // pop up twice for one event.
  let notable = false;
  for (const ev of msg.events || [msg]) {
    if (!ev || !ev.id || !state.backfill.has(ev.id)) continue;
    const n = notifFor(ev);
    if (n) {
      n.id = ev.id;
      notify(n);
      notable = true;
    }
  }
  if (notable) scheduleBadgeRefresh();
  if (!state.paused) {
    renderFlow();
    renderActivity();
  }
}

/* ---------------- start a task ---------------- */
$("runBtn").onclick = async () => {
  const goal = $("goal").value.trim();
  if (!goal) {
    toast(tr("give.saywhat", "Say what the company should work on."), true);
    return;
  }
  const btn = $("runBtn");
  btn.disabled = true;
  btn.textContent = tr("give.running", "Running…");
  try {
    // The chosen agent is the task's owner. It was not sent, so the selector on
    // screen did nothing: the task was created with nobody to do it.
    const chosen = $("scenario").value;
    const scenario = (state.scenarios || []).find((sc) => sc.key === chosen);
    const made = await apiPost("/tasks", {
      title: (scenario ? scenario.title : goal).slice(0, 60),
      goal,
      task_type: $("taskType").value,
      start_workflow: false,
      owner_agent_id: $("agent").value || null,
      // Carried so the office can route the work to the department that owns it,
      // and so the department is held to what was asked for rather than to
      // "something". Absent for free text, which declares nothing.
      ...(scenario
        ? {
            input: {
              owning_department: scenario.agent_name,
              owning_office: scenario.office,
            },
            expected_output_schema: scenario.expected_output_schema,
          }
        : {}),
    });
    const taskId = made.task_id || made.id;

    // **Then actually run it. This is the button's whole job and it was not doing it.**
    //
    // The handler above created the task with `start_workflow: false`, printed "Queued.",
    // and navigated away. On `make page` there is no Temporal and no worker, so nothing
    // ever claimed the row: the queue only ever grew, and every task sat `assigned` with
    // nobody holding it. Measured on the development tenant — 106 `assigned`, 0 `running`,
    // "With an agent: 0" on the register, and a person pressing Run with nothing
    // happening. The button said "Runs on the free model" underneath, which was a promise.
    //
    // `POST /tasks/{id}/run` is the path that exists for exactly this case: it drives the
    // same `TaskExecutionService.execute_task` the Temporal activity drives, on a
    // background task, and returns a handle immediately. A synchronous run would time
    // out — the docstring records 644 seconds and 67k tokens for a three-way delegation.
    //
    // The refusal is reported rather than swallowed: a run already in flight is a real
    // refusal, and 67k tokens is a real bill.
    let started;
    try {
      started = await apiPost(`/tasks/${encodeURIComponent(taskId)}/run`, {});
    } catch (err) {
      toast(
        t_fmt(
          "give.exists_not_running",
          "The task exists but nothing is running it: {e}",
          { e: err.message },
        ),
        true,
      );
      if (taskId) go(`#/give/${encodeURIComponent(taskId)}`);
      return;
    }
    // The handle carries `started`, `already_running` and a cost estimate, and all three
    // change what a person should be told. "Running" on a refusal is the one that lies.
    if (started.already_running) {
      toast(
        tr("give.already", "Already running — not starting a second one."),
        true,
      );
    } else if (started.started === false) {
      toast(
        t_fmt(
          "give.queued_not_running",
          "Queued, but nothing is running it: {e}",
          { e: started.reason || tr("retry.unknown", "unknown") },
        ),
        true,
      );
    } else if (started.estimate_tokens) {
      toast(
        t_fmt(
          "give.running_here",
          "Running here. About {s}s and {tok} tokens — this panel fills in as they report.",
          {
            s: num(started.estimate_seconds),
            tok: num(started.estimate_tokens),
          },
        ),
      );
    } else {
      toast(
        tr(
          "give.running_panel",
          "Running. This panel fills in as the agents report.",
        ),
      );
    }
    // **Straight to the task**, not to a list. The person pressed Run to watch something
    // happen; a list of rows is not the thing they asked to see.
    if (taskId) go(`#/give/${encodeURIComponent(taskId)}`);
    else go("#/give");
  } catch (err) {
    toast(
      t_fmt("run.couldnot_r", "Could not start: {e}", { e: err.message }),
      true,
    );
  } finally {
    btn.disabled = false;
    btn.textContent = tr("give.run", "Run");
  }
};
$("tokenBtn").onclick = () => {
  if (askToken(token()) !== null) {
    forgetToken();
    askToken("");
  }
};
/* The language switch. English is the verified default, so the button offers
   Vietnamese; after switching it offers the way back. Applied immediately to
   the current screen by re-rendering the route — every renderer reads `t()`. */
($("langBtn") || {}).onclick = () => {
  langSet(state.lang === "vi" ? "en" : "vi");
  route();
};
if (state.lang === "vi") {
  const _lb = $("langBtn");
  if (_lb) _lb.textContent = "EN";
  try {
    if (document.documentElement) document.documentElement.lang = "vi";
  } catch {
    /* the harness document has no element to label */
  }
  paintStatic();
}

/* ---------------- the work catalogue ---------------- */
// Filled from the server rather than written into the page, so the work on offer
// and the work the code knows about cannot drift apart.
async function loadScenarios() {
  const sel = $("scenario");
  try {
    const data = await apiGet("/scenarios");
    state.scenarios = data.scenarios || [];
    sel.innerHTML =
      `<option value="">${tr("give.something", "Something else (type it below)")}</option>` +
      state.scenarios
        .map((sc) => `<option value="${sc.key}">${sc.title}</option>`)
        .join("");
  } catch (err) {
    // A catalogue that will not load must not block typing your own work.
    sel.innerHTML = `<option value="">${tr("give.something", "Something else (type it below)")}</option>`;
  }
}
$("scenario").onchange = () => {
  const sc = (state.scenarios || []).find((s) => s.key === $("scenario").value);
  const hint = $("scenarioHint");
  if (!sc) {
    $("goal").value = "";
    hint.hidden = true;
    return;
  }
  $("goal").value = sc.goal;
  $("taskType").value = "coordination";
  // Hand the work to the chief, so the tiers below are the ones who do it.
  const chief = $("agent");
  const match = [...chief.options].find((o) =>
    /executive/i.test(o.textContent),
  );
  if (match) chief.value = match.value;
  hint.textContent =
    tr("give.shouldback", "Should come back: ") + sc.deliverable;
  hint.hidden = false;
};

/* ---------------- who can be given the work ----------------
   **This selector was never filled.** It shipped as `<option value="">loading agents…</option>`
   and no code ever wrote to it, because the screen that used to populate it was one of the
   eight cut when the console went from eleven sections to three. A person opening `Give
   work` saw a picker stuck on "loading agents…" and, pressing Run, created a task with
   `owner_agent_id: null` — nobody to do it. It looked like a slow page; it was a dead
   control, and cutting a screen had taken a live control with it.

   Built from `/departments`, which the Departments screen already loads and which is the
   only endpoint that carries the whole hierarchy with agent names on it. One request, one
   source, and the picker cannot offer an agent the tree does not have.

   Order is the org chart: chief, then offices, then departments. A person picking an owner
   is choosing a tier, and the list reads top-down in the shape they know.

   **An unknown roster must say so rather than leave the placeholder.** "No agents found"
   is a statement about the company; "loading agents…" after the page has finished is a
   statement about the page, and it is the one that sent me looking in the wrong place. */
async function loadOwners() {
  const sel = $("agent");
  if (!sel) return;
  try {
    const d = await apiGet("/departments");
    const rows = [];
    const add = (n, tier) => {
      if (n && n.id && n.name) rows.push({ id: n.id, name: n.name, tier });
    };
    // In the `/departments` payload `second_tier` is the offices and `offices`
    // is the departments — a legacy naming that once had these two labels
    // swapped, offering every department as "an office" and every office as "a
    // department". Read the tier from which list the row came from, not from
    // the list's name.
    add(d.chief, tr("give.tier_company", "the whole company"));
    (d.second_tier || []).forEach((n) =>
      add(n, tr("give.tier_office", "an office")),
    );
    (d.offices || []).forEach((n) =>
      add(n, tr("give.tier_dept", "a department")),
    );
    if (!rows.length) {
      sel.innerHTML = `<option value="">${tr("give.noagents", "no agents found")}</option>`;
      sel.disabled = true;
      return;
    }
    sel.disabled = false;
    sel.innerHTML =
      `<option value="">${tr("give.chief_default", "the chief (default)")}</option>` +
      rows
        .map(
          (r) =>
            `<option value="${esc(r.id)}">${esc(r.name)} — ${esc(r.tier)}</option>`,
        )
        .join("");
    // Default to the chief: a coordination goal belongs at the top, and the tiers below
    // it only come into play when the person picks one deliberately.
    if (rows.length && rows[0].id) sel.value = rows[0].id;
  } catch (err) {
    sel.innerHTML = '<option value="">the roster did not load</option>';
    sel.disabled = true;
  }
}
