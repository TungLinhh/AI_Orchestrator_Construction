/* Business workflow evidence is read from the durable task tree. */
const BusinessWorkflows = (() => {
  const {
    L,
    page,
    paint,
    body,
    card,
    fields,
    record,
    details,
    table,
    link,
    badge,
    empty,
    form,
    input,
    area,
    select,
    wireForm,
  } = Management;
  async function render(r, guard) {
    paint(
      page(
        L("Business workflows", "Workflow nghiệp vụ"),
        L(
          "MEP recruitment through onboarding, then procurement. Every stage has evidence and an explicit review mode.",
          "Tuyển MEP đến onboarding, rồi procurement. Mỗi bước có chứng cứ và chế độ duyệt rõ ràng.",
        ),
      ),
      guard,
    );
    const ctx = await apiGet("/console/context");
    if (!guard.current()) return;
    const selectedId =
      r.section === "overview" ? null : decodeRoutePart(r.section);
    if (!selectedId) {
      const rows = { items: [] };
      for (let offset = 0; ; offset += 100) {
        const batch = await apiGet("/workflows", { limit: 100, offset });
        if (!guard.current()) return;
        rows.items.push(...batch.items);
        if (offset + batch.items.length >= batch.total) break;
        if (!batch.items.length)
          throw new Error(
            L(
              "Refresh the changed workflow list.",
              "Làm mới danh sách workflow đã thay đổi.",
            ),
          );
      }
      body(
        card(
          L("Boss assigns a real recruitment", "Boss giao đợt tuyển thật"),
          form(
            "hiring-brief",
            input(
              "position",
              L("Position", "Vị trí"),
              "Kỹ sư MEP",
              "text",
              "required minlength=3",
            ) +
              input(
                "salary_min",
                L(
                  "Minimum monthly salary (VND)",
                  "Lương tháng tối thiểu (VND)",
                ),
                "",
                "number",
                "required min=1",
              ) +
              input(
                "salary_max",
                L("Maximum monthly salary (VND)", "Lương tháng tối đa (VND)"),
                "",
                "number",
                "required min=1",
              ) +
              input(
                "start_date",
                L("Expected start date", "Ngày dự kiến bắt đầu"),
                "",
                "date",
                "required",
              ) +
              area(
                "boss_brief",
                L(
                  "Discipline, experience, location, responsibilities and acceptance criteria",
                  "Chuyên ngành, kinh nghiệm, địa điểm, trách nhiệm và điều kiện chấp nhận",
                ),
                "",
                "required minlength=20",
              ),
            L("Create a real recruitment", "Tạo đợt tuyển thật"),
            !ctx.can_administer,
            L(
              "One position per workflow. Creation does not start execution. Reviews require real human approval; interviews and onboarding require source evidence.",
              "Một vị trí mỗi workflow. Tạo xong chưa chạy. Các cổng chờ người duyệt thật; phỏng vấn và onboarding cần chứng cứ nguồn.",
            ),
          ),
        ) +
          card(
            L(
              "Create an acceptance exercise",
              "Tạo lượt kiểm tra đầu đến cuối",
            ),
            `<p>${esc(L("Uses fictional inputs. MEP sends test CVs only to the connected mailbox itself. Human reviews, interviews and onboarding acknowledgements are explicitly simulated. A configured real model drafts the artifacts.", "Dùng dữ liệu hư cấu. MEP gửi CV thử về chính hộp thư đã kết nối. Duyệt của người, phỏng vấn và xác nhận onboarding được ghi rõ là mô phỏng. Model thật soạn và đánh giá sản phẩm."))}</p><div class="btn-row"><button class="btn" id="create-mep" ${ctx.can_administer ? "" : "disabled"}>${esc(L("Create MEP workflow", "Tạo workflow MEP"))}</button><button class="btn" id="create-procurement" ${ctx.can_administer ? "" : "disabled"}>${esc(L("Create procurement workflow", "Tạo workflow procurement"))}</button></div>`,
          ) +
          card(
            L("Recorded runs", "Các lượt đã ghi nhận"),
            rows.items.length
              ? table(
                  [
                    L("Work", "Công việc"),
                    L("Mode", "Chế độ"),
                    L("State", "Trạng thái"),
                  ],
                  rows.items.map((v) => [
                    link("#/processes/workflows/" + v.id, v.title),
                    badge(v.mode),
                    badge(v.status),
                  ]),
                )
              : empty(),
          ),
        guard,
      );
      wireForm(
        "hiring-brief",
        async (values) => {
          const created = await apiPost("/workflows/hiring", {
            ...values,
            salary_min: Number(values.salary_min),
            salary_max: Number(values.salary_max),
          });
          if (guard.current()) go("#/processes/workflows/" + created.id);
        },
        guard,
      );
      for (const [button, kind] of [
        ["create-mep", "mep_hiring"],
        ["create-procurement", "procurement"],
      ]) {
        document.getElementById(button).onclick = async () => {
          document.getElementById(button).disabled = true;
          try {
            const created = await apiPost("/workflows/examples", { kind });
            go("#/processes/workflows/" + created.id);
          } catch (err) {
            toast(err.message, true);
            if (guard.current())
              document.getElementById(button).disabled = false;
          }
        };
      }
      return;
    }
    let refreshTimer = null, feedbackDraft = "";
    async function refresh() {
      if (refreshTimer) clearTimeout(refreshTimer);
      const draftInput = document.getElementById("workflow-feedback")?.querySelector("textarea");
      if (draftInput) feedbackDraft = draftInput.value;
      const h = await apiGet(`/workflows/${encodeURIComponent(selectedId)}`);
      if (!guard.current()) return;
      const canRun = !h.waiting && !h.control?.paused && !["completed", "failed", "canceled", "expired"].includes(
        h.status,
      );
      const intake = h.stages.find((s) => s.key === "cv_intake");
      const candidates = h.stages.find((s) => s.key === "scoring");
      const names = new Map(
        (intake?.output?.cvs || []).map((cv) => [cv.candidate_id, cv.filename]),
      );
      const scoreTable = candidates?.output?.candidates?.length
        ? card(
            L("CV scores with source evidence", "Điểm CV và căn cứ nguồn"),
            table(
              [
                L("CV", "CV"),
                L("Score / 100", "Điểm / 100"),
                L("Recommendation", "Đề xuất"),
              ],
              candidates.output.candidates.map((c) => [
                esc(names.get(c.candidate_id) || c.candidate_id),
                esc(c.score),
                esc(c.recommendation),
              ]),
            ) +
              candidates.output.candidates
                .map((c) =>
                  details(names.get(c.candidate_id) || c.candidate_id, c),
                )
                .join(""),
          )
        : "";
      const active = h.stages.find(stage => stage.status !== "completed");
      const journey = `<nav class="campaign-journey" aria-label="${esc(L("Campaign journey", "Luồng đợt tuyển"))}">${[
        ["brief", L("1 · Boss brief", "1 · Brief của Boss"), "brief"],
        ["campaign", L("2 · Follow campaign", "2 · Theo dõi đợt tuyển"), "stages"],
        ["reviews", L("3 · Decisions", "3 · Việc cần duyệt"), "decisions"],
        ["onboarding", L("4 · Onboarding", "4 · Hồ sơ onboarding"), "onboarding"]
      ].map(([key, label, anchor]) => `<a href="#/processes/workflows/${h.id}" data-campaign-anchor="${anchor}" ${key === (active?.key === "brief" ? "brief" : active?.key?.startsWith("onboarding") ? "onboarding" : active?.status === "waiting_for_approval" ? "reviews" : "campaign") ? 'aria-current="step"' : ""}>${esc(label)}</a>`).join("")}</nav>`;
      body(
        journey + `<div class="campaign-progress" aria-label="${h.completed} / ${h.total}"><span style="width:${Math.round(h.completed / Math.max(h.total, 1) * 100)}%"></span></div>` +
        `<div class="btn-row">${link("#/processes/workflows", L("All workflows", "Tất cả workflow"))}${link("#/give/" + h.id, L("Open task tree and events", "Mở cây task và sự kiện"))}${link("#/work/approvals", L("Human approval inbox", "Hộp thư cần người duyệt"))}<button class="btn" id="workflow-run" ${ctx.can_administer && canRun ? "" : "disabled"}>${esc(L("Run / resume workflow", "Chạy / tiếp tục workflow"))}</button><button class="btn sm" id="workflow-refresh">${esc(L("Refresh evidence", "Cập nhật chứng cứ"))}</button></div>` +
          card(
            h.title,
            fields([
              [L("Exact run", "Lượt chính xác"), h.id],
              [L("Mode", "Chế độ"), h.mode],
              [L("State", "Trạng thái"), h.status],
              [L("Current reason", "Lý do hiện tại"), h.error || "—"],
              [
                L("Completed stages", "Bước hoàn thành"),
                h.completed + " / " + h.total,
              ],
              [
                L("Real / fake model calls", "Lượt model thật / giả"),
                h.evidence.real_model_calls +
                  " / " +
                  h.evidence.fake_model_calls,
              ],
              [
                L("Recorded events / audits", "Sự kiện / audit đã ghi"),
                h.evidence.events + " / " + h.evidence.audits,
              ],
            ]) +
              `<p>${esc(h.mode === "simulation" ? L("SIMULATION: no real person approved, no real candidate was hired and no production purchase was issued.", "MÔ PHỎNG: chưa có người duyệt thật, chưa tuyển người thật và chưa phát hành mua hàng production.") : L("Live: mandatory reviews await real human decisions.", "Luồng thật: các cổng bắt buộc chờ quyết định của người."))}</p>` +
              details(
                L(
                  "O-Nexus procedure references",
                  "Tham chiếu quy trình O-Nexus",
                ),
                h.sources,
              ),
          ) +
          card(L("Next step", "Bước tiếp theo"), `<div id="campaign-decisions">${active ? `<p><b>${esc(active.title)}</b> ${badge(active.status)}</p>${link("#/give/" + active.id, L("Open the work and evidence", "Mở công việc và chứng cứ"))}` : `<p>${esc(L("Campaign complete; review the recorded onboarding handover below.", "Đợt tuyển hoàn thành; xem hồ sơ bàn giao onboarding bên dưới."))}</p>`}</div>`) +
          waitingPanel(h) + feedbackPanel(h, ctx) + receiptPanel(h, ctx) +
          scoreTable +
          (h.mail_intake
            ? card(
                L("Receive CVs for this run", "Nhận CV cho đúng đợt này"),
                fields([
                  [
                    L("Mailbox", "Hộp thư"),
                    h.mail_intake.address || L("Not connected", "Chưa kết nối"),
                  ],
                  [
                    L("Required email subject", "Subject email cần có"),
                    h.mail_intake.subject,
                  ],
                ]),
              )
            : "") +
          card(L("Result", "Kết quả"), record(h.summary)) +
          card(
            L("Complete ordered stage log", "Log đầy đủ theo thứ tự"),
            h.stages
              .map(
                (s, n) =>
                  `<div class="card" id="campaign-stage-${esc(s.key)}"><h3>${n + 1}. ${esc(s.title)} ${badge(s.status)}</h3><div class="btn-row">${link("#/give/" + s.id, L("Task and execution evidence", "Task và chứng cứ thực thi"))}${s.owner_agent_id ? link("#/agent/" + s.owner_agent_id, L("Responsible agent", "Agent phụ trách")) : ""}${s.reused_from ? link("#/give/" + s.reused_from, L("Verified artifact reused from this task", "Sản phẩm đã kiểm chứng được dùng lại từ task này")) : ""}</div>${s.error ? `<p class="error">${esc(s.error)}</p>` : ""}${details(L("Produced artifact", "Sản phẩm đã tạo"), s.output)}</div>`,
              )
              .join(""),
          ),
        guard,
      );
      const evidenceStage =
        h.mode === "live" &&
        h.stages.find(
          (s) =>
            s.status === "waiting_for_input" &&
            [
              "interview_technical",
              "interview_hr",
              "offer_acceptance",
              "onboarding_setup",
              "delivery",
            ].includes(s.key),
        );
      if (evidenceStage && !h.waiting && ctx.can_approve) {
        const container = document.createElement("div");
        container.innerHTML = card(
          L("Supply the missing evidence", "Bổ sung chứng cứ đang chờ"),
          form(
            "workflow-evidence",
            input("source", L("Source document or record link", "Tài liệu nguồn hoặc đường dẫn hồ sơ"), "", "text", "required minlength=5") +
            (evidenceStage.key.startsWith("interview_") ? input("filename", L("CV filename", "Tên file CV"), "", "text", "required") + select("result", L("Interview result", "Kết quả phỏng vấn"), [["pass", L("Pass", "Đạt")], ["fail", L("Fail", "Chưa đạt")]]) + area("notes", L("Interview notes and evidence", "Ghi nhận phỏng vấn và căn cứ"), "", "required minlength=10") :
            evidenceStage.key === "offer_acceptance" ? input("offer_hash", L("Reviewed offer hash", "Hash offer đã duyệt"), h.stages.find(stage => stage.key === "offer")?.artifact_hash || "", "text", "required") + select("accepted", L("Candidate acceptance", "Ứng viên chấp nhận"), [["false", L("Not yet", "Chưa chấp nhận")], ["true", L("Accepted", "Đã chấp nhận")]]) :
            evidenceStage.key === "onboarding_setup" ? ["hr_documents", "safety_induction", "role_briefing", "access_verified", "mentor_handover"].map(key => area(key, {hr_documents: L("HR documents", "Hồ sơ HR"), safety_induction: L("Safety induction", "Đào tạo an toàn"), role_briefing: L("Role briefing", "Bàn giao vai trò"), access_verified: L("Verified access", "Quyền truy cập đã kiểm tra"), mentor_handover: L("Mentor handover", "Bàn giao người hướng dẫn")}[key], "", "required minlength=5")).join("") : area("evidence", L("Delivery record (JSON)", "Hồ sơ giao hàng (JSON)"), "", "required")),
            L("Record evidence", "Ghi nhận chứng cứ"),
            false,
            L(
              "Include source and the actual records. Interview: transcripts with filename, result and notes. Acceptance: accepted and the approved offer_hash. Onboarding: hr_documents, safety_induction, role_briefing, access_verified, mentor_handover. Recording does not approve the next gate.",
              "Ghi source và hồ sơ thực tế. Phỏng vấn: transcripts gồm filename, result, notes. Nhận offer: accepted và offer_hash đã duyệt. Onboarding: hr_documents, safety_induction, role_briefing, access_verified, mentor_handover. Ghi nhận chưa duyệt cổng tiếp theo.",
            ),
          ),
        );
        document.getElementById("managementBody").appendChild(container);
        wireForm(
          "workflow-evidence",
          async (values) => {
            const result = await apiPost("/workflows/" + h.id + "/evidence", {
              stage_key:
                evidenceStage.key === "onboarding_setup"
                  ? "onboarding_evidence"
                  : evidenceStage.key,
              evidence: evidenceStage.key.startsWith("interview_") ? {source: values.source, synthetic: false, transcripts: [{filename: values.filename, result: values.result, notes: values.notes}]} :
                evidenceStage.key === "offer_acceptance" ? {source: values.source, synthetic: false, accepted: values.accepted === "true", offer_hash: values.offer_hash} :
                evidenceStage.key === "onboarding_setup" ? {...values, synthetic: false} : JSON.parse(values.evidence),
            });
            return result;
          },
          guard,
        );
      }
      if (h.status === "failed" && ctx.can_administer) {
        const retryButton = document.createElement("button");
        retryButton.className = "btn";
        retryButton.textContent = L(
          "Retry as a new workflow",
          "Thử lại bằng workflow mới",
        );
        document
          .getElementById("workflow-run")
          .parentNode.appendChild(retryButton);
        retryButton.onclick = async () => {
          retryButton.disabled = true;
          try {
            const next = await apiPost("/workflows/" + h.id + "/retry", {});
            go("#/processes/workflows/" + next.id);
          } catch (err) {
            toast(err.message, true);
            retryButton.disabled = false;
          }
        };
      }
      document.getElementById("workflow-refresh").onclick = () =>
        refresh().catch((err) => toast(err.message, true));
      document.getElementById("workflow-run").onclick = async () => {
        document.getElementById("workflow-run").disabled = true;
        try {
          await apiPost("/workflows/" + h.id + "/run", {});
          await refresh();
        } catch (err) {
          toast(err.message, true);
        }
      };
      wireFeedback(h, ctx, guard);
      const feedbackInput = document.getElementById("workflow-feedback")?.querySelector("textarea");
      if (feedbackInput) feedbackInput.value = feedbackDraft;
      document.querySelectorAll("[data-campaign-anchor]").forEach(anchor => anchor.onclick = event => {
        event.preventDefault();
        const target = anchor.dataset.campaignAnchor;
        document.getElementById(target === "onboarding" ? "campaign-stage-onboarding_plan" : target === "brief" ? "campaign-stage-brief" : target === "decisions" ? "campaign-decisions" : "campaign-stage-" + (active?.key || "brief"))?.scrollIntoView({behavior: "smooth"});
      });
      if (active?.status === "waiting_for_approval") {
        const approvals = await apiGet(`/tasks/${encodeURIComponent(active.id)}/approvals`);
        if (guard.current() && approvals.items?.length) document.getElementById("campaign-decisions").insertAdjacentHTML("beforeend", link("#/work/approvals/" + approvals.items[0].id, L("Review this decision", "Duyệt quyết định này")));
      }
      if (["running", "assigned"].includes(h.status) || ["pending", "running", "suspended"].includes(h.control?.state) || ["requested", "thinking"].includes(h.control?.feedback?.state))
        refreshTimer = setTimeout(() => {
          if (guard.current())
            refresh().catch((err) => toast(err.message, true));
        }, 10000);
    }
    await refresh();
  }
  function waitingPanel(h) {
    if (!h.waiting) return "";
    return card(L("Recruitment needs a decision", "Đợt tuyển cần người phụ trách xử lý"),
      `<div role="status"><p>${esc(h.waiting.message)}</p>${fields([
        [L("Approved threshold", "Ngưỡng đã duyệt"), h.waiting.threshold],
        [L("Highest recorded score", "Điểm cao nhất đã ghi"), h.waiting.highest_score ?? "—"],
        [L("Assessed CVs", "CV đã đánh giá"), h.waiting.candidate_count],
      ])}<p>${esc(L("Use feedback below to plan a new revision with new source evidence, or cancel from the task tree. Resuming this campaign cannot change completed scores, interviews or approvals.", "Dùng feedback bên dưới để lên kế hoạch revision mới với nguồn bổ sung, hoặc hủy từ cây task. Tiếp tục đợt này không thay điểm, phỏng vấn hay quyết định đã ghi."))}</p></div>`);
  }
  function feedbackPanel(h, ctx) {
    if (!["completed", "failed", "canceled", "expired"].includes(h.status) && (!h.control?.paused || h.control?.feedback?.state === "failed"))
      return card(L("Adjust the work", "Điều chỉnh công việc"), `<div class="feedback-discussion">${form("workflow-feedback", area("message", L("Feedback or a change in direction", "Feedback hoặc thay đổi yêu cầu"), "", "required minlength=10 maxlength=10000"), L("Pause and ask the agent to reconsider", "Tạm dừng và yêu cầu agent xem lại"), !ctx.can_administer, L("Pauses at a safe boundary. Completed evidence and reviews remain recorded.", "Dừng tại ranh giới an toàn. Chứng cứ và quyết định đã hoàn thành vẫn được giữ."))}</div>`);
    const feedback = h.control?.feedback;
    if (!feedback?.state) return "";
    const assessment = feedback.assessment;
    return card(L("Feedback discussion", "Trao đổi feedback"), `<p>${esc(feedback.message)}</p>${badge(feedback.state)}${feedback.destination_root_id && feedback.destination_root_id !== h.id ? link("#/processes/workflows/" + feedback.destination_root_id, L("Open the new campaign revision", "Mở revision mới của đợt tuyển")) : ""}${feedback.error ? `<p class="error">${esc(feedback.error)}</p>` : ""}` +
      (assessment ? `<p>${esc(assessment.understanding)}</p>${assessment.questions?.length ? `<ul>${assessment.questions.map(q => `<li>${esc(q)}</li>`).join("")}</ul>` : ""}${table([L("Action", "Công việc"), L("Owner", "Phụ trách"), L("Acceptance", "Điều kiện hoàn thành")], assessment.plan.map(step => [esc(step.action), esc(step.owner), esc(step.acceptance)]))}<p><b>${esc(L("Proposed skill lesson", "Bài học skill đề xuất"))}:</b> ${esc(assessment.skill_lesson)}</p>` : `<p>${esc(L("The agent is reviewing the feedback. This page will refresh.", "Agent đang xem lại feedback. Trang sẽ tự cập nhật."))}</p>`) +
      (feedback.state === "awaiting_confirmation" ? form(assessment?.questions?.length ? "workflow-feedback-answer" : "workflow-feedback-confirm", area("answers", L("Answers and corrections to the plan", "Trả lời câu hỏi và bổ sung kế hoạch"), "", assessment?.questions?.length ? "required minlength=5" : "") + select("new_revision", L("Apply to", "Áp dụng cho"), [["false", L("Remaining work in this campaign", "Phần việc còn lại của đợt này")], ["true", L("A new revision; review completed work again", "Revision mới; duyệt lại phần đã hoàn thành")]], "false"), assessment?.questions?.length ? L("Send answers for model review", "Gửi câu trả lời để model xem lại") : L("Confirm plan and continue", "Xác nhận kế hoạch và tiếp tục"), !ctx.can_administer, L("New corrections go back to the model. Leave the box empty to confirm the reviewed plan. A new revision requests fresh reviews.", "Nhập bổ sung sẽ gửi model xem lại. Để trống để xác nhận kế hoạch đã xem. Revision mới yêu cầu các quyết định duyệt mới.")) : ""));
  }
  function receiptPanel(h, ctx) {
    if (!h.connector_actions?.length) return "";
    return card(L("External-action receipts", "Receipt hành động bên ngoài"), h.connector_actions.map(action => `<div><p>${badge(action.state)} ${esc(action.id)}</p>${details(L("Prepared input and observed receipt", "Đầu vào đã lưu và receipt quan sát"), {input: action.snapshot, receipt: action.receipt, error: action.last_error})}${["unknown", "sending", "prepared"].includes(action.state) ? `<button class="btn" data-reconcile="${esc(action.id)}" ${ctx.can_approve ? "" : "disabled"}>${esc(L("Read back mailbox to reconcile", "Đọc lại hộp thư để đối chiếu"))}</button>` : ""}</div>`).join(""));
  }
  function wireFeedback(h, ctx, guard) {
    if (document.getElementById("workflow-feedback")) wireForm("workflow-feedback", values => apiPost("/workflows/" + h.id + "/feedback", values), guard);
    if (document.getElementById("workflow-feedback-answer")) wireForm("workflow-feedback-answer", values => apiPost("/workflows/" + h.id + "/feedback/answer", {revision: h.control.feedback.revision, answers: values.answers}), guard);
    if (document.getElementById("workflow-feedback-confirm")) wireForm("workflow-feedback-confirm", async values => {
      if (values.answers?.trim()) return apiPost("/workflows/" + h.id + "/feedback/answer", {revision: h.control.feedback.revision, answers: values.answers});
      const result = await apiPost("/workflows/" + h.id + "/feedback/confirm", {revision: h.control.feedback.revision, answers: values.answers, new_revision: values.new_revision === "true"});
      if (guard.current()) go("#/processes/workflows/" + result.id);
    }, guard);
    document.querySelectorAll("[data-reconcile]").forEach(button => button.onclick = async () => {
      button.disabled = true;
      try { const result = await apiPost("/workflows/" + h.id + "/actions/" + button.dataset.reconcile + "/reconcile", {}); toast(result.state === "confirmed" || result.not_attempted ? L("Evidence reconciled. Resume the workflow when ready.", "Đã đối chiếu. Tiếp tục workflow khi sẵn sàng.") : L("Still uncertain; no message was resent.", "Kết quả chưa rõ; chưa gửi lại mail.")); if (guard.current()) route(); }
      catch (error) { toast(error.message, true); button.disabled = false; }
    });
  }
  return { render };
})();
