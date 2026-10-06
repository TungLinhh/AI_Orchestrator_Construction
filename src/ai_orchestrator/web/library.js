/* Skills, tools and memory keep provenance and capability gates visible. */
(() => {
  const {
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
    offset,
    pager,
    searchRows,
    search,
    wireForm,
    classifications,
    recordId,
    schemaFields,
    schemaArguments,
    parameterBuilder,
    wireParameters,
    authoredSchema,
  } = Management;
  MANAGEMENT_VIEWS.set("library", async (r, guard) => {
    const active = r.arg || "skills",
      tabs = [
        ["skills", L("Skills", "Kỹ năng"), "#/library/skills"],
        ["tools", L("Tools", "Công cụ"), "#/library/tools"],
        ["memory", L("Memory", "Bộ nhớ"), "#/library/memory"],
      ];
    paint(
      page(
        L("Library", "Thư viện"),
        L(
          "Inspect reusable skills, gated tools and knowledge with provenance.",
          "Kiểm tra kỹ năng tái sử dụng, công cụ có kiểm soát và tri thức có nguồn gốc.",
        ),
        tabs,
        active,
      ),
      guard,
    );
    if (active === "memory") {
      const stats = await apiGet("/memory/stats");
      body(
        card(
          L("Stored knowledge", "Tri thức đã lưu"),
          fields(Object.entries(stats.by_tier || {})),
        ) +
          card(
            L("Search memory", "Tìm trong bộ nhớ"),
            form(
              "memory-search",
              input(
                "query",
                L("Question or keywords", "Câu hỏi hoặc từ khóa"),
                "",
                "text",
                "required maxlength=10000",
              ) +
                select(
                  "max_classification",
                  L("Maximum classification", "Mức bảo mật tối đa"),
                  classifications,
                  "internal",
                ),
              L("Search", "Tìm kiếm"),
            ),
          ) +
          card(
            L("Add a sourced note", "Thêm ghi chú có nguồn"),
            form(
              "memory-write",
              input("title", L("Title", "Tiêu đề")) +
                input(
                  "source_ref",
                  L("Source reference", "Tham chiếu nguồn"),
                  "",
                  "text",
                  "required",
                ) +
                select(
                  "classification",
                  L("Classification", "Bảo mật"),
                  classifications,
                  "internal",
                ) +
                select(
                  "tier",
                  L("Memory tier", "Tầng bộ nhớ"),
                  [
                    "working",
                    "task_episodic",
                    "agent",
                    "department",
                    "organization",
                    "semantic",
                  ].map((s) => [s, s]),
                  "working",
                ) +
                area("content", L("Content", "Nội dung"), "", "required"),
              L("Save note", "Lưu ghi chú"),
            ),
          ),
        guard,
      );
      wireForm(
        "memory-search",
        (v) => apiPost("/memory/search", { ...v, max_results: 20 }),
        guard,
        (result) =>
          (result.results || [])
            .map((item) =>
              card(
                item.title || item.item_id,
                `<p class="readable-text">${esc(item.content)}</p>` +
                  fields([
                    [L("Source", "Nguồn"), item.source_ref || item.source_type],
                    [L("Classification", "Bảo mật"), item.classification],
                    [
                      L("Tier / score", "Tầng / điểm"),
                      `${item.tier} / ${item.score}`,
                    ],
                  ]) +
                  details(L("Citation", "Trích dẫn"), item),
              ),
            )
            .join("") ||
          empty(L("No matching memories.", "Không tìm thấy bộ nhớ phù hợp.")),
      );
      wireForm(
        "memory-write",
        (v) =>
          apiPost("/memory", { ...v, source_type: "document", kind: "note" }),
        guard,
      );
      return;
    }
    if (!["skills", "tools"].includes(active)) {
      body(
        empty(L("Unknown library section.", "Không tìm thấy mục thư viện.")),
        guard,
      );
      return;
    }
    let runtimeTool = null;
    const id = recordId(r),
      ctx = await apiGet("/console/context");
    const d = id
      ? {
          items: [
            await (active === "skills"
              ? apiGet(`/skills/${encodeURIComponent(id)}`)
              : apiGet(`/tools/${encodeURIComponent(id)}`)),
          ],
        }
      : await apiGet(active === "skills" ? "/skills" : "/tools", {
          limit: 50,
          offset: offset(),
        });
    if (id) {
      const item = d.items[0];
      let html = card(
        item.name,
        `<code>${esc(item.id)}</code><p>${esc(item.description)}</p>` +
          fields([
            [L("Version", "Phiên bản"), item.current_version],
            [
              L("Governance / risk", "Kiểm soát / rủi ro"),
              item.governance_state || item.risk_level,
            ],
            [L("Effect", "Tác động"), item.effect_class],
            [L("Approval required", "Cần phê duyệt"), item.requires_approval],
          ]) +
          (item.instructions
            ? `<pre class="readable-text">${esc(item.instructions)}</pre>`
            : "") +
          details(L("Schema and evidence", "Schema và bằng chứng"), item),
      );
      if (active === "tools") {
        const runtime = ctx.runtime_tools.find((t) => t.name === item.name);
        runtimeTool = runtime;
        html += runtime
          ? card(
              L(
                "Test tool through policy gateway",
                "Thử công cụ qua cổng chính sách",
              ),
              details(
                L("Input schema", "Schema đầu vào"),
                runtime.input_schema,
              ) +
                form(
                  "tool-test",
                  schemaFields(runtime.input_schema),
                  L("Run simulation", "Chạy mô phỏng"),
                  false,
                  L(
                    "Simulation is used here. The server still checks authority, approval and tool policy.",
                    "Ở đây dùng chế độ mô phỏng. Server vẫn kiểm tra quyền, phê duyệt và chính sách công cụ.",
                  ),
                ),
            )
          : card(
              L("Runtime availability", "Khả dụng khi thực thi"),
              `<p>${esc(L("This registry tool has no handler in the diagnostic gateway. Bindings do not install a handler.", "Công cụ trong danh mục chưa có handler ở cổng chẩn đoán. Việc cấp quyền không cài đặt handler."))}</p>`,
            );
      }
      if (active === "skills" && !item.is_published) {
        html += card(
          L("Publish after review", "Xuất bản sau kiểm tra"),
          form(
            "skill-publish",
            select("tests_passed", L("Tests passed", "Kiểm thử đã đạt"), [
              ["", L("Select measured result…", "Chọn kết quả đã đo…")],
              ["yes", L("Yes", "Có")],
              ["no", L("No", "Không")],
            ]) +
              input(
                "test_reference",
                L("Test report reference", "Tham chiếu báo cáo kiểm thử"),
                "",
                "text",
                "required",
              ) +
              select(
                "scan_clean",
                L("Security scan clean", "Kiểm tra bảo mật sạch"),
                [
                  ["", L("Select measured result…", "Chọn kết quả đã đo…")],
                  ["yes", L("Yes", "Có")],
                  ["no", L("No", "Không")],
                ],
              ) +
              input(
                "scan_reference",
                L("Scan report reference", "Tham chiếu báo cáo bảo mật"),
              ),
            L("Publish this version", "Xuất bản phiên bản này"),
            !ctx.can_administer,
            L(
              "The server requires passing test evidence and a clean scan for elevated capability. Publication does not invent this evidence.",
              "Server yêu cầu bằng chứng kiểm thử đạt và kiểm tra bảo mật sạch với quyền cao. Việc xuất bản không tự tạo ra bằng chứng này.",
            ) +
              " " +
              adminNote(ctx),
          ),
        );
      }
      body(
        html + link("#/library/" + active, L("Back to library", "Về thư viện")),
        guard,
      );
      wireForm(
        "skill-publish",
        (v) => {
          if (!v.tests_passed || !v.scan_clean)
            throw new Error(
              L(
                "Select the recorded test and scan results.",
                "Chọn kết quả kiểm thử và bảo mật đã ghi nhận.",
              ),
            );
          return apiPost(`/skills/${id}/publish`, {
            skill_version_id: item.version_id,
            test_results: {
              passed: v.tests_passed === "yes",
              reference: v.test_reference,
            },
            security_scan: {
              clean: v.scan_clean === "yes",
              reference: v.scan_reference,
            },
            governance_state: "reviewed",
          });
        },
        guard,
      );
      wireForm(
        "tool-test",
        (v) => {
          const args = schemaArguments(runtimeTool.input_schema, v);
          return apiPost("/tools/invoke", {
            tool_name: item.name,
            arguments: args,
            run_mode: "simulation",
          });
        },
        guard,
      );
      return;
    }
    const author =
      active === "skills"
        ? form(
            "registry-create",
            input(
              "name",
              L("Stable skill name", "Tên kỹ năng cố định"),
              "",
              "text",
              'required pattern="[a-z0-9][a-z0-9_-]*"',
            ) +
              input("description", L("Description", "Mô tả")) +
              area(
                "instructions",
                L("Instructions", "Chỉ dẫn"),
                "",
                "required",
              ) +
              select(
                "risk_level",
                L("Risk", "Rủi ro"),
                ["low", "medium", "high", "critical", "privileged"].map((s) => [
                  s,
                  s,
                ]),
                "low",
              ),
            L("Create experimental skill", "Tạo kỹ năng thử nghiệm"),
            !ctx.can_administer,
            adminNote(ctx),
          )
        : form(
            "registry-create",
            input(
              "name",
              L("Stable tool name", "Tên công cụ cố định"),
              "",
              "text",
              'required pattern="[a-z0-9][a-z0-9_]*"',
            ) +
              input("description", L("Description", "Mô tả")) +
              select(
                "risk_level",
                L("Risk", "Rủi ro"),
                [
                  "read_only",
                  "low_risk_write",
                  "external_side_effect",
                  "privileged",
                  "destructive",
                ].map((s) => [s, s]),
                "read_only",
              ) +
              select(
                "effect_class",
                L("Effect", "Tác động"),
                [
                  "read",
                  "prepare",
                  "mutate_internal",
                  "external_send",
                  "destructive",
                  "privileged",
                ].map((s) => [s, s]),
                "read",
              ) +
              select(
                "data_classification",
                L("Classification", "Bảo mật"),
                classifications,
                "internal",
              ) +
              parameterBuilder(),
            L("Register tool metadata", "Đăng ký thông tin công cụ"),
            !ctx.can_administer,
            L(
              "Registers metadata. A runtime handler must be installed separately before invocation.",
              "Đăng ký thông tin. Handler thực thi cần được cài đặt riêng trước khi gọi.",
            ) +
              " " +
              adminNote(ctx),
          );
    body(
      card(
        L("Registry", "Danh mục"),
        search() +
          table(
            [
              L("Name", "Tên"),
              L("Version", "Phiên bản"),
              L("Governance / risk", "Kiểm soát / rủi ro"),
              L("Approval", "Phê duyệt"),
            ],
            d.items.map((s) => [
              link(`#/library/${active}/${s.id}`, s.name),
              esc(s.current_version),
              badge(s.governance_state || s.risk_level),
              esc(s.requires_approval ? L("Required", "Bắt buộc") : "—"),
            ]),
          ) +
          pager(d, "#/library/" + active),
      ) +
        card(
          L("Author a resource", "Tạo tài nguyên"),
          `<details><summary>${esc(L("Create a new resource", "Tạo tài nguyên mới"))}</summary>${author}</details>`,
        ),
      guard,
    );
    searchRows();
    wireParameters();
    wireForm(
      "registry-create",
      (v) =>
        apiPost(
          active === "skills" ? "/skills" : "/tools",
          active === "skills"
            ? v
            : {
                name: v.name,
                description: v.description,
                risk_level: v.risk_level,
                effect_class: v.effect_class,
                data_classification: v.data_classification,
                input_schema: authoredSchema(v),
              },
        ),
      guard,
    );
  });
})();
