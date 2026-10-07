"use strict";
/* ==================================================================
   A small application. Four ideas, and the whole design follows them.

   1. THE URL IS THE STATE. Every view and every drill-down is a hash
      route, so the browser's own Back button works, a URL can be
      pasted to a colleague, and a refresh lands where you were. The
      first version of this page kept its selection in a JavaScript
      variable, which meant Back went to wherever you had been before
      the *site*, and there was no way out of a project at all. That
      was reported as "hard to use" and it was a routing bug.

   2. A LIST IS NEVER REPLACED BY ITS DETAIL. At >=1080px they sit
      side by side; below that the detail opens as a full panel with a
      Back control in the breadcrumb. Either way the thing you were
      reading is still there or one keystroke away.

   3. ABSENT IS NOT ZERO. A work package with a plan and no recorded
      actuals is hatched and carries no bar, in a different colour AND
      a different shape, because 100% of the real corpus's actual
      columns are copies of its planned ones. `actual_updated` is the
      only thing that knows the difference, so it is read on every row
      and decides the rendering.

   4. EVERY FIGURE IS SHOWN NEXT TO ITS POPULATION. "2778 readings"
      with "0 measured" underneath is a different statement from
      "2778 readings", and the second one is the dangerous one.

   The token is asked for and kept in this tab only. It is never
   written into the page, never put in a URL, and never sent anywhere
   but this origin. A token in a query string ends up in browser
   history, in Referer headers, and in every proxy log on the way.
   ================================================================== */

const API = "/api/v1";
const ORG = "__ORG_ID__";
const AUTH_OFF = __AUTH_OFF__;
const BUILD = "__BUILD__";
const PROVIDER = "__PROVIDER__";
const $ = (id) => document.getElementById(id);
const ORG_RE = /^org_[0-9a-z]{20,}$/;

$("build").textContent = BUILD;
if (AUTH_OFF) {
  $("authwarn").hidden = false;
  /* The button goes too. A control that cannot do anything is worse than an absent one:
     it invites a person to click it, and clicking it when auth is off does nothing at all. */
  const tokenBtn = $("tokenBtn");
  if (tokenBtn) tokenBtn.hidden = true;
}

/* ---------------- auth ---------------- */
function token() {
  return sessionStorage.getItem("ao_token") || "";
}
function setToken(v) {
  sessionStorage.setItem("ao_token", v.trim());
}
function forgetToken() {
  sessionStorage.removeItem("ao_token");
}
/* Accept what a person actually pastes, in the order they are likely to try it.
   A whole `INTERNAL_SERVICE_SECRET=…` line out of a .env is the most likely, and the
   old page unwrapped it. The rewrite dropped that, so a whole line was sent as the
   credential and refused with no visible cause — which is exactly the confusion this
   function exists to remove. */
function normaliseToken(v) {
  let out = String(v || "")
    .trim()
    .replace(/^Bearer\s+/i, "")
    .replace(/^export\s+/, "");
  /* A pasted `export INTERNAL_SERVICE_SECRET=…` line is the most likely thing to
     arrive here, and the `export ` prefix means the left-hand side is not a bare
     identifier -- so the key test has to come after it is stripped, or the whole line
     is sent as the credential and refused with no visible cause.

     Three rules, and the last two matter more than the first:

     * Only an **upper-case** identifier counts as an environment variable name. A
       lower-case `lower_case=s3cr3t` is not one, and unwrapping it would silently
       change a value nobody asked to change.
     * The split is on the **last** `=`, so a secret that itself contains `=` survives
       intact. Splitting on the first one truncates every such secret.
     * A key with a space in it is prose, not a variable, and is left alone. */
  const eq = out.lastIndexOf("=");
  if (eq > 0 && /^[A-Z0-9_]+$/.test(out.slice(0, eq)))
    out = out.slice(eq + 1).trim();
  return out.replace(/^["']|["']$/g, "").trim();
}

function askToken(initial) {
  /* **Never prompts when authentication is off.** One gate, here, rather than a
     `if (!AUTH_OFF)` at each of the three call sites -- which is how a third call site gets
     added without the check. Measured: with `AUTH_OFF = true` the page was still asking
     for a token, because `apiGet`/`apiPost` treat *any* 401 or 403 as a rejected
     credential. It is not. `require_human()` answers 403 with "this endpoint requires a
     human principal", which is a business answer about authorisation, not about identity --
     and the page turned it into a modal asking for a password.

     So the rule is: with auth off there is nothing to authenticate, a 401 means the
     deployment is misconfigured, and a 403 is an answer to show. */
  if (AUTH_OFF) return null;
  const v = window.prompt(
    tr(
      "auth.token_prompt",
      "Service token (sent as `Authorization: Bearer …`)",
    ),
    initial || "",
  );
  if (v === null) return null;
  /* One call, so there is no window in which an unnormalised token sits in
     sessionStorage -- and a token pasted with the word "Bearer" still works. */
  const clean = normaliseToken(v);
  if (clean) setToken(normaliseToken(v));
  else forgetToken();
  return clean;
}
function authHeaders(extra) {
  const headers = Object.assign({}, extra || {});
  /* With authentication off there is nothing to send, and sending an empty
     `Authorization` would be worse than sending none: it looks like a credential
     was presented and rejected, which is the exact confusion this mode removes. */
  if (!AUTH_OFF) headers["Authorization"] = "Bearer " + token();
  /* The tenant header is not optional. `authenticate` resolves a service
     credential's organization from `x-organization-id` and refuses with
     `missing_org_header` when it is absent — and the token check passes first, so
     the failure reads as a rejected token and names the wrong thing. The id is an
     identifier rather than a secret: it appears in every log line, so it travels in
     the page URL as `?org=` and is baked into this document. */
  if (ORG) headers["x-organization-id"] = ORG;
  return headers;
}
if (!AUTH_OFF && !token() && !ORG_RE.test(ORG)) askToken("");

/* ---------------- language ----------------
   Bilingual chrome, English default. `tr(key, fallback)` renders the current
   language and falls back to the English literal already at the call site, so
   the default render is byte-identical to the page that 96 checks verify —
   there is no second copy of English to drift. Vietnamese lives only in
   `STR.vi`; dynamic data (task titles, answers, logs, reasons, names, ids) is
   never translated, only the chrome around it.

   Static markup carries `data-i18n="key"` (text), `data-i18n-ph` (placeholder),
   `data-i18n-aria` (aria-label), `data-i18n-title` (title). The served document
   keeps its English inline, so the static-markup checks keep passing; the
   original is captured on first paint and restored when switching back.

   `localStorage` is guarded: the verification harness provides `sessionStorage`
   but no `localStorage`, and an unguarded read throws at boot — taking every
   check after it down with it. */
const STR = { vi: {} };
/* Vietnamese chrome, batch 1: shell (nav, bell, crumbs). Dynamic data is never
   translated — only the furniture around it. */
Object.assign(STR.vi, {
  "task.cancel": "Hủy công việc",
  "task.cancel_explain":
    "Hủy đúng công việc này và yêu cầu workflow dừng. Lịch sử vẫn được giữ lại.",
  "task.cancel_confirm": "Xác nhận hủy",
  "run.started_now":
    "Đã bắt đầu chạy nền. Thời gian và hạn mức phụ thuộc nhà cung cấp; trang này sẽ tiếp tục theo dõi.",
  "run.started_short": "Công việc đã bắt đầu chạy nền.",
  "bell.title": "Thông báo",
  "bell.clear": "Bỏ hết",
  "bell.empty":
    "Chưa có gì. Khi có phê duyệt chờ hoặc task xong, sẽ hiện ở đây lẫn popup.",
  "nav.org": "Tổ chức",
  "nav.departments": "Phòng ban",
  "nav.work": "Công việc",
  "nav.give": "Giao việc",
  "nav.needs": "Cần bạn",
});
/* Batch 2: departments screen (tiles, boxes, tiers, finder). */
Object.assign(STR.vi, {
  "dept.search": "Tìm phòng ban hoặc văn phòng…",
  "dept.filter": "Lọc theo văn phòng",
  "dept.legend":
    "đèn chạy ở viền nghĩa là đang làm; viền đứt nghĩa là tiến trình bị kẹt",
  "dept.empty": "Chưa có agent nào cho các phòng ban này.",
  "dept.nomatch": "Không có gì khớp. Xóa tìm kiếm hoặc chọn văn phòng khác.",
  "dept.back": "Về toàn tổ chức",
  "dept.did": "Việc đã làm",
  "dept.neverran": "Chưa chạy lần nào.",
  "dept.work": "Việc của đơn vị",
  "dept.open": "Đang mở",
  "dept.attn": "Cần chú ý",
  "dept.done": "Đã xong",
  "dept.nothing": "Chưa ghi nhận gì.",
  "dept.alloffices": "Mọi văn phòng",
  "dept.heading": "Trưởng, {o} văn phòng và {d} phòng ban",
  "dept.sub": "{o} văn phòng · {d} phòng ban · {e} lượt giao việc đã ghi nhận",
  "dept.noagent": "không có agent tên {n}",
  "dept.unassigned":
    "<b>{n} phòng ban chưa thuộc văn phòng nào</b> nên không có trong cây trên: {who}. Chúng vẫn được tính vào tổng số.",
  "tile.working": "Đang làm",
  "tile.of_agents": "trên {n} agent",
  "tile.open": "Việc đang mở",
  "tile.across": "trên {n} phòng ban",
  "tile.attention": "Cần chú ý",
  "tile.attention_sub": "thất bại hoặc bị từ chối",
  "tile.stranded": "Tiến trình kẹt",
  "tile.stranded_sub": "đã bắt đầu, chưa bao giờ đóng",
  "box.own_open": "tự mở",
  "box.own_done": "tự xong",
  "box.stuck": "kẹt",
  "box.stranded": "kẹt cứng",
  "box.roll":
    "{n} đơn vị dưới · mở <b>{o}</b> · xong <b>{d}</b>{att} · tổng <b>{n}</b> agent bên dưới",
  "box.roll_att": " · cần chú ý <b>{a}</b>",
  "tier.chief": "Trưởng",
  "tier.offices": "Các văn phòng và phòng ban",
  "tier.departments": "Phòng ban",
});
/* Batch 3: department panel. */
Object.assign(STR.vi, {
  "dept.nosuch": "Không có phòng ban này.",
  "dept.noagent": "chưa đăng ký agent",
  "dept.panel_sub": "{g} đã cấp, trần {c} · mô hình {m} · {r} lượt chạy",
  "dept.runs": "{n} lượt chạy gần đây",
  "dept.tok": "{n} tok",
  "dept.secs": "{n}s",
  "dept.stranded_flag": " · kẹt cứng",
  "dept.roll_sub":
    "của mình: {co} mở · {ca} cần chú ý · {cd} xong — với {n} đơn vị dưới: {o} mở · {a} cần chú ý · {d} xong",
  "dept.own_sub": "{co} mở · {ca} cần chú ý · {cd} xong",
  "dept.last_run":
    "<b>Lượt chạy gần nhất:</b> {s} trên {m} — {n} token. Tóm tắt ở trên cũng là chuỗi mà workflow dùng để rẽ nhánh, nên panel không thể nói một đằng, máy chạy một nẻo.",
  "dept.never_report": "Chưa chạy lần nào nên chưa có gì để báo cáo.",
});
/* Batch 4: give-work register rows, needs-you, approvals, shared filters. */
Object.assign(STR.vi, {
  "flt.everything": "Tất cả",
  "flt.you": "Chờ tôi",
  "flt.agent": "Agent đang làm",
  "flt.settled": "Đã xong",
  "flt.failed": "Thất bại",
  "flt.blocked": "Bị chặn",
  "flt.noowner": "Chưa có chủ",
  "give.start": "Giao việc mới",
  "give.start_sub": "bảo công ty làm gì, bằng lời của bạn",
  "give.pick": "Chọn một đầu việc",
  "give.what": "Công ty nên làm gì?",
  "give.goal_ph": "Đối chiếu hồ sơ đấu thầu",
  "give.who": "Ai làm việc này",
  "give.run": "Chạy",
  "give.run_hint":
    "Chạy trên mô hình miễn phí. Task điều phối có thể mất vài phút.",
  "give.workflow": "Luồng việc",
  "give.norun": "Chưa có gì chạy.",
  "give.now": "Ngay lúc này",
  "give.idle":
    "Chưa có gì diễn ra. Bấm <b>Chạy</b> và dòng này sẽ hiện dần khi các agent báo cáo — ai nhờ ai làm gì, và trả về gì.",
  "give.register": "Sổ công việc",
  "give.empty": "Sổ chưa có gì. Giao việc ở trên, vài giây sau sẽ hiện ở đây.",
  "give.note":
    "<b>Chọn một task, và log mới là điều đáng đọc.</b> Mỗi dòng ghi ai giữ việc, đã giao cho bao nhiêu đơn vị, và bao nhiêu quyết định đang chờ người. Task điều phối mà tự làm hết sẽ bị <b>nền tảng đánh trượt có chủ ý</b> — một đội mà việc gì cũng tự trả lời thì không phải là đội — nên cây giao việc là bằng chứng, nằm ở trang chi tiết.",
  "reg.waiting": "Chờ bạn",
  "reg.waiting_sub": "cần quyết định hoặc cần chủ",
  "reg.agent": "Agent đang làm",
  "reg.agent_sub": "đang chạy hoặc đã giao",
  "reg.settled": "Đã xong",
  "reg.settled_sub": "đã hoàn thành và báo cáo",
  "reg.total": "Tổng",
  "reg.total_sub": "task trong sổ",
  "row.held": " · {n} giữ",
  "row.noowner": " · chưa ai giữ",
  "row.handed": " · đã giao cho {n}",
  "row.subs": " · {n} việc con",
  "row.refused": "bị từ chối: {e}",
  "row.todecide": "{n} việc cần quyết",
  "row.you": "bạn",
  "row.agent": "một agent",
  "row.settled": "đã xong",
  "need.issues": "Sự cố",
  "need.issues_empty":
    "Không có gì hỏng. Mọi task hoặc đã xong hoặc vẫn đang được làm.",
  "need.approvals": "Phê duyệt",
  "need.decide": "Cần quyết định",
  "need.allpending": "Mọi việc đang chờ",
  "need.approvals_empty": "Không có gì chờ người.",
  "need.you": "Cần bạn",
  "need.you_sub": "một quyết định, một chủ, hoặc một phán xét",
  "need.failed": "Thất bại",
  "need.failed_sub": "công ty không hoàn thành được",
  "need.blocked": "Bị chặn",
  "need.blocked_sub": "đang chờ thứ gì đó",
  "need.approvals_t": "Phê duyệt",
  "need.approvals_sub": "đang chờ người",
  "need.look": "{n} việc cần xem",
  "need.noreason_short": "chưa ghi lý do",
  "need.noreason": "Chưa ghi nhận lý do cho kết quả này.",
  "need.task": "task",
  "need.open": "Mở ra",
  "need.retry": "Chạy lại",
  "need.sub_all": "{c} đang chờ · {l} vẫn trả lời được",
  "need.sub_live": "{l} việc chờ bạn trên {c} đang chờ",
  "need.clear": "trống",
  "need.note":
    "Việc cũ nhất trước, vì hàng đợi phê duyệt không có trường mức độ khẩn, ai làm theo thứ tự chèn là làm theo may rủi. <b>Hỏi</b> gửi task về cho agent kèm câu hỏi của bạn thay vì kết thúc nó — thường là đáp án đúng khi bạn chưa chắc.",
  "appr.approve": "Duyệt",
  "appr.ask": "Hỏi",
  "appr.reject": "Từ chối",
  "appr.expired": "hết hạn",
  "appr.reraise": "cần nêu lại — giờ không ai trả lời được",
  "appr.highrisk": "rủi ro cao",
  "appr.asked": "{n} yêu cầu",
  "appr.unknown": "không rõ",
  "appr.routed": "· chuyển tới {n}",
  "appr.queue": "· hàng chung",
  "appr.waiting": "· chờ {n} ngày",
});
/* Batch 5: task detail, sheets, run/retry, feed, crumbs, shell. */
Object.assign(STR.vi, {
  "task.answer": "Trả lời",
  "task.copy": "Chép",
  "task.noanswer":
    "Chưa có trả lời — các bước dưới cho biết việc đang tới đâu.",
  "task.decisions": "Quyết định",
  "task.nodecision": "Không có gì cần quyết.",
  "task.steps": "Các bước",
  "task.nosteps":
    "Chưa có gì chạy. Bấm <b>Chạy</b> ở dưới, mỗi lượt làm sẽ hiện dần — bấm vào từng bước để xem log và chi tiết.",
  "task.who": "Ai làm gì",
  "task.nograph": "Chưa giao cho ai.",
  "task.log": "Nhật ký",
  "task.runit": "Chạy",
  "task.runnow": "Chạy task này ngay",
  "task.runhint":
    "Chạy trên mô hình miễn phí. Một lượt chạy giao cho ba agent đã tốn <b>10 phút</b> và <b>67.000 token</b> — chạy nền và trang này sẽ theo dõi.",
  "task.finished_empty": "xong nhưng không có văn bản",
  "task.nothing_yet": "chưa ghi gì",
  "task.points": "{n} điểm đã trả lời",
  "task.copied": "Đã chép câu trả lời.",
  "task.copyfail": "Không chép được.",
  "task.readall": "Đọc hết",
  "task.answered_with": "Đã trả lời gồm: ",
  "task.finished_steps": "Đã xong. Các bước dưới ghi ai đã làm gì.",
  "task.held": "{n} giữ. ",
  "task.inflight": "Việc đang chạy — trang này vẫn theo dõi. ",
  "task.idle": "Chưa có gì chạy. Bấm <b>Chạy</b> ở dưới để bắt đầu.",
  "task.failed_means":
    "Nghĩa là: việc dừng ở đây, mọi thứ bên dưới chưa chạy vì lý do này. Sửa nguyên nhân — hoặc bấm <b>Chạy lại</b> để thử cùng việc dưới dạng task mới — các bước dưới là biên bản những gì đã thử.",
  "task.allissues": "Xem mọi sự cố",
  "task.graph_sub": "{t} task, {d} lượt giao",
  "task.appr_sub": "{n} việc trong task này",
  "task.decided": "{d} bởi {who} · {when}",
  "task.someone": "ai đó",
  "task.refuse": "Từ chối",
  "task.log_sub": "{n} sự kiện",
  "task.reported":
    "<b>Báo cáo:</b> {ty} này đã giao cho {to} agent{ref}, {ex} đã chạy và {fa} thất bại, {ap} quyết định cần người. Mô hình đã dùng: <code>{mo}</code>.",
  "task.reported_refused": ", {n} đã từ chối và nêu lý do",
  "task.reported_nomodel":
    "<b>Báo cáo:</b> {ty} này đã giao cho {to} agent{ref}, {ex} đã chạy, {fa} thất bại, {ap} quyết định cần người. <br><b>Không ghi tên mô hình</b> vì không có lượt chạy nào ghi lại — một bản tường trình không nói rõ mô hình nào đã hành động là bản tường trình không ai kiểm chứng được.",
  "task.reported_refused_short": ", {n} đã từ chối",
  "task.outcome": "Kết quả",
  "task.handed": "Đã giao cho",
  "task.handed_sub": "đơn vị hoặc agent",
  "task.ran": "Đã chạy",
  "task.ran_sub": "{n} thất bại",
  "task.inflight_t": "Đang chạy",
  "task.inflight_sub": "trên {n} lượt thử",
  "task.decisions_t": "Quyết định",
  "task.decisions_sub": "{n} chờ người",
  "step.done": "Xong.",
  "step.ended": "Kết thúc ở trạng thái {s}.",
  "step.issue": "Sự cố: {e}",
  "step.issuekind": "Loại sự cố: {k}.",
  "step.cost": "Chi phí: {c}.",
  "step.files": "giữ lại {n} file.",
  "step.sub": "{n} lượt làm · {f} đã xong",
  "step.stopped": "Dừng với lý do: ",
  "step.readall": "Đọc toàn bộ",
  "step.nooutput": "Lượt này không để lại văn bản.",
  "step.logfor": "Log của bước này ({n})",
  "step.model": "Mô hình:",
  "step.finished": " · xong ",
  "sheet.close": "Đóng",
  "sheet.full_answer": "Toàn văn câu trả lời",
  "sheet.answer_empty": "Câu trả lời này trống.",
  "sheet.agent_said": "Agent đã nói gì",
  "run.openfirst": "Mở một task trước.",
  "run.starting": "Đang bắt đầu…",
  "run.notstarted": "Chưa bắt đầu.",
  "run.notstarted_r": "Chưa bắt đầu: {r}",
  "run.started":
    '<span class="t-ok">Đã bắt đầu</span> chạy nền. Dự kiến khoảng <b>{m} phút</b> và <b>{tok} token</b> trên mô hình miễn phí. Trang này sẽ tiếp tục theo dõi.',
  "run.started_t": "Đã bắt đầu. Việc này mất khoảng {m} phút.",
  "run.couldnot": "Không bắt đầu được.",
  "run.couldnot_r": "Không bắt đầu được: {e}",
  "run.watching":
    '<span class="t-del">đang chạy</span> · {ex} đã chạy, {fa} thất bại, {ap} quyết định đang chờ · đã kiểm tra {tk} lần',
  "run.produced": "Lượt chạy đã ra kết quả. Báo cáo đã cập nhật.",
  "run.lost": "Mất dấu lượt chạy.",
  "retry.queued": "Đã xếp hàng thử lại.",
  "retry.running": "Đang chạy lại cùng việc dưới dạng task mới.",
  "retry.couldnot": "Không chạy lại được: {e}",
  "retry.unknown": "không rõ",
  "give.saywhat": "Hãy nói công ty nên làm gì.",
  "give.running": "Đang chạy…",
  "give.exists_not_running": "Task đã tạo nhưng chưa có gì chạy nó: {e}",
  "give.already": "Đang chạy rồi — không bắt đầu cái thứ hai.",
  "give.queued_not_running": "Đã xếp hàng nhưng chưa có gì chạy: {e}",
  "give.running_here":
    "Đang chạy ở đây. Khoảng {s}s và {tok} token — panel này sẽ hiện dần khi họ báo cáo.",
  "give.running_panel":
    "Đang chạy. Panel này sẽ hiện dần khi các agent báo cáo.",
  "give.something": "Việc khác (gõ ở dưới)",
  "give.shouldback": "Nên thu về: ",
  "give.tier_company": "cả công ty",
  "give.tier_office": "một văn phòng",
  "give.tier_dept": "một phòng ban",
  "give.noagents": "không tìm thấy agent",
  "give.chief_default": "trưởng (mặc định)",
  "appr.ask_q": "Agent nên trả lời gì trước khi bạn quyết?",
  "appr.approve_v": "Duyệt",
  "appr.reject_v": "Từ chối",
  "appr.confirm": "{a} yêu cầu này?",
  "appr.approved": "Đã duyệt.",
  "appr.rejected": "Đã từ chối.",
  "appr.sentback": "Đã gửi lại kèm câu hỏi của bạn.",
  "appr.couldnot_action": "Không {a} được: {e}",
  "appr.whyrefused":
    "Vì sao từ chối?\n\nLý do được giữ cùng quyết định để người đọc sau biết đã cân nhắc gì. Phê duyệt không có ghi chú chỉ là một dấu tick.",
  "appr.decided": "Đã quyết, và đã ghi vào log.",
  "appr.refused_note": "Đã từ chối, kèm lý do của bạn.",
  "appr.couldnot": "Không ghi nhận được: {e}",
  "agent.couldnot_read": "Không đọc được agent đó: {e}",
  "agent.why_stop":
    "Vì sao dừng agent này?\n\nLý do là bắt buộc — database từ chối lệnh dừng không lý do, và lý do được ghi vào log quyết định.",
  "agent.need_reason":
    "Bắt buộc có lý do. Lệnh dừng không lý do không khác gì tai nạn.",
  "agent.stopped": "Đã dừng, và đã ghi quyết định.",
  "agent.couldnot_stop": "Không dừng được agent đó: {e}",
  "agent.revive_q":
    "Chạy lại? Nó quay lại ở L1 — quyền cũ không được phục hồi.",
  "agent.running_l1": "Đang chạy lại, ở L1.",
  "agent.couldnot_revive": "Không chạy lại được agent đó: {e}",
  "agent.nopanel": "Agent đó không có panel.",
  "ago.now": "vừa xong",
  "ago.mins": "{n} phút trước",
  "ago.hours": "{n} giờ trước",
  "ago.days": "{n} ngày trước",
  "notif.decision": "Một quyết định đang chờ bạn",
  "notif.decision_sub": "có phê duyệt được yêu cầu",
  "notif.review": "Xem",
  "notif.failed": "Một task thất bại",
  "notif.failed_sub": "xem đã hỏng ở đâu",
  "notif.finished": "Một task đã xong",
  "notif.finished_sub": "xem đã thu về gì",
  "notif.open": "Mở",
  "feed.delegated": "{a} nhờ {b} làm việc",
  "feed.handed": "việc đã giao cho agent khác",
  "feed.refused": "{a} bị từ chối{r}",
  "feed.refused_r": ": {r}",
  "feed.refused_bare": "một lượt giao việc bị từ chối",
  "feed.decision": "một quyết định đang chờ người{a}",
  "feed.decision_at": " tại {n}",
  "feed.ran": "{n} đã chạy task",
  "feed.ran_bare": "task đã chạy",
  "feed.done": "{n} đã xong và báo cáo",
  "feed.done_bare": "task đã xong",
  "feed.failed": "{n} không hoàn thành được",
  "feed.review": "người kiểm tra đã xem kết quả và yêu cầu làm lại",
  "feed.learned": "agent đã ghi lại điều học được",
  "flow.count": "{n} task",
  "flow.untitled": "việc chưa đặt tên",
  "flow.hands": "{n} lượt làm bên dưới",
  "conn.connecting": "đang nối",
  "conn.live": "trực tiếp",
  "conn.retry": "đang nối lại",
  "conn.none": "mất kết nối",
  "err.load": "Không tải được màn này.",
  "err.reload": "Tải lại",
  "err.back": "Về Phòng ban",
  "task.graph_aria": "Đồ thị giao việc: {t} task, {d} lượt giao",
  "crumb.decide": "Quyết định",
  "crumb.dept": "Phòng ban",
  "crumb.back": "Quay lại",
  "crumb.back_btn": "‹ Quay lại",
  "auth.off": "Xác thực đang tắt.",
  "auth.off_sub":
    "Mọi request đều gán cho <code>dev:no-auth</code>. Tiện cho demo local — không bật được ở production hay test — và đừng coi đây là phiên đăng nhập thật.",
  "auth.token_prompt": "Service token (gửi dạng `Authorization: Bearer …`)",
  "auth.need_token": "Chưa được phép. Bấm Token để nhập service token.",
  "misc.nosummary": "chưa có tóm tắt",
  "prov.live": "Model thật",
  "prov.demo": "Chạy mô phỏng",
  "prov.live_sub":
    "lượt chạy dùng quota model miễn phí và làm việc thật; giữ server này chạy trong khi task thực hiện",
  "prov.demo_sub":
    "server này trả lời bằng output có sẵn — không có gì bấm ở đây hoàn thành thật được; dùng server live để làm việc thật",
});
function tr(key, fallback) {
  if (state.lang === "vi" && Object.hasOwn(STR.vi, key)) return STR.vi[key];
  return fallback;
}
function t_fmt(key, fallback, vars) {
  let s = tr(key, fallback);
  for (const [k, v] of Object.entries(vars || {}))
    s = s.split(`{${k}}`).join(String(v));
  return s;
}

Object.assign(STR.vi, {
  "nav.workspace": "Không gian làm việc",
  "nav.give": "Công việc",
  "nav.departments": "Tổ chức",
  "auth.local": "Môi trường cục bộ · chưa bật xác thực",
  "auth.off_sub":
    "Yêu cầu được ghi dưới tài khoản dev:no-auth. Chế độ này dành cho môi trường cục bộ.",
  "give.page_title": "Theo dõi mọi công việc",
  "give.intro":
    "Tìm công việc, xem người phụ trách, rồi mở kết quả và nhật ký của đúng công việc đó.",
  "give.new": "+ Giao việc mới",
  "give.register": "Danh sách công việc",
  "give.start": "Giao việc mới",
  "give.type": "Loại công việc",
  "give.who": "Người phụ trách",
  "give.search": "Tìm theo tên, người phụ trách hoặc mã task…",
  "give.now": "Hoạt động gần đây",
  "give.empty": "Không có công việc phù hợp. Xóa bộ lọc hoặc giao việc mới.",
  "dept.page_title": "Tổ chức và trách nhiệm",
  "dept.intro":
    "Trưởng điều hành, ba văn phòng và các phòng ban trực thuộc. Chọn một đơn vị để xem công việc của đơn vị đó.",
  "dept.open_run": "Mở lần thực thi này",
  "need.intro": "Mở công việc liên quan, đọc nguyên nhân và chọn cách xử lý.",
  "task.workspace": "Hồ sơ công việc",
  "task.answer": "Kết quả",
  "task.assignment": "Phân công",
  "task.subtasks": "Công việc và người phụ trách",
  "task.request": "Yêu cầu ban đầu",
  "task.events": "sự kiện",
  "task.log": "Nhật ký",
  "task.log_search": "Tìm trong nhật ký công việc này…",
  "task.noevents": "Không có sự kiện phù hợp.",
  "task.parent": "Công việc cấp trên",
  "task.root": "Toàn bộ mục tiêu",
  "task.error_details": "Chi tiết lỗi",
  "task.model_unavailable":
    "Chưa có model trả lời được lần chạy này. Mở chi tiết lỗi để xem nguyên nhân từ từng nhà cung cấp.",
  "task.stopped":
    "Lần chạy này đã dừng. Đọc nguyên nhân và các bước đã ghi nhận trước khi chạy lại.",
  "task.read_steps": "Đọc các bước đã thực hiện",
  "task.waiting": "Đang chờ người duyệt. Mở mục Quyết định để đọc bản nháp.",
  "task.no_model_recorded": "Tên model chưa được ghi nhận trong hồ sơ này.",
  "task.runhint":
    "Công việc chạy ở chế độ nền. Trang này cập nhật theo trạng thái thực tế.",
  "task.idle": "Có thể bắt đầu khi đã có người phụ trách.",
  "appr.draft": "Bản nháp chính xác đang được xin duyệt",
  "feed.created": "Công việc được tạo",
  "feed.started": "Bắt đầu thực hiện",
  "feed.approved": "Yêu cầu được duyệt",
  "feed.rejected": "Yêu cầu bị từ chối",
  "feed.unknown": "Sự kiện chưa có tên",
  "status.created": "Mới tạo",
  "status.assigned": "Đã giao",
  "status.running": "Đang chạy",
  "status.completed": "Hoàn tất",
  "status.failed": "Thất bại",
  "status.waiting_for_approval": "Chờ duyệt",
  "status.waiting_for_input": "Chờ đầu vào",
  "status.pending": "Chờ duyệt",
  "status.approved": "Đã duyệt",
  "status.rejected": "Từ chối",
  "status.needs_information": "Cần bổ sung thông tin",
  "status.canceled": "Đã hủy",
  "status.cancelled": "Đã hủy",
  "status.expired": "Hết hạn",
  "status.blocked": "Bị chặn",
});

Object.assign(STR.vi, {
  "step.profile": "Cấu hình:",
  "feed.platform": "Hệ thống",
  "feed.expired": "Yêu cầu đã hết hạn",
  "feed.cancelled": "Công việc đã bị hủy",
  "log.previous": "Sự kiện trước",
  "log.next": "Sự kiện tiếp theo",
  "task.loading": "Đang tải công việc…",
  "task.runhint":
    "Dùng hạn mức model miễn phí; thời gian phụ thuộc việc phân công. Công việc chạy nền và trang này theo dõi trạng thái thực tế.",
});
function langGet() {
  try {
    const v = window.localStorage && window.localStorage.getItem("ao-lang-v1");
    return v === "en" ? "en" : "vi";
  } catch {
    return "vi";
  }
}
function langSet(v) {
  state.lang = v === "vi" ? "vi" : "en";
  try {
    if (window.localStorage)
      window.localStorage.setItem("ao-lang-v1", state.lang);
  } catch {
    /* a private window without storage still gets the session language */
  }
  try {
    if (document.documentElement) document.documentElement.lang = state.lang;
  } catch {
    /* the harness document has no element to label */
  }
  const b = $("langBtn");
  if (b && !window.UIShell) b.textContent = state.lang === "vi" ? "EN" : "VI";
  paintStatic();
  if (state.connection) setConn(...state.connection);
  paintSidebar();
  window.UIShell?.refresh();
}
function paintStatic() {
  const vi = state.lang === "vi";
  const paint = (attr, get, set) => {
    for (const el of document.querySelectorAll(`[${attr}]`)) {
      try {
        const key = el.getAttribute(attr);
        if (el.dataset.i18nOrig === undefined) el.dataset.i18nOrig = get(el);
        set(
          el,
          vi && Object.hasOwn(STR.vi, key) ? STR.vi[key] : el.dataset.i18nOrig,
        );
      } catch {
        /* one unpaintable label must not break the screen */
      }
    }
  };
  paint(
    "data-i18n",
    (el) => el.textContent,
    (el, v) => {
      el.textContent = v;
    },
  );
  /* `data-i18n-html` for labels whose English carries markup (`<b>Run</b>`).
     A `textContent` paint would print the tags literally in Vietnamese, so the
     HTML variant is painted through `innerHTML` — served English untouched. */
  paint(
    "data-i18n-html",
    (el) => el.innerHTML,
    (el, v) => {
      el.innerHTML = v;
    },
  );
  paint(
    "data-i18n-ph",
    (el) => el.getAttribute("placeholder") || "",
    (el, v) => {
      el.setAttribute("placeholder", v);
    },
  );
  paint(
    "data-i18n-aria",
    (el) => el.getAttribute("aria-label") || "",
    (el, v) => {
      el.setAttribute("aria-label", v);
    },
  );
  paint(
    "data-i18n-title",
    (el) => el.getAttribute("title") || "",
    (el, v) => {
      el.setAttribute("title", v);
    },
  );
}

/* The provider pill: what actually runs when Run is pressed.

   `fake` (the dev/verify server) answers with a scripted canned output, so no
   task pressed there can ever genuinely finish — every one of them failed
   `output_contract_unmet` producing `proposal_count, scripted`. `openrouter`
   (the chairman's server) spends the free-model quota and does the work. The
   mode is painted on both run controls because a button that promises a model
   while the server runs a script is a lie the page must not tell. */
function paintProvider() {
  const live = (PROVIDER || "") !== "fake";
  const pill = (extra) =>
    `<span class="pill ${live ? "ok" : "bad"}">${live ? tr("prov.live", "Live model") : tr("prov.demo", "Simulated runs")}</span>` +
    (extra || "");
  setHTML(
    "providerPill",
    pill(
      live
        ? `<span> · ${tr("prov.live_sub", "runs spend the free-model quota and do the work; keep this server running while a task executes")}</span>`
        : `<span> · ${tr("prov.demo_sub", "this server answers with canned outputs — nothing pressed here can genuinely finish; use the live server for real work")}</span>`,
    ),
  );
  setHTML("runProvider", pill(""));
}

/* ---------------- state ---------------- */
const state = {
  lang: langGet(),
  es: null,
  paused: false,
  bootedAt: Date.now(),
  filter: "all",
  approvalFilter: "pending",
  giveFilter: "",
  decisionFilter: "",
  tasks: new Map(),
  agents: new Map(),
  events: [],
  selected: null,
  backfill: new Set(),
  portfolio: null,
  projects: [],
  project: null,
  workspace: null,
  inbox: [],
  // The task currently open in `#/task/...`. The retry button needs to know which one,
  // and there is exactly one task on that screen, so this is the whole of the state it
  // adds. Keyed by nothing else, and read only by the handler below.
  taskId: null,
};

function esc(s) {
  return String(s == null ? "" : s).replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
}
// Preserve stored words and identifiers while presenting structured values without JSON.
function valueText(value) {
  if (value == null) return "—";
  if (Array.isArray(value)) return value.map(valueText).join("\n");
  if (typeof value === "object")
    return Object.entries(value)
      .map(([key, item]) => `${humanKey(key)}: ${valueText(item)}`)
      .join("\n");
  return String(value);
}
function valueHTML(value) {
  if (Array.isArray(value))
    return value.length
      ? `<ul class="record-list">${value.map((item) => `<li>${valueHTML(item)}</li>`).join("")}</ul>`
      : `<span class="muted">${esc(tr("record.empty", "No entries"))}</span>`;
  if (value && typeof value === "object")
    return Object.keys(value).length
      ? `<dl class="record-fields">${Object.entries(value)
          .map(
            ([key, item]) =>
              `<div><dt>${esc(humanKey(key))}</dt><dd>${valueHTML(item)}</dd></div>`,
          )
          .join("")}</dl>`
      : `<span class="muted">${esc(tr("record.empty", "No entries"))}</span>`;
  return `<span class="record-value">${esc(valueText(value))}</span>`;
}
/* What a run said, cleaned up for somebody who is not reading logs.
 *
 * Three things were reaching this line raw, all visible in a screenshot:
 *
 * 1. `**bold**` markers. An agent writes markdown, and the page showed the
 *    asterisks. It reads as a broken page, not as emphasis.
 * 2. Raw tool-call JSON -- `{"tool":"read","args":{"path":"..."}}` -- printed
 *    in the middle of a business page. That is a diagnostic, and a diagnostic
 *    is what you put in the console, not in front of a hiring manager.
 * 3. Our own engineering prose. "closed by the stranded-execution sweep rather
 *    than left to make the platform claim somebody is working on it" was a
 *    maintainer explaining a design decision to other maintainers, shown to
 *    everyone else. It is a good comment and the wrong sentence on a page.
 *
 * The rule applied: keep what the agent said, drop what we did to the agent.
 * A person deciding whether work is finished needs the finding, not the
 * machinery. */
function readable(raw, limit = 900) {
  let t = String(raw == null ? "" : raw).trim();
  if (!t) return tr("misc.nosummary", "no summary");

  // 2. Tool-call JSON, whole lines of it.
  t = t
    .replace(/^\s*[\[{].*[\]}]\s*$/gm, "")
    .replace(/\{\s*"tool"[^]*?\}\n?/g, "")
    .trim();

  // 3. Our own engineering prose, reduced to the fact it reports.
  t = t
    .replace(
      /;\s*closed by the stranded-execution sweep rather than left to make the platform claim somebody is working on it\.?/gi,
      ".",
    )
    .replace(/\s*\(seeded by the organization[^)]*\)/gi, "")
    .trim();

  // 1. Markdown, rendered. Emphasis, inline code, bullets -- escaped first,
  //    because the text came from a model and is not trusted markup.
  t = esc(t)
    .replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<i>$2</i>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/^#{1,6}\s*(.+)$/gm, "<b>$1</b>")
    .replace(/^[-*]\s+/gm, "• ")
    .replace(/\n{2,}/g, "<br><br>")
    .replace(/\n/g, "<br>");

  return t.length > limit ? t.slice(0, limit).replace(/<[^>]*$/, "") + "…" : t;
}
/* Repeated titles, counted.
 *
 * A panel that lists eight identical rows tells a reader nothing they can act
 * on -- they cannot tell which one to open, and the shape of the problem is
 * invisible behind the repetition. Grouping by title keeps every row reachable
 * (the click still goes to the newest one) and makes the count the headline,
 * which is the number somebody actually wants.
 *
 * Newest first within a group: a task that failed an hour ago and one that
 * failed a minute ago are the same title and not the same problem, and the
 * recent one is the one to look at. */
function groupByTitle(rows) {
  const order = [];
  const groups = new Map();
  for (const t of rows || []) {
    const key = t.title || "(untitled)";
    if (!groups.has(key)) {
      groups.set(key, { title: key, status: t.status, n: 0 });
      order.push(key);
    }
    const g = groups.get(key);
    g.n += 1;
    g.status = t.status;
    g.id = t.id;
  }
  return order.map((k) => groups.get(k));
}
function taskRows(rows, cls) {
  return (rows || [])
    .map(
      (
        t,
      ) => `<a class="taskitem ${cls}" href="#/give/${encodeURIComponent(t.id)}">
    <span class="st">${esc(statusName(t.status))}</span><span class="grow">${esc(t.title)}</span>
    <span class="task-id">${esc(String(t.id).slice(-8))}</span></a>`,
    )
    .join("");
}
function setHTML(id, html) {
  const el = $(id);
  if (el) el.innerHTML = html;
}
function num(v, dp) {
  if (v === null || v === undefined) return "—";
  return new Intl.NumberFormat(undefined, {
    maximumFractionDigits: dp ?? 0,
  }).format(v);
}
function ago(iso) {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "—";
  const mins = Math.round((Date.now() - then) / 60000);
  if (mins < 1) return tr("ago.now", "just now");
  if (mins < 60) return t_fmt("ago.mins", "{n}m ago", { n: mins });
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return t_fmt("ago.hours", "{n}h ago", { n: hrs });
  return t_fmt("ago.days", "{n}d ago", { n: Math.round(hrs / 24) });
}

/* ---------------- toasts: honest about failure ---------------- */
let toastTimer = 0;
function toast(message, bad) {
  const el = $("toast");
  el.textContent = message;
  el.className = "toast show" + (bad ? " bad" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(
    () => {
      el.className = "toast";
    },
    bad ? 7000 : 4000,
  );
}

/* Notifications reconcile with the durable pending inbox, including after reload. */
const NOTIF_SEEN_KEY = "ao-notif-seen-v2:" + ORG;
let notifs = [];
try {
  const seen = JSON.parse(localStorage.getItem(NOTIF_SEEN_KEY) || "[]");
  if (Array.isArray(seen)) state._notifSeen = new Set(seen);
} catch {
  state._notifSeen = new Set();
}
if (!state._notifSeen) state._notifSeen = new Set();

function notifSeenIds() {
  // Bounded: the set only grows, and an unbounded list in localStorage is a slow leak.
  // 300 ids is far more than any session produces; the oldest are forgotten first.
  const ids = [...state._notifSeen].slice(-300);
  try {
    localStorage.setItem(NOTIF_SEEN_KEY, JSON.stringify(ids));
  } catch {
    /* private mode */
  }
}

function unreadCount() {
  return notifs.filter((n) => !n.read).length;
}

function paintBell() {
  const n = unreadCount();
  const badge = $("notifCount");
  if (badge) {
    badge.textContent = n ? String(n) : "";
    badge.hidden = !n;
  }
  window.UIShell?.bell(n);
  document.title = n ? `(${n}) Orchestrator` : "Orchestrator";
  const list = $("notifList");
  if (list) {
    setHTML(
      "notifList",
      notifs
        .slice()
        .reverse()
        .map(
          (nt) => `
      <a class="notif${nt.read ? " read" : ""}" href="${esc(nt.href || "#/work/approvals")}" data-notif="${esc(nt.id)}">
        <span class="ndot"></span>
        <div class="grow"><div class="nt">${esc(nt.title)}</div>
          <div class="ns">${esc(nt.body)}${nt.at ? " · " + esc(ago(nt.at)) : ""}</div></div>
      </a>`,
        )
        .join(""),
    );
  }
  const empty = $("notifEmpty");
  if (empty) empty.hidden = notifs.length > 0;
}

/** A popup with a jump link, plus a bell entry. One call, both surfaces. */
function notify({ id, kind, title, body, href, action, silent = false, at }) {
  if (!id || state.backfill.has("notified:" + id)) return;
  state.backfill.add("notified:" + id);
  const nt = {
    id,
    kind,
    title,
    body: body || "",
    href: href || "",
    at: at || new Date().toISOString(),
    read: state._notifSeen.has(id),
  };
  notifs.push(nt);
  const general = notifs.filter(n => !n.id.startsWith("approval:"));
  if (general.length > 60) {
    const expired = new Set(general.slice(0, general.length - 60).map(n => n.id));
    notifs = notifs.filter(n => !expired.has(n.id));
  }
  paintBell();
  if (silent || nt.read) return;
  // The popup. Text plus one link — a popup with two actions is a dialog, and this is
  // not the place for a decision, only for "go look".
  const el = $("toast");
  el.innerHTML =
    `<span>${esc(title)}${body ? " — " + esc(String(body).slice(0, 110)) : ""}</span>` +
    (href ? `<a href="${esc(href)}">${esc(action || "Open")}</a>` : "");
  el.className =
    "toast show" + (kind === "bad" ? " bad" : "") + (href ? " action" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    el.className = "toast";
  }, 9000);
}

function markNotifRead(id) {
  const nt = notifs.find((n) => n.id === id);
  if (nt && !nt.read) {
    nt.read = true;
    state._notifSeen.add(id);
    notifSeenIds();
    paintBell();
  }
}

$("notifBtn").onclick = (e) => {
  e.stopPropagation();
  const panel = $("notifPanel");
  panel.hidden = !panel.hidden;
  $("notifBtn").setAttribute("aria-expanded", String(!panel.hidden));
};
document.addEventListener("click", (e) => {
  const panel = $("notifPanel");
  if (panel && !panel.hidden && !e.target.closest(".bellwrap"))
    { panel.hidden = true; $("notifBtn").setAttribute("aria-expanded", "false"); }
  const row = e.target.closest("[data-notif]");
  if (row && row.dataset.notif) {
    const nt = notifs.find((n) => n.id === row.dataset.notif);
    markNotifRead(row.dataset.notif);
    if (nt && nt.href) go(nt.href.startsWith("#") ? nt.href : "#" + nt.href);
  }
});
$("notifClear").onclick = () => {
  for (const n of notifs)
    if (!n.read) {
      n.read = true;
      state._notifSeen.add(n.id);
    }
  notifSeenIds();
  paintBell();
};

/* Stream-triggered refresh plus a short recovery poll when the stream is unavailable. */
let badgeTimer = null;
let inboxRefreshing = false;
let inboxRefreshAgain = false;
let inboxInitialized = false;
async function refreshNotificationInbox() {
  if (inboxRefreshing) { inboxRefreshAgain = true; return; }
  inboxRefreshing = true;
  try {
    const items = [];
    for (let offset = 0; ; offset += 200) {
      const page = await apiGet("/approvals", { status: "pending", limit: 200, offset });
      items.push(...page.items);
      if (page.items.length < 200) break;
    }
    const live = new Set(items.map(a => "approval:" + a.id));
    notifs = notifs.filter(n => !n.id.startsWith("approval:") || live.has(n.id));
    for (const a of items) {
      notify({ id: "approval:" + a.id, kind: "warn",
        title: tr("notif.decision", "A decision is waiting on you"),
        body: a.reason || a.action_type,
        href: "#/approval/" + encodeURIComponent(a.id),
        action: tr("notif.review", "Review"),
        at: a.created_at, silent: !inboxInitialized });
    }
    // A resolved request may be reintroduced only with a new approval id.
    setHTML("navApprovalCount", items.length ? String(items.length) : "");
    $("navApprovalCount").hidden = !items.length;
    inboxInitialized = true;
    paintBell();
  } catch {
    // Preserve the last known inbox on transport errors; reconnect/poll retries it.
  } finally {
    inboxRefreshing = false;
    if (inboxRefreshAgain) { inboxRefreshAgain = false; scheduleBadgeRefresh(); }
  }
}
function scheduleBadgeRefresh() {
  if (badgeTimer) return;
  badgeTimer = setTimeout(() => {
    badgeTimer = null;
    refreshNotificationInbox();
  }, 100);
}

/** Classify one stream event into a notification, or nothing. Kept beside `eventLine`
    (which describes) because this one *decides* — and a classifier mixed into a
    renderer is how a new event type starts popping up where it should not. */
function notifFor(ev) {
  const type = String(ev.type || ev.event_type || "");
  const v = ev.view || {};
  const when = ev.occurred_at ? Date.parse(ev.occurred_at) : NaN;
  // History does not pop up: only events stamped after this page booted (with a 60s
  // tolerance for clock skew) may notify. The backfill replay still populates state.
  if (!Number.isNaN(when) && when < state.bootedAt - 60000) return null;
  if (/task\.failed/i.test(type)) {
    return {
      kind: "bad",
      title: tr("notif.failed", "A task failed"),
      body: v.title || tr("notif.failed_sub", "see what went wrong"),
      href: v.task_id ? `#/give/${encodeURIComponent(v.task_id)}` : "#/give",
      action: tr("notif.open", "Open"),
    };
  }
  if (/task\.completed/i.test(type)) {
    return {
      kind: "good",
      title: tr("notif.finished", "A task finished"),
      body: v.title || tr("notif.finished_sub", "see what came back"),
      href: v.task_id ? `#/give/${encodeURIComponent(v.task_id)}` : "#/give",
      action: tr("notif.open", "Open"),
    };
  }
  return null;
}

/* ---------------- api ---------------- */
async function apiGet(path, params) {
  const url = new URL(API + path, location.origin);
  if (params)
    for (const [k, v] of Object.entries(params)) {
      if (v !== null && v !== undefined && v !== "") url.searchParams.set(k, v);
    }
  const res = await fetch(url.toString(), { headers: authHeaders() });
  if (!res.ok) {
    const detail = await res.text().catch(() => "");
    let message = res.status + " " + res.statusText;
    try {
      message = JSON.parse(detail).error?.message || message;
    } catch {
      /* not json */
    }
    const err = new Error(message);
    err.status = res.status;
    /* Forget it, then ask again. Keeping a rejected credential means every
       subsequent request is made with it, so the page can never recover without a
       manual storage clear — and the prompt looks like it worked. This is the failure
       a person hits when their first paste is wrong. */
    /* A 403 is an authorisation *answer*, not a rejected credential: `require_human()`
       and `require_admin()` both use it, and with auth off it means the caller is a
       service principal doing something only a person may do. It is reported as an error
       and shown. Only a 401 is "your credential was refused". */
    if (res.status === 401 || (res.status === 403 && !AUTH_OFF)) {
      forgetToken();
      err.auth = true;
      askToken("");
    }
    throw err;
  }
  return res.json();
}
async function apiPost(path, body) {
  const r = await fetch(API + path, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(body || {}),
  });
  if (!r.ok) {
    const detail = await r.text().catch(() => "");
    let message = r.status + " " + r.statusText;
    try {
      const parsed = JSON.parse(detail);
      message =
        parsed.error?.message ||
        (Array.isArray(parsed.detail)
          ? parsed.detail
              .map((d) => `${(d.loc || []).slice(-1)[0]}: ${d.msg}`)
              .join("; ")
          : message);
    } catch {
      /* not json */
    }
    const err = new Error(message);
    err.status = r.status;
    if (r.status === 401 || (r.status === 403 && !AUTH_OFF)) {
      forgetToken();
      askToken("");
    }
    throw err;
  }
  return r.json();
}

function paintSidebar() {
  const collapsed = document.documentElement.classList.contains("sidebar-collapsed");
  const btn = $("sidebarToggle");
  btn.innerHTML = UIIcon(collapsed ? "panel-left-open" : "panel-left-close");
  btn.title = state.lang === "vi"
    ? (collapsed ? "Mở rộng thanh bên" : "Thu gọn thanh bên")
    : (collapsed ? "Expand sidebar" : "Collapse sidebar");
  btn.setAttribute("aria-label", btn.title);
  btn.setAttribute("aria-expanded", String(!collapsed));
  window.UIShell?.indicator();
  document.querySelectorAll("#navItems a").forEach(a => {
    const label = a.querySelector("[data-i18n]")?.textContent || a.textContent;
    a.setAttribute("aria-label", label.trim());
    a.title = label.trim();
  });
}
try {
  if (localStorage.getItem("ao-sidebar-collapsed") === "true")
    document.documentElement.classList.add("sidebar-collapsed");
} catch { /* storage may be unavailable */ }
$("sidebarToggle").onclick = () => {
  document.documentElement.classList.toggle("sidebar-collapsed");
  try { localStorage.setItem("ao-sidebar-collapsed", String(document.documentElement.classList.contains("sidebar-collapsed"))); } catch { }
  paintSidebar();
};
paintSidebar();
