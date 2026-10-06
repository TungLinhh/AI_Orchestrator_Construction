/* Source intake creates a campaign; approvals and external writes remain server-owned. */
const ProcurementIntake = (() => {
  const { L, card, form, input, area, select, wireForm } = Management;
  function panel(ctx) {
    return card(
      L("Prepare a real purchase", "Chuẩn bị đợt mua sắm thật"),
      `<details><summary>${esc(L("Add BOQ and supplier quotations", "Nhập BOQ và báo giá nhà cung cấp"))}</summary>` +
        form(
          "procurement-intake",
          area(
            "boss_brief",
            L(
              "Purchase brief and acceptance criteria",
              "Yêu cầu mua sắm và điều kiện chấp nhận",
            ),
            "",
            "required minlength=20",
          ) +
            input(
              "boq_source",
              L("BOQ source document", "Tài liệu BOQ nguồn"),
              "",
              "text",
              "required",
            ) +
            input(
              "material_review_owner",
              L(
                "Material quality review owner",
                "Người phụ trách duyệt chất lượng vật tư",
              ),
              "",
              "text",
              "required",
            ) +
            input(
              "need_date",
              L("Required delivery date", "Ngày cần vật tư"),
              "",
              "date",
              "required",
            ) +
            input(
              "g3_date",
              L("G3 milestone", "Mốc G3"),
              "",
              "date",
              "required",
            ) +
            `<fieldset class="wide intake-group"><legend>1 · ${esc(L("BOQ materials", "Danh mục vật tư BOQ"))}</legend><div id="intake-materials"></div><button class="btn sm" type="button" id="intake-add-material">${esc(L("Add material", "Thêm vật tư"))}</button></fieldset>` +
            `<fieldset class="wide intake-group"><legend>2 · ${esc(L("Supplier source records", "Hồ sơ nguồn nhà cung cấp"))}</legend><p class="form-notice">${esc(L("At least three suppliers. Record actual legal, financial, HSE and quotation sources.", "Tối thiểu ba NCC. Ghi nguồn pháp lý, tài chính, HSE và báo giá thực tế."))}</p><div id="intake-suppliers"></div><button class="btn sm" type="button" id="intake-add-supplier">${esc(L("Add supplier", "Thêm nhà cung cấp"))}</button></fieldset>` +
            `<fieldset class="wide intake-group"><legend>3 · ${esc(L("Quote each BOQ line", "Báo giá từng dòng BOQ"))}</legend><p class="form-notice">${esc(L("Quantities follow the BOQ. Certificates and conformity are source claims for QA to verify; a human material review remains mandatory.", "Số lượng lấy từ BOQ. Chứng chỉ và tính phù hợp là khai báo nguồn để QA kiểm tra; vẫn phải qua người vật tư duyệt."))}</p><div id="intake-quotes"></div></fieldset>`,
          L("Create procurement for review", "Tạo đợt mua sắm để xét duyệt"),
          !ctx.can_administer,
          L(
            "Creation does not run work, send RFQs, issue POs or pay. Delivery and invoices are supplied later with their own evidence.",
            "Tạo xong chưa chạy, chưa gửi RFQ, phát hành PO hay thanh toán. Hồ sơ giao nhận và hóa đơn được bổ sung ở bước riêng.",
          ),
        ) +
        `</details>`,
    );
  }
  function wire(ctx, guard) {
    const host = document.getElementById("procurement-intake");
    if (!host) return;
    let materialSequence = 0,
      supplierSequence = 0;
    const rowValues = (node) =>
      Object.fromEntries(
        Array.from(node.querySelectorAll("input,select,textarea")).map((el) => [
          el.name,
          el.value,
        ]),
      );
    const materials = () =>
      Array.from(host.querySelectorAll("[data-intake-material]")).map(
        rowValues,
      );
    const suppliers = () =>
      Array.from(host.querySelectorAll("[data-intake-supplier]")).map(
        rowValues,
      );
    const pairKey = (supplier, material) => supplier + ":" + material;
    const quotes = new Map();
    function rebuildQuotes() {
      host
        .querySelectorAll("[data-intake-quote]")
        .forEach((node) =>
          quotes.set(node.dataset.intakeQuote, rowValues(node)),
        );
      document.getElementById("intake-quotes").innerHTML = suppliers()
        .map(
          (s) =>
            `<h3>${esc(s.supplier_id)}</h3>` +
            materials()
              .map((m) => {
                const key = pairKey(s.supplier_id, m.material_id),
                  q = quotes.get(key) || {};
                return (
                  `<div class="intake-line" data-intake-quote="${esc(key)}"><h4>${esc(m.material_id)} · ${esc(m.name)} — ${esc(m.quantity)} ${esc(m.unit)}</h4><div class="manage-form">` +
                  input(
                    "unit_price",
                    L("Unit price (VND)", "Đơn giá (VND)"),
                    q.unit_price || "",
                    "number",
                    "required min=1 step=1",
                  ) +
                  input(
                    "delivery_days",
                    L("Delivery (days)", "Giao trong (ngày)"),
                    q.delivery_days ?? "",
                    "number",
                    "required min=0 max=3650 step=1",
                  ) +
                  input(
                    "warranty_months",
                    L("Warranty (months)", "Bảo hành (tháng)"),
                    q.warranty_months ?? "",
                    "number",
                    "required min=0 max=1200 step=1",
                  ) +
                  input(
                    "certificate_ref",
                    L(
                      "Certificate source (blank if missing)",
                      "Nguồn chứng chỉ (để trống nếu thiếu)",
                    ),
                    q.certificate_ref || "",
                  ) +
                  select(
                    "spec_compliant",
                    L(
                      "Technical source confirms compliance",
                      "Nguồn kỹ thuật xác nhận phù hợp",
                    ),
                    [
                      ["false", L("Unverified / no", "Chưa xác minh / không")],
                      [
                        "true",
                        L("Yes, subject to QA review", "Có, chờ QA kiểm tra"),
                      ],
                    ],
                    q.spec_compliant || "false",
                  ) +
                  `</div></div>`
                );
              })
              .join(""),
        )
        .join("");
    }
    function addMaterial() {
      const row = document.createElement("div");
      row.className = "intake-line";
      row.dataset.intakeMaterial = "true";
      row.innerHTML =
        `<div class="manage-form">` +
        input(
          "material_id",
          L("Material ID", "Mã vật tư"),
          "MAT-" + ++materialSequence,
          "text",
          "required pattern='[A-Za-z0-9_-]{1,80}'",
        ) +
        input(
          "name",
          L("Material name", "Tên vật tư"),
          "",
          "text",
          "required",
        ) +
        input(
          "quantity",
          L("Quantity", "Số lượng"),
          "",
          "number",
          "required min=0.001 step=0.001",
        ) +
        input("unit", L("Unit", "Đơn vị"), "", "text", "required") +
        area(
          "technical_spec",
          L("Technical specification", "Yêu cầu kỹ thuật"),
          "",
          "required minlength=5",
        ) +
        select("long_lead", L("Long lead", "Thời gian cung ứng dài"), [
          ["false", L("No", "Không")],
          ["true", L("Yes", "Có")],
        ]) +
        `</div><button type="button" class="btn sm" data-remove>${esc(L("Remove material", "Xóa vật tư"))}</button>`;
      document.getElementById("intake-materials").appendChild(row);
      row.querySelector("[data-remove]").onclick = () => {
        row.remove();
        rebuildQuotes();
      };
      row.onchange = rebuildQuotes;
      rebuildQuotes();
    }
    function addSupplier() {
      const row = document.createElement("div");
      row.className = "intake-line";
      row.dataset.intakeSupplier = "true";
      row.innerHTML =
        `<div class="manage-form">` +
        input(
          "supplier_id",
          L("Supplier ID", "Mã NCC"),
          "NCC-" + ++supplierSequence,
          "text",
          "required pattern='[A-Za-z0-9_-]{1,80}'",
        ) +
        select(
          "legal_valid",
          L("Legal status verified", "Pháp lý đã xác minh hợp lệ"),
          [
            ["false", L("No / incomplete", "Không / chưa đủ")],
            ["true", L("Yes", "Có")],
          ],
        ) +
        [
          ["legal_ref", L("Legal source", "Nguồn pháp lý")],
          ["financial_ref", L("Financial source", "Nguồn tài chính")],
          ["hse_ref", L("HSE source", "Nguồn HSE")],
          ["quote_ref", L("Quotation source", "Nguồn báo giá")],
          ["financial_health", L("Financial assessment", "Đánh giá tài chính")],
          ["hse", L("HSE assessment", "Đánh giá HSE")],
        ]
          .map(([key, label]) => input(key, label, "", "text", "required"))
          .join("") +
        `</div><button type="button" class="btn sm" data-remove>${esc(L("Remove supplier", "Xóa NCC"))}</button>`;
      document.getElementById("intake-suppliers").appendChild(row);
      row.querySelector("[data-remove]").onclick = () => {
        row.remove();
        rebuildQuotes();
      };
      row.onchange = rebuildQuotes;
      rebuildQuotes();
    }
    document.getElementById("intake-add-material").onclick = addMaterial;
    document.getElementById("intake-add-supplier").onclick = addSupplier;
    addMaterial();
    for (let n = 0; n < 3; n++) addSupplier();
    wireForm(
      "procurement-intake",
      async () => {
        const value = (name) => host.querySelector(`[name="${name}"]`).value;
        const boq = materials().map((m) => ({
          ...m,
          quantity: Number(m.quantity),
          long_lead: m.long_lead === "true",
        }));
        const supplierRows = suppliers().map((s) => ({
          ...s,
          legal_valid: s.legal_valid === "true",
          quotes: boq.map((m) => {
            const q = rowValues(
              Array.from(host.querySelectorAll("[data-intake-quote]")).find(
                (el) =>
                  el.dataset.intakeQuote ===
                  pairKey(s.supplier_id, m.material_id),
              ),
            );
            return {
              ...q,
              material_id: m.material_id,
              quantity: m.quantity,
              unit_price: Number(q.unit_price),
              delivery_days: Number(q.delivery_days),
              warranty_months: Number(q.warranty_months),
              spec_compliant: q.spec_compliant === "true",
              currency: "VND",
            };
          }),
        }));
        const created = await apiPost("/workflows/procurement", {
          ...Object.fromEntries(
            [
              "boss_brief",
              "boq_source",
              "material_review_owner",
              "need_date",
              "g3_date",
            ].map((k) => [k, value(k)]),
          ),
          boq,
          suppliers: supplierRows,
        });
        if (guard.current()) go("#/processes/workflows/" + created.id);
      },
      guard,
    );
  }
  return { panel, wire };
})();
