/* Organization configuration is separate from agent behavior and lifecycle. */
(() => {
  const {
    L,
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
    wireForm,
    patch,
    numberOrNull,
    allPages,
    recordId,
  } = Management;
  MANAGEMENT_VIEWS.set("settings", async (r, guard) => {
    const active = r.arg || "organization",
      tabs = [
        [
          "appearance",
          L("Language & appearance", "Ngôn ngữ & giao diện"),
          "#/settings/appearance",
        ],
        [
          "organization",
          L("Organization", "Tổ chức"),
          "#/settings/organization",
        ],
        ["units", L("Units", "Đơn vị"), "#/settings/units"],
        ["roles", L("Roles", "Vai trò"), "#/settings/roles"],
      ];
    paint(
      page(
        L("Organization settings", "Cài đặt tổ chức"),
        L(
          "Manage the organization, its units and role boundaries.",
          "Quản lý tổ chức, các đơn vị và giới hạn vai trò.",
        ),
        tabs,
        active,
      ),
      guard,
    );
    if (active === "appearance") {
      const prefs = UIPreferences.get();
      body(
        card(
          L("Your workspace", "Không gian làm việc của bạn"),
          form(
            "ui-preferences",
            `<fieldset class="palette-field wide"><legend>${esc(L("Workspace palette", "Bảng màu không gian làm việc"))}</legend><p class="form-notice">${esc(L("Six balanced palettes, each with a matching light and dark mode.", "Sáu bảng màu hài hòa, mỗi bảng đều có chế độ sáng và tối tương ứng."))}</p><div class="palette-grid">${UIPalettes.items.map((p) => `<label class="palette-option"><input type="radio" name="palette" value="${p.id}" ${p.id === prefs.palette ? "checked" : ""}><span class="palette-swatch" style="--swatch:${p.light[0]};--swatch-soft:${p.light[2]}"><i></i><i></i><i></i></span><span>${esc(state.lang === "vi" ? p.vi : p.en)}</span></label>`).join("")}</div></fieldset>` +
              select(
                "language",
                L("Language", "Ngôn ngữ"),
                [
                  ["vi", "Tiếng Việt"],
                  ["en", "English"],
                ],
                state.lang,
              ) +
              select(
                "theme",
                L("Display mode", "Chế độ hiển thị"),
                [
                  ["auto", L("Follow system", "Theo hệ thống")],
                  ["light", L("Light", "Sáng")],
                  ["dark", L("Dark", "Tối")],
                ],
                prefs.theme,
              ) +
              select(
                "density",
                L("Spacing", "Khoảng cách"),
                [
                  ["comfortable", L("Comfortable", "Thoáng")],
                  ["compact", L("Compact", "Gọn")],
                ],
                prefs.density,
              ) +
              select(
                "motion",
                L("Motion", "Chuyển động"),
                [
                  ["auto", L("Follow system", "Theo hệ thống")],
                  ["reduced", L("Reduced", "Giảm chuyển động")],
                ],
                prefs.motion,
              ) +
              select(
                "home",
                L("Start screen", "Màn hình bắt đầu"),
                [
                  [
                    "campaigns",
                    L("Recruitment & workflows", "Đợt tuyển & quy trình"),
                  ],
                  ["organization", L("Organization", "Tổ chức")],
                ],
                prefs.home,
              ),
            L("Save preferences", "Lưu tùy chọn"),
            false,
            L(
              "Preview changes instantly. Save to keep them on this browser. Business permissions are managed separately.",
              "Xem màu ngay khi chọn. Bấm Lưu để giữ trên trình duyệt này. Quyền nghiệp vụ được quản lý riêng.",
            ),
          ),
        ),
        guard,
      );
      const preferenceForm = document.getElementById("ui-preferences");
      const savedPreferences = {...prefs,language:state.lang};

      const revert = document.createElement('button');
      revert.type='button'; revert.className='ui-btn ui-btn-ghost ui-btn-md';
      revert.textContent=L('Reset preview','Bỏ thay đổi xem trước');
      preferenceForm.querySelector('.ui-toolbar').append(revert);
      revert.onclick=()=>{
        Object.entries(savedPreferences).forEach(([key,value])=>{
          const field=preferenceForm.elements.namedItem(key);
          if(field) field.value=value;
        });
        UIPreferences.preview(savedPreferences);
      };
      preferenceForm.addEventListener("change", () => {
        UIPreferences.preview(Object.fromEntries(new FormData(preferenceForm)));
      });
      wireForm(
        "ui-preferences",
        async (values) => {
          UIPreferences.save(values);
          Object.assign(savedPreferences,UIPreferences.get(),{language:values.language});
          langSet(values.language);
        },
        guard,
        () =>
          esc(
            L(
              "Preferences saved on this browser.",
              "Đã lưu tùy chọn trên trình duyệt này.",
            ),
          ),
      );
      return;
    }
    const ctx = await apiGet("/console/context");
    if (active === "organization") {
      const [org, profiles] = await Promise.all([
        apiGet(`/organizations/${ORG}`),
        apiGet("/model-profiles"),
      ]);
      body(
        card(
          org.name,
          fields([
            ["ID", org.id],
            [L("Status", "Trạng thái"), org.status],
            [L("Created", "Ngày tạo"), org.created_at],
          ]),
        ) +
          card(
            L("Configuration", "Cấu hình"),
            form(
              "org-config",
              input(
                "name",
                L("Organization name", "Tên tổ chức"),
                org.name,
                "text",
                "required maxlength=255",
              ) +
                select(
                  "default_model_profile",
                  L("Default model profile", "Hồ sơ model mặc định"),
                  profiles.items.map((p) => [p.name, p.name]),
                  org.default_model_profile,
                ) +
                input(
                  "spend_cap_usd",
                  L("Spend cap (USD)", "Trần chi phí (USD)"),
                  org.spend_cap_usd,
                  "number",
                  'min="0" step="0.01"',
                ) +
                input(
                  "data_retention_days",
                  L("Retention (days)", "Lưu dữ liệu (ngày)"),
                  org.data_retention_days,
                  "number",
                  'min="1" max="3650" required',
                ) +
                area(
                  "description",
                  L("Description", "Mô tả"),
                  org.description || "",
                ),
              L("Save settings", "Lưu cài đặt"),
              !ctx.can_administer,
              adminNote(ctx),
            ),
          ),
        guard,
      );
      wireForm(
        "org-config",
        (v) =>
          patch(`/organizations/${ORG}`, {
            ...v,
            spend_cap_usd: numberOrNull(v.spend_cap_usd),
            data_retention_days: Number(v.data_retention_days),
          }),
        guard,
      );
      return;
    }
    const id = recordId(r);
    if (active === "units") {
      const data = await apiGet("/org-units", { limit: 50, offset: offset() });
      const units = await allPages("/org-units");
      const parents = [
        ["", L("Root unit", "Đơn vị gốc")],
        ...units.filter((u) => u.id !== id).map((u) => [u.id, u.name]),
      ];
      if (id) {
        const u = await apiGet(`/org-units/${id}`);
        body(
          card(
            u.name,
            fields([
              ["ID", u.id],
              [L("Purpose", "Mục đích"), u.purpose],
              [L("Type", "Loại"), u.unit_type],
              [L("Parent", "Cấp trên"), u.parent_id],
              [L("Path", "Đường dẫn"), u.path],
            ]) +
              (u.head_agent_id
                ? link(
                    "#/agent/" + u.head_agent_id,
                    L("Unit head controls", "Điều khiển agent trưởng đơn vị"),
                  )
                : ""),
          ) +
            card(
              L("Move unit", "Di chuyển đơn vị"),
              form(
                "unit-move",
                select(
                  "parent_id",
                  L("New parent", "Cấp trên mới"),
                  parents,
                  u.parent_id || "",
                ),
                L("Move this unit", "Di chuyển đơn vị này"),
                !ctx.can_administer,
                L(
                  "Moves its subtree. The server refuses cycles.",
                  "Di chuyển cả nhánh bên dưới. Server từ chối chu trình.",
                ) +
                  " " +
                  adminNote(ctx),
              ),
            ) +
            link("#/settings/units", L("Back to units", "Về các đơn vị")),
          guard,
        );
        wireForm(
          "unit-move",
          (v) =>
            apiPost(`/org-units/${id}/move`, {
              parent_id: v.parent_id || null,
            }),
          guard,
        );
        return;
      }
      body(
        card(
          L("Units", "Đơn vị"),
          table(
            [L("Name", "Tên"), L("Type", "Loại"), L("Purpose", "Mục đích")],
            data.items.map((u) => [
              link("#/settings/units/" + u.id, u.name),
              badge(u.unit_type),
              esc(u.purpose),
            ]),
          ) + pager(data, "#/settings/units"),
        ) +
          card(
            L("Create unit", "Tạo đơn vị"),
            `<details><summary>${esc(L("New unit", "Đơn vị mới"))}</summary>` +
              form(
                "unit-create",
                input("name", L("Name", "Tên"), "", "text", "required") +
                  input(
                    "slug",
                    L("Stable slug", "Slug cố định"),
                    "",
                    "text",
                    'required pattern="[a-z0-9][a-z0-9-]*"',
                  ) +
                  select("parent_id", L("Parent", "Cấp trên"), parents) +
                  select(
                    "unit_type",
                    L("Type", "Loại"),
                    ["department", "office", "team"].map((v) => [v, v]),
                    "department",
                  ) +
                  area("purpose", L("Purpose", "Mục đích")),
                L("Create unit", "Tạo đơn vị"),
                !ctx.can_administer,
                adminNote(ctx),
              ) +
              "</details>",
          ),
        guard,
      );
      wireForm(
        "unit-create",
        (v) => apiPost("/org-units", { ...v, parent_id: v.parent_id || null }),
        guard,
      );
      return;
    }
    if (active === "roles") {
      const data = await apiGet("/roles", { limit: 50, offset: offset() });
      const roles = id ? await allPages("/roles") : data.items;
      if (id) {
        const role = roles.find((r) => r.id === id);
        body(
          role
            ? card(
                role.name,
                fields([
                  ["ID", role.id],
                  [L("Description", "Mô tả"), role.description],
                  [L("Maximum autonomy", "Tự chủ tối đa"), role.max_autonomy],
                  [
                    L("Delegation depth", "Độ sâu giao việc"),
                    role.max_delegation_depth,
                  ],
                ]) +
                  details(L("Authority details", "Chi tiết quyền hạn"), role),
              ) + link("#/settings/roles", L("Back to roles", "Về các vai trò"))
            : empty(),
          guard,
        );
        return;
      }
      body(
        card(
          L("Role boundaries", "Giới hạn vai trò"),
          table(
            [
              L("Role", "Vai trò"),
              L("Autonomy ceiling", "Trần tự chủ"),
              L("Delegation depth", "Độ sâu giao việc"),
            ],
            roles.map((r) => [
              link("#/settings/roles/" + r.id, r.name),
              badge(r.max_autonomy),
              esc(r.max_delegation_depth),
            ]),
          ) + pager(data, "#/settings/roles"),
        ) +
          card(
            L("Author role", "Tạo vai trò"),
            `<details><summary>${esc(L("New role", "Vai trò mới"))}</summary>` +
              form(
                "role-create",
                input("name", L("Name", "Tên"), "", "text", "required") +
                  area("description", L("Description", "Mô tả")) +
                  select(
                    "max_autonomy",
                    L("Maximum autonomy", "Tự chủ tối đa"),
                    [
                      "l0_suggest",
                      "l1_low_risk_autonomous",
                      "l2_parent_review",
                      "l3_human_approval",
                      "l4_bounded_autonomous",
                    ].map((v) => [v, v]),
                    "l0_suggest",
                  ) +
                  input(
                    "max_delegation_depth",
                    L("Maximum delegation depth", "Độ sâu giao việc tối đa"),
                    0,
                    "number",
                    'min="0" max="8" required',
                  ),
                L("Create role", "Tạo vai trò"),
                !ctx.can_administer,
                adminNote(ctx),
              ) +
              "</details>",
          ),
        guard,
      );
      wireForm(
        "role-create",
        (v) =>
          apiPost("/roles", {
            ...v,
            max_delegation_depth: Number(v.max_delegation_depth),
          }),
        guard,
      );
      return;
    }
    body(
      empty(L("Unknown settings section.", "Không tìm thấy mục cài đặt.")),
      guard,
    );
  });
})();
