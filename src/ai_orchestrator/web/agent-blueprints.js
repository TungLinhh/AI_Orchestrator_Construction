/* Model-authored plans remain editable until Boss approves their exact revision. */
const AgentBlueprints = (() => {
  const {
    L,
    card,
    fields,
    link,
    input,
    area,
    select,
    form,
    body,
    wireForm,
    record,
    empty,
  } = Management;
  const lines = (value) =>
    String(value || "")
      .split("\n")
      .map((v) => v.trim())
      .filter(Boolean);
  const keys = (value) =>
    String(value || "")
      .split(/[\s,]+/)
      .filter(Boolean);
  const url = (id) => "#/processes/provision/" + encodeURIComponent(id);
  const workflowUrl = (id) =>
    "#/processes/provision/workflow/" + encodeURIComponent(id);
  async function render(r, guard) {
    const ctx = await apiGet("/console/context");
    if (r.section === "workflow" && r.focus)
      return renderWorkflow(r.focus, ctx, guard);
    if (r.section !== "overview") return renderDraft(r.section, ctx, guard);
    const [defs, agents, units, profiles, drafts] = await Promise.all([
      Management.catalogue("definitions"),
      apiGet("/agents", { limit: 200 }),
      apiGet("/org-units", { limit: 200 }),
      apiGet("/model-profiles"),
      apiGet("/agent-blueprints"),
    ]);
    const departments = [
      ["hr", "HR"],
      ["procurement", L("Procurement", "Mua sắm")],
      ["design", L("Design", "Thiết kế")],
      ["qa", "QA/QC-HSE"],
      ["finance", L("Finance", "Tài chính")],
      ["sales", "Sales"],
    ];
    body(
      card(
        L(
          "Design an agent with a free model",
          "Thiết kế agent bằng model free",
        ),
        `<p>${esc(
          L(
            "Describe the department's mandate. The model drafts responsibilities and a complete SOP workflow. Boss edits and approves before setup.",
            "Mô tả nhiệm vụ của phòng ban. Model soạn trách nhiệm và workflow theo toàn bộ SOP; Boss chỉnh sửa, duyệt trước khi thiết lập.",
          ),
        )}</p>` +
          form(
            "blueprint-draft",
            input(
              "name",
              L("Agent name", "Tên agent"),
              "",
              "text",
              "required maxlength=180",
            ) +
              select(
                "department",
                L("Department", "Phòng ban"),
                departments,
                "hr",
              ) +
              select(
                "definition_id",
                L("Role template", "Mẫu vai trò"),
                defs.items.map((d) => [d.id, d.name]),
              ) +
              select(
                "model_profile",
                L("Model profile", "Hồ sơ model"),
                profiles.items
                  .filter((p) =>
                    p.providers.some(
                      (c) =>
                        c.provider === "openrouter" &&
                        c.model.endsWith(":free") &&
                        c.supports_tools,
                    ),
                  )
                  .map((p) => [p.name, p.name]),
                "primary",
              ) +
              area(
                "mandate",
                L(
                  "Department mandate and desired outcomes",
                  "Nhiệm vụ phòng ban và kết quả mong muốn",
                ),
                "",
                "required minlength=10 maxlength=12000",
              ),
            L("Generate reviewable draft", "Soạn bản nháp để duyệt"),
            !ctx.can_administer,
            L(
              "Uses real free models only. No deployment before approval. IT is deferred.",
              "Chỉ dùng model free thật. Triển khai sau khi duyệt. Phòng IT tạm hoãn.",
            ),
          ),
      ) +
        card(
          L("Drafts and provisioned agents", "Bản nháp và agent đã thiết lập"),
          drafts.items.length
            ? `<div class="list">${drafts.items.map((d) => `<a class="row" href="${url(d.id)}"><span class="grow">${esc(d.title)}</span><span class="pill">${esc(statusName(d.status))}</span>${d.provisioned ? `<span>${esc(L("Provisioned", "Đã thiết lập"))}</span>` : ""}</a>`).join("")}</div>`
            : empty(),
        ),
      guard,
    );
    const dept = document.querySelector('#blueprint-draft [name="department"]');
    const template = document.querySelector(
      '#blueprint-draft [name="definition_id"]',
    );
    function chooseRole() {
      const unit = units.items.find((u) => u.slug === dept.value);
      const definitions = new Set(
        agents.items
          .filter(
            (a) =>
              a.org_unit_id === unit?.id && a.lifecycle_status === "active",
          )
          .map((a) => a.definition_id),
      );
      template.innerHTML = defs.items
        .filter((d) => definitions.has(d.id))
        .map((d) => `<option value="${esc(d.id)}">${esc(d.name)}</option>`)
        .join("");
    }
    dept.onchange = chooseRole;
    chooseRole();
    wireForm(
      "blueprint-draft",
      (v) => apiPost("/agent-blueprints", v),
      guard,
      (result) =>
        link(
          url(result.id),
          L("Edit and review this plan", "Chỉnh sửa và duyệt kế hoạch"),
        ) +
        `<p>${esc(result.error || L("The plan is ready for your review.", "Kế hoạch đã sẵn sàng để xem xét."))}</p>`,
    );
  }
  async function renderDraft(id, ctx, guard) {
    let data = await apiGet(`/agent-blueprints/${encodeURIComponent(id)}`);
    if (!data.output?.plan) {
      body(
        card(
          L("Draft preparation", "Soạn bản nháp"),
          record({ status: data.status, error: data.error }) +
            link("#/give/" + id, L("Execution log", "Nhật ký thực thi")),
        ),
        guard,
      );
      return;
    }
    let plan = JSON.parse(JSON.stringify(data.output.plan));
    const locked = !!data.output.provisioned || !ctx.can_administer;
    const textList = (value) => (Array.isArray(value) ? value.join("\n") : "");
    function editor() {
      body(
        card(
          data.config.name,
          fields([
            [L("Revision", "Phiên bản"), data.output.revision],
            [L("Department", "Phòng ban"), data.config.department],
            [
              L("Source SOPs", "SOP nguồn"),
              data.config.source_sops.map((s) => s.code + " · " + s.name),
            ],
            [
              L(
                "Published skills / read-only tools",
                "Skill đã phát hành / công cụ chỉ đọc",
              ),
              {
                skills: data.config.skills.length
                  ? L(
                      data.config.skills.length + " pinned published versions",
                      data.config.skills.length +
                        " phiên bản đã phát hành được ghim",
                    )
                  : L(
                      "No published pinned skills",
                      "Chưa có skill đã phát hành được ghim",
                    ),
                tools: data.config.tools.map((t) => t.name),
              },
            ],
          ]) +
            (data.approvals.find((review) => review.status === "pending")
              ? `<div class="btn-row">${link("#/approval/" + data.approvals.find((review) => review.status === "pending").id, L("Open current Boss review", "Mở bản duyệt hiện tại"))}</div>`
              : "") +
            (data.output.provisioned
              ? `<div class="note">${esc(L("Approved and provisioned. The workflow waits for its real input.", "Đã duyệt và thiết lập. Workflow đang chờ đầu vào thực tế."))}</div><div class="btn-row">${link("#/agent/" + data.output.provisioned.agent_id, L("Agent controls", "Điều khiển agent"))}${link(workflowUrl(data.output.provisioned.workflow_id), L("Open prepared workflow", "Mở workflow đã chuẩn bị"))}</div>`
              : ""),
        ) +
          card(
            L(
              "Source procedures and controls",
              "Quy trình nguồn và điểm kiểm soát",
            ),
            data.config.source_sops
              .map(
                (s) =>
                  `<details class="blueprint-step"><summary>${esc(s.code)} · ${esc(s.name)}</summary>${record(s)}</details>`,
              )
              .join(""),
          ) +
          card(
            L("Editable operating plan", "Kế hoạch vận hành có thể chỉnh sửa"),
            `<form id="blueprint-editor" class="manage-form">${area("description", L("Description", "Mô tả"), plan.description, "required")}
          ${area("system_instructions", L("Operating instructions", "Chỉ dẫn vận hành"), plan.system_instructions, "required")}
          ${area("responsibilities", L("Responsibilities · one per line", "Trách nhiệm · mỗi dòng một mục"), textList(plan.responsibilities), "required")}
          <div class="wide blueprint-steps">${Object.entries(
            plan.required_inputs,
          )
            .map(
              ([k, v], i) => `<div class="blueprint-step manage-form">
          ${input("source_key_" + i, L("Source input key", "Mã đầu vào nguồn"), k, "text", "required pattern=[a-z][a-z0-9_]{0,47}")}
          ${area("source_description_" + i, L("Source description", "Mô tả nguồn"), v, "required")}
          ${!locked ? `<div class="wide"><button type="button" class="btn sm danger" data-source-remove="${i}">${esc(L("Remove source", "Xóa nguồn"))}</button></div>` : ""}</div>`,
            )
            .join("")}</div>
          ${!locked ? `<div class="wide"><button type="button" class="btn" id="blueprint-source-add">${esc(L("Add source input", "Thêm đầu vào nguồn"))}</button></div>` : ""}
          <div class="wide blueprint-steps">${plan.steps
            .map(
              (
                s,
                i,
              ) => `<details class="blueprint-step" ${i === 0 ? "open" : ""}><summary>${i + 1}. ${esc(s.title)} ${s.human_review ? "· " + esc(L("Human review", "Người duyệt")) : ""}</summary><div class="manage-form">
          ${input("key_" + i, L("Step key", "Mã bước"), s.key, "text", "required pattern=[a-z][a-z0-9_]{0,47}")}
          ${input("title_" + i, L("Title", "Tên bước"), s.title, "text", "required maxlength=180")}
          ${area("instructions_" + i, L("Task instructions", "Chỉ dẫn tác vụ"), s.instructions, "required")}
          ${input("inputs_" + i, L("Input keys · sources or earlier outputs", "Mã đầu vào · nguồn hoặc đầu ra trước"), s.input_keys.join(", "), "text", "required")}
          ${input("outputs_" + i, L("Output fields", "Trường đầu ra"), s.output_fields.join(", "), "text", "required")}
          ${area("acceptance_" + i, L("Acceptance criteria · one per line", "Tiêu chí nghiệm thu · mỗi dòng một mục"), textList(s.acceptance_criteria), "required")}
          ${input("sources_" + i, L("SOP codes", "Mã SOP"), s.source_sop_codes.join(", "), "text", "required")}
          ${input("source_steps_" + i, L("Source step references", "Tham chiếu bước nguồn"), s.source_step_refs?.join(", ") || "", "text")}
          ${select(
            "review_" + i,
            L("Requires human review", "Cần người duyệt"),
            [
              ["true", L("Yes", "Có")],
              ["false", L("No", "Không")],
            ],
            String(s.human_review),
          )}
          ${!locked ? `<div class="wide btn-row"><button type="button" class="btn sm" data-step-up="${i}">${esc(L("Move up", "Chuyển lên"))}</button><button type="button" class="btn sm danger" data-step-remove="${i}">${esc(L("Remove step", "Xóa bước"))}</button></div>` : ""}</div></details>`,
            )
            .join("")}</div>
          ${!locked ? `<div class="wide btn-row"><button type="button" class="btn" id="blueprint-add">${esc(L("Add step", "Thêm bước"))}</button><button type="submit" class="btn primary">${esc(L("Save edits", "Lưu chỉnh sửa"))}</button><button type="button" class="btn" id="blueprint-submit">${esc(L("Save and request Boss review", "Lưu và yêu cầu Boss duyệt"))}</button></div>` : ""}
          <div class="wide form-result" role="status" hidden></div></form>`,
          ) +
          card(
            L("Approval history", "Lịch sử phê duyệt"),
            data.approvals
              .map(
                (review) =>
                  `<p>${link("#/approval/" + review.id, L("Open decision", "Mở quyết định"))} · ${esc(statusName(review.status))}</p>`,
              )
              .join("") || empty(),
          ),
        guard,
      );
      if (locked) {
        document
          .querySelectorAll(
            "#blueprint-editor input, #blueprint-editor textarea, #blueprint-editor select",
          )
          .forEach((el) => (el.disabled = true));
        return;
      }
      const el = document.getElementById("blueprint-editor");
      function capture() {
        const v = Object.fromEntries(new FormData(el).entries());
        const sourceEntries = Object.keys(plan.required_inputs).map((_, i) => [
          v["source_key_" + i],
          v["source_description_" + i],
        ]);
        if (
          new Set(sourceEntries.map(([key]) => key)).size !==
          sourceEntries.length
        ) {
          toast(
            L(
              "Source input keys must be unique.",
              "Mã đầu vào nguồn phải khác nhau.",
            ),
            true,
          );
          return false;
        }
        plan = {
          description: v.description,
          system_instructions: v.system_instructions,
          responsibilities: lines(v.responsibilities),
          required_inputs: Object.fromEntries(sourceEntries),
          steps: plan.steps.map((s, i) => ({
            key: v["key_" + i],
            title: v["title_" + i],
            instructions: v["instructions_" + i],
            input_keys: keys(v["inputs_" + i]),
            output_fields: keys(v["outputs_" + i]),
            acceptance_criteria: lines(v["acceptance_" + i]),
            source_sop_codes: keys(v["sources_" + i]),
            source_step_refs: keys(v["source_steps_" + i]),
            human_review: v["review_" + i] === "true",
          })),
        };
        return true;
      }
      async function save(submit) {
        if (!el.reportValidity()) return;
        if (!capture()) return;
        const out = el.querySelector(".form-result");
        out.hidden = false;
        out.textContent = L("Saving…", "Đang lưu…");
        const buttons = [...el.querySelectorAll("button")];
        buttons.forEach((b) => (b.disabled = true));
        try {
          data = await Management.patch("/agent-blueprints/" + id, {
            revision: data.output.revision,
            plan,
          });
          if (submit) {
            const a = await apiPost("/agent-blueprints/" + id + "/submit", {});
            scheduleBadgeRefresh();
            out.innerHTML = link(
              "#/approval/" + a.approval_id,
              L("Open Boss review", "Mở yêu cầu Boss duyệt"),
            );
          } else {
            out.textContent =
              L("Saved revision ", "Đã lưu phiên bản ") + data.output.revision;
          }
        } catch (e) {
          out.textContent = e.message;
          out.className = "wide form-result failure";
        } finally {
          buttons.forEach((b) => (b.disabled = false));
        }
      }
      el.onsubmit = (e) => {
        e.preventDefault();
        save(false);
      };
      document.getElementById("blueprint-submit").onclick = () => save(true);
      document.getElementById("blueprint-source-add").onclick = () => {
        if (!capture()) return;
        let n = Object.keys(plan.required_inputs).length + 1;
        while (Object.hasOwn(plan.required_inputs, "source_" + n)) n++;
        plan.required_inputs["source_" + n] = "";
        editor();
      };
      el.querySelectorAll("[data-source-remove]").forEach(
        (b) =>
          (b.onclick = () => {
            if (!capture()) return;
            delete plan.required_inputs[
              Object.keys(plan.required_inputs)[Number(b.dataset.sourceRemove)]
            ];
            editor();
          }),
      );
      document.getElementById("blueprint-add").onclick = () => {
        if (!capture()) return;
        plan.steps.push({
          key: "step_" + (plan.steps.length + 1),
          title: L("New step", "Bước mới"),
          instructions: "",
          input_keys: [Object.keys(plan.required_inputs)[0]],
          output_fields: ["report"],
          acceptance_criteria: [""],
          source_sop_codes: [data.config.source_sops[0].code],
          human_review: true,
        });
        editor();
      };
      el.querySelectorAll("[data-step-up]").forEach(
        (b) =>
          (b.onclick = () => {
            if (!capture()) return;
            const i = Number(b.dataset.stepUp);
            if (i) {
              [plan.steps[i - 1], plan.steps[i]] = [
                plan.steps[i],
                plan.steps[i - 1],
              ];
            }
            editor();
          }),
      );
      el.querySelectorAll("[data-step-remove]").forEach(
        (b) =>
          (b.onclick = () => {
            if (!capture()) return;
            plan.steps.splice(Number(b.dataset.stepRemove), 1);
            editor();
          }),
      );
    }
    editor();
  }
  async function renderWorkflow(id, ctx, guard) {
    const d = await apiGet(
      `/agent-blueprints/workflows/${encodeURIComponent(id)}`,
    );
    const missingInputs = d.missing_inputs.length > 0;
    body(
      card(
        L("Prepared agent workflow", "Workflow agent đã chuẩn bị"),
        fields([
          [L("State", "Trạng thái"), statusName(d.status)],
          [L("Interruption or failure", "Gián đoạn hoặc lỗi"), d.error || "—"],
          [L("Current missing inputs", "Đầu vào đang thiếu"), d.missing_inputs],
        ]) +
          `<p>${esc(L("This operating plan prepares document drafts for human review. Use business workflows for MEP email intake, CV scoring and procurement evidence.", "Kế hoạch vận hành này soạn hồ sơ dự thảo để người phụ trách duyệt. Workflow nghiệp vụ xử lý nhận CV MEP qua mail, chấm CV và chứng cứ procurement."))} ${link("#/processes/workflows", L("Open business workflows", "Mở workflow nghiệp vụ"))}</p>` +
          `<div class="btn-row"><button class="btn" id="blueprint-refresh">${esc(L("Refresh status", "Cập nhật trạng thái"))}</button>${link("#/agent/" + d.owner_agent_id, L("Agent controls", "Điều khiển agent"))}${link("#/give/" + id + "/log", L("Execution log", "Nhật ký thực thi"))}<button class="btn" id="blueprint-run" ${!ctx.can_administer || (d.status === "waiting_for_input" && missingInputs) || ["completed", "failed", "canceled", "expired"].includes(d.status) ? "disabled" : ""}>${esc(L("Run / resume workflow", "Chạy / tiếp tục workflow"))}</button><button class="btn danger" id="blueprint-cancel" ${!ctx.can_administer || ["completed", "failed", "canceled", "expired"].includes(d.status) ? "disabled" : ""}>${esc(L("Stop workflow", "Dừng workflow"))}</button></div>`,
      ) +
        card(
          L("Source inputs", "Đầu vào nguồn"),
          `<p>${esc(L("Supply sources when each step needs them. Earlier sources stay fixed after execution begins.", "Bổ sung dữ liệu nguồn khi đến bước cần dùng. Dữ liệu đã cung cấp được giữ cố định sau khi chạy."))}</p>` +
            form(
              "blueprint-inputs",
              Object.entries(d.plan.required_inputs)
                .map(([k, v]) =>
                  area(
                    k,
                    v,
                    d.inputs[k] || "",
                    `maxlength=100000 ${d.inputs_locked && d.inputs[k] ? "readonly" : ""}`,
                  ),
                )
                .join(""),
              L("Save source inputs", "Lưu đầu vào nguồn"),
              !ctx.can_administer || d.status !== "waiting_for_input",
            ),
        ) +
        card(
          L("All tasks and review gates", "Toàn bộ task và cổng duyệt"),
          `<div class="list">${d.steps.map((s) => `<details><summary>${esc(s.title)} · <span class="pill">${esc(statusName(s.status))}</span></summary>${record(s.output)}${s.error ? `<p class="note late">${esc(s.error)}</p>` : ""}<div class="btn-row">${link("#/give/" + s.id, L("Open task and log", "Mở task và nhật ký"))}${s.status === "waiting_for_approval" ? link("#/give/" + s.id + "/decisions", L("Open this review", "Mở quyết định của bước này")) : ""}</div></details>`).join("")}</div>`,
        ),
      guard,
    );
    document.getElementById("blueprint-refresh").onclick = () => route();
    wireForm(
      "blueprint-inputs",
      (v) =>
        apiPost("/agent-blueprints/workflows/" + id + "/inputs", {
          inputs: Object.fromEntries(
            Object.entries(v).filter(([, value]) => String(value).trim()),
          ),
        }),
      guard,
      () => {
        route();
        return esc(L("Source inputs saved.", "Đã lưu đầu vào nguồn."));
      },
    );
    document.getElementById("blueprint-cancel").onclick = async () => {
      try {
        await apiPost("/tasks/" + id + "/cancel", {});
        toast(L("Workflow stopped.", "Đã dừng workflow."));
        route();
      } catch (e) {
        toast(e.message, true);
      }
    };
    document.getElementById("blueprint-run").onclick = async () => {
      try {
        await apiPost("/agent-blueprints/workflows/" + id + "/run", {});
        toast(
          L(
            "Workflow started; it pauses at each human review.",
            "Workflow đã chạy; sẽ dừng ở mỗi bước cần người duyệt.",
          ),
        );
        route();
      } catch (e) {
        toast(e.message, true);
      }
    };
  }
  return { render };
})();
