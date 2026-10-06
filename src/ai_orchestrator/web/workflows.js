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
    let refreshTimer = null;
    async function refresh() {
      if (refreshTimer) clearTimeout(refreshTimer);
      const h = await apiGet(`/workflows/${encodeURIComponent(selectedId)}`);
      if (!guard.current()) return;
      const canRun = !["completed", "failed", "canceled", "expired"].includes(
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
      body(
        `<div class="btn-row">${link("#/processes/workflows", L("All workflows", "Tất cả workflow"))}${link("#/give/" + h.id, L("Open task tree and events", "Mở cây task và sự kiện"))}${link("#/work/approvals", L("Human approval inbox", "Hộp thư cần người duyệt"))}<button class="btn" id="workflow-run" ${ctx.can_administer && canRun ? "" : "disabled"}>${esc(L("Run / resume workflow", "Chạy / tiếp tục workflow"))}</button><button class="btn sm" id="workflow-refresh">${esc(L("Refresh evidence", "Cập nhật chứng cứ"))}</button></div>` +
          card(
            h.title,
            fields([
              [L("Exact run", "Lượt chính xác"), h.id],
              [L("Mode", "Chế độ"), h.mode],
              [L("State", "Trạng thái"), h.status],
              [L("Failure reason", "Lý do lỗi"), h.error || "—"],
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
                  `<div class="card"><h3>${n + 1}. ${esc(s.title)} ${badge(s.status)}</h3><div class="btn-row">${link("#/give/" + s.id, L("Task and execution evidence", "Task và chứng cứ thực thi"))}${s.owner_agent_id ? link("#/agent/" + s.owner_agent_id, L("Responsible agent", "Agent phụ trách")) : ""}${s.reused_from ? link("#/give/" + s.reused_from, L("Verified artifact reused from this task", "Sản phẩm đã kiểm chứng được dùng lại từ task này")) : ""}</div>${s.error ? `<p class="error">${esc(s.error)}</p>` : ""}${details(L("Produced artifact", "Sản phẩm đã tạo"), s.output)}</div>`,
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
      if (evidenceStage && ctx.can_approve) {
        const container = document.createElement("div");
        container.innerHTML = card(
          L("Supply the missing evidence", "Bổ sung chứng cứ đang chờ"),
          form(
            "workflow-evidence",
            area(
              "evidence",
              L("Source record (JSON)", "Hồ sơ nguồn (JSON)"),
              "",
              "required",
            ),
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
              evidence: JSON.parse(values.evidence),
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
      if (["running", "assigned"].includes(h.status))
        refreshTimer = setTimeout(() => {
          if (guard.current())
            refresh().catch((err) => toast(err.message, true));
        }, 10000);
    }
    await refresh();
  }
  return { render };
})();
