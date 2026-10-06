/* Optional corpus views preserve missing measurements and document lineage. */
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
    page,
    paint,
    body,
    table,
    offset,
    pager,
    wireForm,
    recordId,
  } = Management;
  MANAGEMENT_VIEWS.set("business", async (r, guard) => {
    const active = r.arg || "projects",
      tabs = [
        ["projects", L("Projects", "Dự án"), "#/business/projects"],
        ["documents", L("Documents", "Tài liệu"), "#/business/documents"],
      ];
    paint(
      page(
        L("Business records", "Dữ liệu nghiệp vụ"),
        L(
          "Projects and controlled documents from this organization's imported corpus.",
          "Dự án và tài liệu được kiểm soát từ dữ liệu đã nhập của tổ chức.",
        ),
        tabs,
        active,
      ),
      guard,
    );
    const id = recordId(r);
    if (active === "projects") {
      if (id) {
        const [p, w, breakdown] = await Promise.all([
          apiGet(`/projects/${id}`),
          apiGet(`/projects/${id}/workspace`),
          apiGet(`/projects/${id}/wbs`),
        ]);
        const positions = (rows) =>
          table(
            [
              L("Code / work package", "Mã / hạng mục"),
              L("Measurements", "Lượt đo thực tế"),
              L("Plan", "Kế hoạch"),
              L("Actual", "Thực tế"),
              L("Delay (days)", "Trễ (ngày)"),
            ],
            rows.map((z) => [
              esc(z.code) + " · " + esc(z.name),
              esc(z.measured),
              esc(z.last_planned_finish),
              esc(
                z.is_measured
                  ? z.last_actual_finish
                  : L("Unmeasured", "Chưa đo thực tế"),
              ),
              esc(z.is_measured ? (z.slip_days ?? "—") : "—"),
            ]),
          );
        body(
          card(
            p.name,
            fields([
              ["ID", p.id],
              [L("Code", "Mã"), p.code],
              [L("Status", "Trạng thái"), p.status],
              [L("Address", "Địa chỉ"), p.address],
              [
                L("Planned completion", "Hoàn thành dự kiến"),
                p.planned_completion,
              ],
            ]),
          ) +
            card(
              L("Late measured work", "Công việc đã đo và bị trễ"),
              positions(w.late || []),
            ) +
            card(
              L("Unmeasured work", "Công việc chưa có đo thực tế"),
              `<p>${esc(L("A planned date is not evidence of actual progress.", "Ngày kế hoạch không phải bằng chứng tiến độ thực tế."))}</p>` +
                positions(w.unmeasured || []),
            ) +
            card(
              L("All work packages", "Toàn bộ hạng mục"),
              table(
                [
                  L("Code", "Mã"),
                  L("Work package", "Hạng mục"),
                  L("Parent", "Cấp trên"),
                ],
                breakdown.items.map((n) => [
                  esc(n.code),
                  `<details><summary>${esc(n.name)}</summary>${fields([["ID", n.id]])}${details(L("Record", "Bản ghi"), n)}</details>`,
                  esc(n.parent_id || "—"),
                ]),
              ) + details(L("Workspace evidence", "Bằng chứng của dự án"), w),
            ) +
            link(
              "#/business/projects",
              L("Back to projects", "Về danh sách dự án"),
            ),
          guard,
        );
        return;
      }
      const d = await apiGet("/projects", { limit: 50, offset: offset() });
      body(
        card(
          L("Project register", "Danh mục dự án"),
          d.total
            ? table(
                [
                  L("Project", "Dự án"),
                  L("Status", "Trạng thái"),
                  L("Planned completion", "Hoàn thành dự kiến"),
                ],
                d.items.map((x) => [
                  link("#/business/projects/" + x.id, x.name),
                  badge(x.status),
                  esc(x.planned_completion),
                ]),
              ) + pager(d, "#/business/projects")
            : empty(
                L(
                  "No projects have been imported. Import the organization's corpus before inspecting project progress.",
                  "Chưa có dự án được nhập. Nhập bộ dữ liệu của tổ chức để xem tiến độ dự án.",
                ),
              ),
        ),
        guard,
      );
      return;
    }
    if (active === "documents") {
      if (id) {
        const d = await apiGet(`/documents/${id}`);
        const outstanding = (d.distributions || []).filter(
          (x) => !x.acknowledged_at && x.distributed_at,
        );
        body(
          card(
            d.title,
            fields([
              ["ID", d.id],
              [L("Code", "Mã"), d.code],
              [L("Classification", "Bảo mật"), d.classification],
              [
                L("Current version", "Phiên bản có hiệu lực"),
                d.live_version_no,
              ],
              [
                L(
                  "Required / acknowledged / outstanding",
                  "Bắt buộc / đã đọc / chưa đọc",
                ),
                `${d.compliance.required} / ${d.compliance.acknowledged} / ${d.compliance.outstanding}`,
              ],
            ]),
          ) +
            card(
              L("Version history", "Lịch sử phiên bản"),
              table(
                [
                  L("Version", "Phiên bản"),
                  L("Status", "Trạng thái"),
                  L("Issued / effective", "Ban hành / hiệu lực"),
                  L("Change", "Thay đổi"),
                ],
                d.versions.map((v) => [
                  esc(v.version_no),
                  badge(v.status),
                  esc(`${v.issued_on} / ${v.effective_from || "—"}`),
                  `<details><summary>${esc(v.change_note)}</summary>${fields([
                    ["ID", v.id],
                    [L("Content hash", "Hash nội dung"), v.content_hash],
                    [L("Approved by", "Người duyệt"), v.approved_by],
                  ])}</details>`,
                ]),
              ),
            ) +
            card(
              L("Distribution and reading", "Phân phối và xác nhận đọc"),
              table(
                [
                  L("Audience", "Đối tượng nhận"),
                  L("Version", "Phiên bản"),
                  L("Channel", "Kênh"),
                  L("Read by / at", "Người đọc / thời điểm"),
                ],
                d.distributions.map((x) => [
                  esc(x.audience_role_key),
                  esc(x.version_no),
                  esc(x.channel),
                  esc(
                    x.acknowledged_at
                      ? `${x.acknowledged_by} · ${x.acknowledged_at}`
                      : L("Not acknowledged", "Chưa xác nhận"),
                  ),
                ]),
              ),
            ) +
            card(
              L("Record a document action", "Ghi nhận thao tác tài liệu"),
              `<details><summary>${esc(L("Distribute current revision", "Phân phối phiên bản hiện tại"))}</summary>` +
                form(
                  "document-distribute",
                  input(
                    "audience_role_key",
                    L("Audience role key", "Khóa vai trò người nhận"),
                    "",
                    "text",
                    "required minlength=2",
                  ) +
                    select(
                      "channel",
                      L("Channel", "Kênh"),
                      ["system", "email", "print", "signage", "induction"].map(
                        (s) => [s, s],
                      ),
                      "system",
                    ) +
                    area("note", L("Note", "Ghi chú")),
                  L("Record distribution", "Ghi nhận phân phối"),
                  !d.live_version_no,
                  L(
                    "Records a distribution obligation; this endpoint does not send an email or upload a file.",
                    "Ghi nhận nghĩa vụ phân phối; endpoint này không gửi email hay tải file lên.",
                  ),
                ) +
                "</details>" +
                `<details><summary>${esc(L("Acknowledge reading", "Xác nhận đã đọc"))}</summary>` +
                form(
                  "document-acknowledge",
                  select(
                    "distribution_id",
                    L("Distribution", "Lượt phân phối"),
                    outstanding.map((x) => [
                      x.id,
                      `${x.audience_role_key} · v${x.version_no}`,
                    ]),
                  ) +
                    input(
                      "acknowledged_by",
                      L("Reader name", "Tên người đọc"),
                      "",
                      "text",
                      "required minlength=2",
                    ),
                  L("Record acknowledgement", "Ghi nhận xác nhận"),
                  !outstanding.length,
                  L(
                    "The reader name is self-asserted and stored with this action.",
                    "Tên người đọc do người thao tác khai và được lưu cùng thao tác này.",
                  ),
                ) +
                "</details>" +
                `<details><summary>${esc(L("Record a draft revision", "Ghi nhận phiên bản nháp"))}</summary>` +
                form(
                  "document-revision",
                  input(
                    "content_hash",
                    L("Content hash", "Hash nội dung"),
                    "",
                    "text",
                    'required pattern="[0-9a-fA-F]{8,64}"',
                  ) +
                    input(
                      "issued_on",
                      L("Issue date", "Ngày ban hành"),
                      "",
                      "date",
                      "required",
                    ) +
                    area(
                      "change_note",
                      L("Change note", "Ghi chú thay đổi"),
                      "",
                      "required minlength=3",
                    ),
                  L("Create draft revision", "Tạo phiên bản nháp"),
                  false,
                  L(
                    "This records a draft hash and change note. It does not activate the revision or replace document content.",
                    "Ghi hash và ghi chú phiên bản nháp. Không kích hoạt phiên bản và không thay nội dung tài liệu.",
                  ),
                ) +
                "</details>",
            ) +
            details(
              L("Source and lineage evidence", "Bằng chứng nguồn và lịch sử"),
              d,
            ) +
            link("#/business/documents", L("Back to documents", "Về tài liệu")),
          guard,
        );
        wireForm(
          "document-distribute",
          (v) => apiPost(`/documents/${id}/distributions`, v),
          guard,
        );
        wireForm(
          "document-acknowledge",
          (v) => apiPost(`/documents/${id}/acknowledge`, v),
          guard,
        );
        wireForm(
          "document-revision",
          (v) =>
            apiPost(`/documents/${id}/versions`, { ...v, activate: false }),
          guard,
        );
        return;
      }
      const d = await apiGet("/documents", { limit: 50, offset: offset() });
      body(
        card(
          L("Controlled documents", "Tài liệu được kiểm soát"),
          d.total
            ? table(
                [
                  L("Document", "Tài liệu"),
                  L("Code", "Mã"),
                  L("Department", "Phòng ban"),
                ],
                d.items.map((x) => [
                  link(
                    "#/business/documents/" + x.id,
                    x.title || x.name || x.code || x.id,
                  ),
                  esc(x.code || L("Unnumbered", "Chưa đánh số")),
                  esc(x.department),
                ]),
              ) + pager(d, "#/business/documents")
            : empty(
                L(
                  "No documents have been imported into this organization.",
                  "Chưa có tài liệu được nhập vào tổ chức này.",
                ),
              ),
        ),
        guard,
      );
      return;
    }
    body(empty(), guard);
  });
})();
