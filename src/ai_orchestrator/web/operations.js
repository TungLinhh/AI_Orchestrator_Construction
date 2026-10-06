/* Recorded routing and events remain distinct from current configuration. */
(() => {
  const {
    L,
    record,
    empty,
    badge,
    link,
    card,
    fields,
    area,
    select,
    form,
    page,
    paint,
    body,
    table,
    offset,
    pager,
    searchRows,
    search,
    wireForm,
    classifications,
    catalogue,
  } = Management;
  MANAGEMENT_VIEWS.set("operations", async (r, guard) => {
    const active = r.arg || "models",
      tabs = [
        ["models", L("Models", "Model"), "#/operations/models"],
        ["usage", L("Usage", "Sử dụng"), "#/operations/usage"],
        ["events", L("Event log", "Nhật ký sự kiện"), "#/operations/events"],
        ["decisions", L("Decisions", "Quyết định"), "#/operations/decisions"],
        ["audit", L("Audit", "Kiểm toán"), "#/operations/audit"],
        ["governance", L("Governance", "Kiểm soát"), "#/operations/governance"],
        ["system", L("System", "Hệ thống"), "#/operations/system"],
      ];
    paint(
      page(
        L("Operations", "Vận hành"),
        L(
          "Trace model routing, decisions and system health using recorded evidence.",
          "Theo dõi định tuyến model, quyết định và sức khỏe hệ thống bằng bằng chứng đã ghi nhận.",
        ),
        tabs,
        active,
      ),
      guard,
    );
    if (active === "models") {
      const p = await apiGet("/model-profiles");
      body(
        p.items
          .map((profile) =>
            card(
              profile.name,
              `<p>${esc(profile.description)}</p><p>${esc(L("Maximum classification", "Bảo mật tối đa"))}: ${badge(profile.max_classification)} · ${esc(L("Fallback", "Dự phòng"))}: ${esc(profile.fallback_profile || "—")}</p>` +
                table(
                  [
                    L("Provider", "Nhà cung cấp"),
                    "Model",
                    L("Weight", "Trọng số"),
                    L("Tools", "Công cụ"),
                  ],
                  profile.providers.map((c) => [
                    esc(c.provider),
                    esc(c.model),
                    esc(c.weight),
                    esc(c.supports_tools ? L("Yes", "Có") : L("No", "Không")),
                  ]),
                ),
            ),
          )
          .join("") +
          (p.warnings?.length
            ? card(
                L("Configuration warnings", "Cảnh báo cấu hình"),
                record(p.warnings),
              )
            : "") +
          card(
            L("Check model routing", "Kiểm tra định tuyến model"),
            form(
              "model-check",
              select(
                "profile",
                L("Profile", "Hồ sơ"),
                p.items.map((p) => [p.name, p.name]),
                "primary",
              ) +
                select(
                  "data_classification",
                  L("Classification", "Bảo mật"),
                  classifications,
                  "public",
                ) +
                area(
                  "prompt",
                  L("Test prompt", "Nội dung thử"),
                  L(
                    "Reply with one short greeting.",
                    "Trả lời bằng một lời chào ngắn.",
                  ),
                  "required maxlength=100000",
                ),
              L("Send test call", "Gửi lệnh gọi thử"),
              false,
              L(
                "Uses the same tenant profiles as tasks. A real call consumes provider quota; errors and fallback routing appear in the result.",
                "Dùng cùng hồ sơ tổ chức với task. Lệnh gọi thật sử dụng hạn mức nhà cung cấp; lỗi và định tuyến dự phòng hiển thị trong kết quả.",
              ),
            ),
          ),
        guard,
      );
      wireForm(
        "model-check",
        (v) => apiPost("/model/call", { ...v, max_output_tokens: 256 }),
        guard,
      );
      return;
    }
    if (active === "audit") {
      const d = await apiGet("/audit", { limit: 50, offset: offset() });
      body(
        card(
          L("Append-only audit trail", "Nhật ký kiểm toán chỉ ghi thêm"),
          search() +
            table(
              [
                L("Sequence / time", "Thứ tự / thời gian"),
                L("Actor", "Tác nhân"),
                L("Action", "Hành động"),
                L("Resource / outcome", "Đối tượng / kết quả"),
              ],
              d.items.map((x) => [
                esc(`${x.sequence} · ${x.at}`),
                esc(`${x.actor_type} · ${x.actor_id}`),
                esc(x.action),
                `<details><summary>${esc(x.resource_type)} · ${esc(x.outcome)}</summary>${x.task_id ? link("#/give/" + x.task_id + "/log", x.task_id) : esc(x.resource_id)}${record(x)}</details>`,
              ]),
            ) +
            pager(d, "#/operations/audit"),
        ),
        guard,
      );
      searchRows();
      return;
    }
    if (active === "usage") {
      const agentId =
          r.section !== "overview" && r.section !== "page" ? r.section : null,
        d = await apiGet("/model/usage", {
          limit: 50,
          offset: offset(),
          agent_id: agentId,
        });
      body(
        card(
          L("Recorded model calls", "Lệnh gọi model đã ghi nhận"),
          table(
            [
              L("Time", "Thời gian"),
              "Model",
              L("Task", "Công việc"),
              L("Routing", "Định tuyến"),
              L("Tokens / USD", "Token / USD"),
            ],
            d.items.map((x) => [
              esc(x.created_at),
              esc(`${x.provider}/${x.model_used}`),
              x.task_id
                ? link("#/give/" + x.task_id + "/steps", x.task_id)
                : "—",
              badge(x.routing_reason || x.status),
              esc(
                `${x.input_tokens + x.output_tokens + x.reasoning_tokens} / ${x.cost_usd}`,
              ),
            ]),
          ) + pager(d, "#/operations/usage" + (agentId ? "/" + agentId : "")),
        ),
        guard,
      );
      return;
    }
    if (active === "events") {
      const id = r.section.startsWith("evt_") ? r.section : null;
      if (id) {
        const e = await apiGet(`/events/${id}`);
        body(
          card(
            e.type,
            fields([
              ["ID", e.id],
              [L("Time", "Thời gian"), e.occurred_at],
              [L("Actor", "Tác nhân"), e.actor_id],
              [L("Subject", "Đối tượng"), e.subject],
            ]) + record(e.data),
          ) +
            link(
              "#/operations/events",
              L("Back to event log", "Về nhật ký sự kiện"),
            ),
          guard,
        );
        return;
      }
      const d = await apiGet("/events", { limit: 50, offset: offset() });
      body(
        card(
          L("Durable event log", "Nhật ký sự kiện bền vững"),
          search() +
            table(
              [
                L("Time", "Thời gian"),
                L("Event", "Sự kiện"),
                L("Subject", "Đối tượng"),
              ],
              d.items.map((x) => [
                esc(x.occurred_at),
                link("#/operations/events/" + x.id, x.type),
                esc(x.subject),
              ]),
            ) +
            pager(d, "#/operations/events"),
        ),
        guard,
      );
      searchRows();
      return;
    }
    if (active === "decisions") {
      const agentId =
          r.section !== "overview" && r.section !== "page" ? r.section : null,
        d = await apiGet("/ai-decisions", {
          limit: 50,
          offset: offset(),
          agent_id: agentId,
        });
      body(
        card(
          L("Decision history", "Lịch sử quyết định"),
          table(
            [
              L("Time", "Thời gian"),
              "Agent",
              L("Decision", "Quyết định"),
              L("Reason / outcome", "Lý do / kết quả"),
            ],
            d.items.map((x) => [
              esc(x.occurred_at),
              x.agent_id
                ? link("#/agent/" + x.agent_id, x.agent_name || x.agent_id)
                : esc(x.actor_type),
              esc(x.decision),
              `<details><summary>${esc(x.outcome || x.rationale)}</summary><p>${esc(x.rationale)}</p>${x.task_id ? link("#/give/" + x.task_id + "/decisions", L("Task decisions", "Quyết định của công việc")) : ""}${record(x)}</details>`,
            ]),
          ) +
            pager(d, "#/operations/decisions" + (agentId ? "/" + agentId : "")),
        ),
        guard,
      );
      return;
    }
    if (active === "governance") {
      const [summary, spend, policies] = await Promise.all([
        apiGet("/governance/summary"),
        apiGet("/governance/spend", { limit: 200 }),
        catalogue("policies"),
      ]);
      const names = {
        agents_with_elevated_autonomy: L(
          "Agents with elevated autonomy",
          "Agent có quyền tự chủ cao",
        ),
        privileged_tool_bindings: L(
          "Privileged tool grants",
          "Quyền công cụ đặc biệt",
        ),
        pending_approvals: L("Pending approvals", "Chờ phê duyệt"),
        tasks_waiting_for_approval: L(
          "Tasks awaiting approval",
          "Công việc chờ duyệt",
        ),
        blocked_tasks: L("Blocked tasks", "Công việc bị chặn"),
        total_cost_usd: L("Total cost (USD)", "Tổng chi phí (USD)"),
        total_tokens: L("Total tokens", "Tổng token"),
      };
      body(
        card(
          L("Authority and spend", "Quyền hạn và chi phí"),
          fields(Object.entries(summary).map(([k, v]) => [names[k] || k, v])),
        ) +
          card(
            L("Cost by agent", "Chi phí theo agent"),
            table(
              [
                "Agent",
                "USD",
                L("Tokens", "Token"),
                L("Executions / failures", "Lượt chạy / lỗi"),
              ],
              spend.items.map((x) => [
                link("#/agent/" + x.agent_id, x.name),
                esc(x.cost_usd),
                esc(x.tokens),
                esc(`${x.executions} / ${x.failures}`),
              ]),
            ),
          ) +
          card(
            L("Autonomy policies", "Chính sách tự chủ"),
            table(
              [
                L("Action", "Hành động"),
                L("Ceiling", "Trần"),
                L("Reason", "Lý do"),
              ],
              policies.items.map((x) => [
                esc(x.name_vi || x.action_class),
                badge(
                  x.is_hard_block
                    ? L("Hard block", "Chặn bắt buộc")
                    : x.max_level,
                ),
                esc(x.rationale),
              ]),
            ) + pager(policies, "#/operations/governance"),
          ),
        guard,
      );
      return;
    }
    if (active === "system") {
      const [streamStatus, events, secrets, ctx] = await Promise.all([
        apiGet("/stream/status"),
        apiGet("/events/stats"),
        fetch("/system/secrets").then((r) => {
          if (!r.ok) throw new Error("Credential status unavailable");
          return r.json();
        }),
        apiGet("/console/context"),
      ]);
      body(
        card(
          L("Current connection", "Kết nối hiện tại"),
          fields([
            [L("Organization", "Tổ chức"), ctx.organization_id],
            [L("Caller", "Người gọi"), ctx.actor_kind],
            [
              L("Stream subscribers", "Kết nối luồng"),
              streamStatus.subscribers,
            ],
            [L("Last event", "Sự kiện gần nhất"), streamStatus.last_event_at],
          ]),
        ) +
          card(
            L("Event delivery", "Chuyển giao sự kiện"),
            fields(Object.entries(events.outbox)),
          ) +
          card(
            L("Configured credentials", "Thông tin xác thực đã cấu hình"),
            fields(Object.entries(secrets)),
          ) +
          card(
            L("Event types", "Loại sự kiện"),
            fields(Object.entries(events.by_type)),
          ),
        guard,
      );
      return;
    }
    body(
      empty(L("Unknown operations section.", "Không tìm thấy mục vận hành.")),
      guard,
    );
  });
})();
