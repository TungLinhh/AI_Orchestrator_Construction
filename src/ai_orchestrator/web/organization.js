/* Agent controls and process definitions extend the hierarchy, not a second roster. */
(() => {
  const {
    host,
    L,
    record,
    empty,
    badge,
    link,
    card,
    details,
    fields,
    input,
    area,
    select,
    form,
    adminNote,
    page,
    paint,
    body,
    table,
    pager,
    searchRows,
    search,
    wireForm,
    patch,
    numberOrNull,
    catalogue,
    recordId,
  } = Management;
  MANAGEMENT_VIEWS.set("agent", async (r, guard) => {
    if (!r.arg) {
      go("#/departments");
      return;
    }
    paint(
      page(
        L("Agent controls", "Điều khiển agent"),
        L(
          "Inspect authority, resources and health before changing operation.",
          "Kiểm tra quyền hạn, tài nguyên và sức khỏe trước khi điều chỉnh hoạt động.",
        ),
      ),
      guard,
    );
    const [a, c, caps, h, ctx, profiles] = await Promise.all([
      apiGet(`/agents/${r.arg}`),
      apiGet(`/agents/${r.arg}/control`),
      apiGet(`/agents/${r.arg}/capabilities`),
      apiGet(`/agents/${r.arg}/health`),
      apiGet("/console/context"),
      apiGet("/model-profiles"),
    ]);
    if (!guard.current()) return;
    const labels = {
      activate: L("Activate", "Kích hoạt"),
      pause: L("Pause", "Tạm dừng"),
      resume: L("Resume", "Tiếp tục"),
      suspend: L("Suspend", "Đình chỉ"),
      retire: L("Retire", "Ngừng sử dụng"),
    };
    const transitions = (ctx.agent_transitions[a.lifecycle_status] || [])
      .filter((s) => labels[s])
      .map(
        (action) =>
          `<button class="btn sm" data-life="${action}" ${ctx.can_administer && !c.kill_switch ? "" : "disabled"}>${esc(labels[action])}</button>`,
      )
      .join("");
    body(
      `<div class="card identity-card"><h2>${esc(a.name)}</h2><code>${esc(a.id)}</code><p>${esc(a.description)}</p><div class="btn-row">${link("#/dept/" + encodeURIComponent(a.id), L("Work in this unit", "Công việc của đơn vị"))}${a.parent_agent_id ? link("#/agent/" + encodeURIComponent(a.parent_agent_id), L("Parent agent", "Agent cấp trên")) : ""}</div></div><div class="manage-grid">` +
        card(
          L("State and authority", "Trạng thái và quyền hạn"),
          fields([
            [L("Lifecycle", "Vòng đời"), a.lifecycle_status],
            [L("Runtime", "Thực thi"), a.runtime_status],
            [L("Health", "Sức khỏe"), h.health],
            [
              L("Granted / ceiling", "Quyền cấp / trần"),
              `${c.granted_level} / ${c.autonomy_ceiling}`,
            ],
            [
              L("Active / queued", "Đang chạy / chờ"),
              `${h.active_tasks} / ${h.queue_depth}`,
            ],
            [
              L("Heartbeat", "Nhịp hoạt động"),
              h.heartbeat_stale
                ? L("Stale or absent", "Cũ hoặc chưa ghi nhận")
                : `${h.heartbeat_age_s}s`,
            ],
          ]) +
            `<div class="btn-row">${transitions}</div><p class="form-notice">${esc(adminNote(ctx))}</p>`,
        ) +
        card(
          L("Emergency stop", "Dừng khẩn cấp"),
          c.kill_switch
            ? `<p class="error">${esc(c.kill_reason)}</p>` +
                form(
                  "agent-stop",
                  "",
                  L("Clear stop switch at L1", "Gỡ công tắc dừng ở L1"),
                  false,
                  L(
                    "Lifecycle stays suspended. Resume is a separate action.",
                    "Vòng đời vẫn đình chỉ. Tiếp tục hoạt động là thao tác riêng.",
                  ),
                )
            : form(
                "agent-stop",
                area(
                  "reason",
                  L("Reason for stopping", "Lý do dừng"),
                  "",
                  'required minlength="3" maxlength="2000"',
                ),
                L("Stop this agent", "Dừng agent này"),
                false,
                L(
                  "Stops further execution and records the reason in the decision log.",
                  "Dừng thực thi tiếp và ghi lý do vào nhật ký quyết định.",
                ),
              ),
        ) +
        "</div>" +
        card(
          L("Operational configuration", "Cấu hình vận hành"),
          form(
            "agent-config",
            select(
              "model_profile",
              L("Model profile", "Hồ sơ model"),
              profiles.items.map((p) => [p.name, p.name]),
              a.model_profile,
            ) +
              input(
                "budget_limit_usd",
                L("Budget limit (USD)", "Giới hạn ngân sách (USD)"),
                a.budget_limit_usd,
                "number",
                'min="0" step="0.01"',
              ) +
              input(
                "budget_limit_tokens",
                L("Token limit", "Giới hạn token"),
                a.budget_limit_tokens,
                "number",
                'min="0" step="1"',
              ) +
              area(
                "description",
                L("Description", "Mô tả"),
                a.description || "",
              ),
            L("Save configuration", "Lưu cấu hình"),
            !ctx.can_administer,
            adminNote(ctx),
          ),
        ) +
        card(
          L("Bound tools", "Công cụ được cấp"),
          table(
            [
              L("Tool", "Công cụ"),
              L("Tool risk", "Rủi ro công cụ"),
              L("Binding ceiling", "Trần được cấp"),
              L("Approval", "Phê duyệt"),
            ],
            (caps.tools || []).map((t) => [
              link("#/library/tools/" + t.id, t.name),
              badge(t.risk_level),
              badge(t.binding_max_risk),
              esc(
                t.requires_approval
                  ? L("Required", "Bắt buộc")
                  : L("No", "Không"),
              ),
            ]),
          ),
        ) +
        card(
          L("Bound skills", "Kỹ năng được cấp"),
          (caps.skills || [])
            .map(
              (s) =>
                `<p>${link("#/library/skills/" + s.id, s.name)} ${badge(s.governance_state)}</p>`,
            )
            .join("") || empty(),
        ) +
        card(
          L("Declared capabilities", "Năng lực khai báo"),
          record(caps.declared),
        ) +
        `<div class="btn-row">${link("#/operations/decisions/" + a.id, L("Decision history", "Lịch sử quyết định"))}${link("#/operations/usage/" + a.id, L("Model usage", "Lịch sử dùng model"))}${link("#/processes/definitions/" + a.definition_id, L("Agent definition", "Định nghĩa agent"))}</div>`,
      guard,
    );
    wireForm(
      "agent-config",
      (v) =>
        patch("/agents/" + r.arg, {
          ...v,
          budget_limit_usd: numberOrNull(v.budget_limit_usd),
          budget_limit_tokens: numberOrNull(v.budget_limit_tokens),
        }),
      guard,
    );
    wireForm(
      "agent-stop",
      async (v) => {
        const result = await apiPost(
          "/agents/" + r.arg + (c.kill_switch ? "/revive" : "/kill"),
          v,
        );
        await route();
        return result;
      },
      guard,
    );
    if (ctx.can_administer) {
      const [skills, tools] = await Promise.all([
        apiGet("/skills", { limit: 200 }),
        apiGet("/tools", { limit: 200 }),
      ]);
      if (!guard.current()) return;
      const grant = document.createElement("div");
      grant.innerHTML = card(
        L("Grant a capability", "Cấp năng lực"),
        form(
          "agent-grant-skill",
          select(
            "skill_id",
            L("Skill", "Kỹ năng"),
            skills.items
              .filter((s) => s.is_published)
              .map((s) => [s.id, `${s.name} · ${s.current_version}`]),
          ),
          L("Bind published skill", "Gắn kỹ năng đã xuất bản"),
          !skills.items.some((s) => s.is_published),
          L(
            "A binding pins the published version selected at grant time.",
            "Quyền cấp gắn với phiên bản đã xuất bản được chọn tại thời điểm cấp.",
          ),
        ) +
          form(
            "agent-grant-tool",
            select(
              "tool_id",
              L("Tool", "Công cụ"),
              tools.items.map((t) => [t.id, t.name]),
            ) +
              select(
                "max_risk",
                L("Binding risk ceiling", "Trần rủi ro được cấp"),
                [
                  "read_only",
                  "low_risk_write",
                  "external_side_effect",
                  "privileged",
                  "destructive",
                ].map((s) => [s, s]),
                "read_only",
              ),
            L("Grant tool within ceiling", "Cấp công cụ trong giới hạn"),
            !tools.items.length,
            L(
              "The server refuses a ceiling wider than the tool permits.",
              "Server từ chối trần quyền rộng hơn mức công cụ cho phép.",
            ),
          ),
      );
      document.getElementById("managementBody").appendChild(grant);
      wireForm(
        "agent-grant-skill",
        (v) => {
          const selected = skills.items.find((s) => s.id === v.skill_id);
          return apiPost(`/agents/${r.arg}/skills`, {
            ...v,
            skill_version_id: selected.version_id,
          });
        },
        guard,
      );
      wireForm(
        "agent-grant-tool",
        (v) => apiPost(`/agents/${r.arg}/tools`, v),
        guard,
      );
    }
    host()
      .querySelectorAll("[data-life]")
      .forEach((button) => {
        button.onclick = async () => {
          button.disabled = true;
          try {
            await apiPost("/agents/" + r.arg + "/" + button.dataset.life, {});
            await route();
          } catch (err) {
            toast(err.message, true);
            button.disabled = false;
          }
        };
      });
  });
  MANAGEMENT_VIEWS.set("processes", async (r, guard) => {
    if (r.arg === "workflows") {
      await BusinessWorkflows.render(r, guard);
      return;
    }
    const active = r.arg || "catalogue",
      tabs = [
        [
          "workflows",
          L("Business workflows", "Workflow nghiệp vụ"),
          "#/processes/workflows",
        ],
        [
          "catalogue",
          L("Procedure catalogue", "Danh mục quy trình"),
          "#/processes/catalogue",
        ],
        [
          "hiring",
          L("Recruitment example", "Quy trình tuyển dụng mẫu"),
          "#/processes/hiring",
        ],
        [
          "definitions",
          L("Agent definitions", "Định nghĩa agent"),
          "#/processes/definitions",
        ],
        [
          "provision",
          L("Provision agent", "Tạo agent"),
          "#/processes/provision",
        ],
      ];
    paint(
      page(
        L("Processes", "Quy trình"),
        L(
          "Read responsibilities, gates and the definition followed by an agent.",
          "Đọc trách nhiệm, các cổng kiểm soát và định nghĩa agent đang tuân theo.",
        ),
        tabs,
        active,
      ),
      guard,
    );
    if (active === "provision") {
      const [ctx, defs, units, profiles, agents] = await Promise.all([
        apiGet("/console/context"),
        catalogue("definitions"),
        apiGet("/org-units", { limit: 200 }),
        apiGet("/model-profiles"),
        apiGet("/agents", { limit: 200 }),
      ]);
      body(
        card(
          L("Provision a draft agent", "Tạo agent ở trạng thái nháp"),
          form(
            "agent-provision",
            input(
              "name",
              L("Agent name", "Tên agent"),
              "",
              "text",
              "required maxlength=255",
            ) +
              select(
                "definition_id",
                L("Definition", "Định nghĩa"),
                defs.items.map((d) => [d.id, d.name]),
              ) +
              select(
                "org_unit_id",
                L("Organizational unit", "Đơn vị tổ chức"),
                [
                  ["", L("Unassigned", "Chưa gán")],
                  ...units.items.map((u) => [u.id, u.name]),
                ],
              ) +
              select("parent_agent_id", L("Parent agent", "Agent cấp trên"), [
                ["", L("No parent", "Không có cấp trên")],
                ...agents.items.map((a) => [a.id, a.name]),
              ]) +
              select(
                "model_profile",
                L("Model profile", "Hồ sơ model"),
                profiles.items.map((p) => [p.name, p.name]),
                "primary",
              ) +
              area("description", L("Description", "Mô tả")),
            L("Create draft", "Tạo nháp"),
            !ctx.can_administer || !defs.items.length,
            L(
              "The role comes from its definition. Creation does not activate the agent; review its controls before activation.",
              "Vai trò lấy từ định nghĩa. Việc tạo không kích hoạt agent; kiểm tra hồ sơ điều khiển trước khi kích hoạt.",
            ) +
              " " +
              adminNote(ctx),
          ),
        ),
        guard,
      );
      wireForm(
        "agent-provision",
        (v) => {
          const d = defs.items.find((d) => d.id === v.definition_id);
          return apiPost("/agents", {
            ...v,
            role_id: d.role_id,
            org_unit_id: v.org_unit_id || null,
            parent_agent_id: v.parent_agent_id || null,
          });
        },
        guard,
        (result) =>
          link(
            "#/agent/" + result.id,
            L("Open new agent controls", "Mở điều khiển agent vừa tạo"),
          ) + record(result),
      );
      return;
    }
    if (active === "hiring") {
      const h = await apiGet("/hiring");
      body(
        card(
          h.sop_title || h.sop_code,
          `<p>${esc(h.position)} · ${esc(h.office)} / ${esc(h.department)}</p><p>${esc(L("This is the seeded recruitment example, not all company recruitment.", "Đây là quy trình tuyển dụng mẫu đã được tạo, không phải toàn bộ tuyển dụng của công ty."))}</p>` +
            fields([
              [L("Procedure", "Quy trình"), h.sop_code],
              [L("Headcount", "Số lượng tuyển"), h.headcount],
              [L("JD reference", "Tham chiếu JD"), h.jd_reference],
              [
                L("Finished / recorded stages", "Bước đã xong / đã ghi nhận"),
                `${h.done_count} / ${h.stage_count}`,
              ],
              [
                L("Current stage", "Bước hiện tại"),
                h.current
                  ? `${h.current.n}. ${h.current.name_vi} · ${statusName(h.current.task_status)}`
                  : L(
                      "No example tasks have been created for this organization.",
                      "Chưa tạo task mẫu trong tổ chức này.",
                    ),
              ],
            ]) +
            details(
              L(
                "JD sections and scoring rubric",
                "Các phần của JD và rubric chấm điểm",
              ),
              { jd_sections: h.jd_sections, rubric: h.rubric },
            ) +
            table(
              [
                L("Stage", "Bước"),
                L("Owner", "Phụ trách"),
                L("Task", "Công việc"),
                L("Gate", "Cổng duyệt"),
              ],
              (h.stages || []).map((s) => [
                esc(`${s.n}. ${s.name_vi}`),
                esc(s.department),
                link(
                  "#/give/" +
                    s.task_id +
                    (s.waiting_on_you ? "/decisions" : "/overview"),
                  statusName(s.task_status),
                ),
                esc(s.approval_question || s.question || "—"),
              ]),
            ),
        ),
        guard,
      );
      return;
    }
    const kind = active === "definitions" ? "definitions" : "procedures",
      id = recordId(r),
      d = await catalogue(kind, id);
    if (id) {
      const e = d.items[0];
      body(
        e
          ? card(
              e.name || e.name_vi,
              fields(
                Object.entries(e).filter(
                  ([k, v]) =>
                    !Array.isArray(v) &&
                    typeof v !== "object" &&
                    k !== "system_instructions",
                ),
              ) +
                (e.system_instructions
                  ? `<h3>${esc(L("Instructions", "Chỉ dẫn"))}</h3><pre class="readable-text">${esc(e.system_instructions)}</pre>`
                  : "") +
                details(L("Full definition", "Định nghĩa đầy đủ"), e),
            ) +
              link(
                "#/processes/" + active,
                L("Back to catalogue", "Về danh mục"),
              )
          : empty(
              L(
                "This record does not exist in this organization.",
                "Bản ghi không tồn tại trong tổ chức này.",
              ),
            ),
        guard,
      );
      return;
    }
    const extra =
      active === "definitions"
        ? await Promise.all([
            apiGet("/console/context"),
            apiGet("/roles"),
            apiGet("/model-profiles"),
          ])
        : null;
    body(
      card(
        L("Catalogue", "Danh mục"),
        search() +
          table(
            [
              L("Name", "Tên"),
              L("Code / version", "Mã / phiên bản"),
              L("Owner / profile", "Phụ trách / hồ sơ"),
            ],
            d.items.map((s) => [
              link(`#/processes/${active}/${s.id}`, s.name || s.name_vi),
              esc(s.code || s.version),
              esc(s.owner_role_key || s.model_profile),
            ]),
          ) +
          pager(d, "#/processes/" + active),
      ) +
        (extra
          ? card(
              L("Author a definition", "Tạo định nghĩa"),
              `<details><summary>${esc(L("New agent definition", "Định nghĩa agent mới"))}</summary>` +
                form(
                  "definition-create",
                  input("name", L("Name", "Tên"), "", "text", "required") +
                    select(
                      "role_id",
                      L("Role", "Vai trò"),
                      extra[1].items.map((r) => [r.id, r.name]),
                    ) +
                    select(
                      "model_profile",
                      L("Model profile", "Hồ sơ model"),
                      extra[2].items.map((p) => [p.name, p.name]),
                      "primary",
                    ) +
                    area(
                      "system_instructions",
                      L("Instructions", "Chỉ dẫn"),
                      "",
                      "required",
                    ) +
                    input(
                      "task_types",
                      L(
                        "Allowed task types (comma separated)",
                        "Loại công việc được phép (cách nhau bằng dấu phẩy)",
                      ),
                    ),
                  L("Create definition", "Tạo định nghĩa"),
                  !extra[0].can_administer,
                  adminNote(extra[0]),
                ) +
                "</details>",
            )
          : ""),
      guard,
    );
    searchRows();
    wireForm(
      "definition-create",
      (v) => {
        const { task_types, ...values } = v;
        return apiPost("/agents/definitions", {
          ...values,
          allowed_task_types: task_types
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean),
        });
      },
      guard,
    );
  });
})();
