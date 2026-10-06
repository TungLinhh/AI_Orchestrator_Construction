/* Features share presentation and guarded forms, not resource caches. */
const Management = (() => {
  const host = () => document.getElementById("managementContent");
  const L = (en, vi) => (state.lang === "vi" ? vi : en);
  const record = valueHTML;
  const empty = (message) =>
    `<div class="empty">${esc(message || L("No records yet.", "Chưa có bản ghi."))}</div>`;
  const badge = (value) => `<span class="pill">${esc(value ?? "—")}</span>`;
  const link = (href, name) => `<a href="${esc(href)}">${esc(name)}</a>`;
  const card = (title, content) =>
    `<section class="card"><header><h2>${esc(title)}</h2></header><div class="manage-body">${content}</div></section>`;
  const details = (title, value) =>
    `<details class="record-details"><summary>${esc(title)}</summary>${record(value)}</details>`;
  const fields = (entries) =>
    `<dl class="record-fields">${entries.map(([name, value]) => `<div><dt>${esc(name)}</dt><dd>${record(value)}</dd></div>`).join("")}</dl>`;
  const input = (name, title, value = "", type = "text", attrs = "") =>
    `<label>${esc(title)}<input name="${esc(name)}" type="${type}" value="${esc(value ?? "")}" ${attrs}></label>`;
  const area = (name, title, value = "", attrs = "") =>
    `<label class="wide">${esc(title)}<textarea name="${esc(name)}" rows="5" ${attrs}>${esc(value)}</textarea></label>`;
  const select = (name, title, options, selected = "") =>
    `<label>${esc(title)}<select name="${esc(name)}">${options.map(([v, t]) => `<option value="${esc(v)}"${v === selected ? " selected" : ""}>${esc(t)}</option>`).join("")}</select></label>`;
  const form = (id, content, title, disabled = false, note = "") =>
    `<form id="${id}" class="manage-form">${content}<div class="wide form-notice">${esc(note)}</div><div class="wide"><button class="btn primary" type="submit" ${disabled ? "disabled" : ""}>${esc(title)}</button></div><div class="wide form-result" role="status" hidden></div></form>`;
  const adminNote = (ctx) =>
    ctx.can_administer
      ? ""
      : L(
          "Changing configuration requires a privileged human credential. Use Token to select one.",
          "Thay đổi cấu hình cần thông tin xác thực của người có quyền quản trị. Dùng nút Token để chọn.",
        );
  const page = (title, intro, tabs = [], active = "") =>
    `<div class="page-heading"><div><div class="eyebrow">${esc(L("Organization management", "Quản lý tổ chức"))}</div><h1>${esc(title)}</h1><p>${esc(intro)}</p></div><button class="btn" data-manage-refresh>${esc(L("Refresh", "Làm mới"))}</button></div>${tabs.length ? `<nav class="workspace-tabs" aria-label="${esc(title)}">${tabs.map(([key, label, href]) => `<a href="${href}"${key === active ? ' aria-current="page"' : ""}>${esc(label)}</a>`).join("")}</nav>` : ""}<div id="managementBody"><div class="empty">${esc(L("Loading…", "Đang tải…"))}</div></div>`;
  function paint(html, guard) {
    if (!guard.current()) return false;
    host().innerHTML = html;
    host().querySelector("[data-manage-refresh]").onclick = () => route();
    return true;
  }
  function body(html, guard) {
    if (guard.current())
      document.getElementById("managementBody").innerHTML = html;
  }
  const table = (headers, rows) =>
    rows.length
      ? `<div class="table-scroll"><table class="manage-table"><thead><tr>${headers.map((h) => `<th scope="col">${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows.map((c) => `<tr>${c.map((v) => `<td>${v}</td>`).join("")}</tr>`).join("")}</tbody></table>`
      : empty();
  function offset() {
    const parts = location.hash.split("/");
    const i = parts.indexOf("page");
    return i >= 0 && /^\d+$/.test(parts[i + 1] || "")
      ? Number(parts[i + 1])
      : 0;
  }
  function pager(data, href) {
    const start = data.offset || 0,
      limit = data.limit || 50,
      total = data.total ?? data.count ?? data.items.length;
    return `<div class="list-footer"><span>${esc(L("Records", "Bản ghi"))} ${total ? start + 1 : 0}–${Math.min(start + (data.returned ?? data.items.length), total)} / ${total}</span><div class="btn-row">${start ? link(href + "/page/" + Math.max(0, start - limit), L("Previous", "Trước")) : ""}${start + limit < total ? link(href + "/page/" + (start + limit), L("Next", "Tiếp")) : ""}</div></div>`;
  }
  function searchRows() {
    const el = host().querySelector("[data-table-search]");
    if (!el) return;
    el.oninput = () => {
      const term = el.value.toLocaleLowerCase();
      host()
        .querySelectorAll("tbody tr")
        .forEach((row) => {
          row.hidden = !row.textContent.toLocaleLowerCase().includes(term);
        });
    };
  }
  const search = () =>
    `<label class="manage-search">${esc(L("Search this page", "Tìm trong trang này"))}<input type="search" data-table-search placeholder="${esc(L("Name, ID or status", "Tên, ID hoặc trạng thái"))}"></label>`;
  function wireForm(id, action, guard, renderResult = record) {
    const el = document.getElementById(id);
    if (!el || !guard.current()) return;
    el.onsubmit = async (event) => {
      event.preventDefault();
      if (!el.reportValidity()) return;
      const submit = el.querySelector('button[type="submit"]'),
        result = el.querySelector(".form-result");
      submit.disabled = true;
      result.hidden = false;
      result.textContent = L("Saving…", "Đang xử lý…");
      try {
        const response = await action(
          Object.fromEntries(new FormData(el).entries()),
        );
        if (!guard.current()) return;
        result.className = "wide form-result success";
        result.innerHTML = `<b>${esc(L("Result", "Kết quả"))}</b>${renderResult(response)}`;
        toast(
          L(
            "Request completed. The result is shown below.",
            "Đã xử lý yêu cầu. Kết quả hiển thị bên dưới.",
          ),
        );
      } catch (err) {
        if (!guard.current()) return;
        result.className = "wide form-result error";
        result.textContent = err.message || String(err);
      } finally {
        submit.disabled = false;
      }
    };
  }
  async function patch(path, values, method = "PATCH") {
    const response = await fetch(API + path, {
      method,
      headers: authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify(values),
    });
    const payload = await response.json();
    if (!response.ok)
      throw new Error(
        payload.error?.message || valueText(payload.detail || payload),
      );
    return payload;
  }
  // Tool parameters come from the runtime contract, with typed inputs and no code editor.
  function schemaFields(schema) {
    return Object.entries(schema.properties || {})
      .map(([key, field]) => {
        const name = "arg_" + key,
          title = field.description || humanKey(key);
        const required = (schema.required || []).includes(key)
          ? "required"
          : "";
        if (field.enum)
          return select(name, title, [
            ["", L("Select…", "Chọn…")],
            ...field.enum.map((v) => [String(v), String(v)]),
          ]);
        if (field.type === "boolean")
          return select(name, title, [
            ["", L("Select…", "Chọn…")],
            ["true", L("Yes", "Có")],
            ["false", L("No", "Không")],
          ]);
        if (["integer", "number"].includes(field.type))
          return input(
            name,
            title,
            "",
            "number",
            `${required} step="${field.type === "integer" ? "1" : "any"}"`,
          );
        if (field.type === "string") return area(name, title, "", required);
        return empty(
          L(
            "This parameter requires a structured contract supported by the runtime.",
            "Tham số này cần hợp đồng dữ liệu được runtime hỗ trợ.",
          ),
        );
      })
      .join("");
  }
  function schemaArguments(schema, values) {
    const args = Object.create(null);
    for (const [key, field] of Object.entries(schema.properties || {})) {
      const raw = values["arg_" + key];
      if (raw == null || raw === "") {
        if ((schema.required || []).includes(key))
          throw new Error(
            L("Required parameter: ", "Tham số bắt buộc: ") + humanKey(key),
          );
        continue;
      }
      let value = raw;
      if (["number", "integer"].includes(field.type)) {
        value = Number(raw);
        if (
          !Number.isFinite(value) ||
          (field.type === "integer" && !Number.isInteger(value))
        )
          throw new Error(
            L("Invalid number: ", "Số không hợp lệ: ") + humanKey(key),
          );
      } else if (field.type === "boolean") value = raw === "true";
      if (field.enum) {
        value = field.enum.find((item) => String(item) === raw);
        if (value === undefined)
          throw new Error(
            L("Invalid choice: ", "Lựa chọn không hợp lệ: ") + humanKey(key),
          );
      }
      args[key] = value;
    }
    return args;
  }
  function parameterRow(index) {
    return `<div class="parameter-row">${input("parameter_name_" + index, L("Parameter name", "Tên tham số"), "", "text", 'pattern="[a-zA-Z_][a-zA-Z0-9_]*"')}${select(
      "parameter_type_" + index,
      L("Value type", "Kiểu giá trị"),
      [
        ["string", L("Text", "Văn bản")],
        ["number", L("Number", "Số")],
        ["integer", L("Whole number", "Số nguyên")],
        ["boolean", L("Yes / no", "Có / không")],
      ],
    )}${select("parameter_required_" + index, L("Required", "Bắt buộc"), [
      ["no", L("No", "Không")],
      ["yes", L("Yes", "Có")],
    ])}</div>`;
  }
  function parameterBuilder() {
    return `<div class="wide" data-parameter-rows>${parameterRow(0)}</div><div class="wide"><button class="btn sm" type="button" data-add-parameter>${esc(L("Add parameter", "Thêm tham số"))}</button></div>`;
  }
  function wireParameters() {
    const el = host().querySelector("[data-add-parameter]");
    if (!el) return;
    el.onclick = () => {
      const rows = host().querySelector("[data-parameter-rows]"),
        index = rows.children.length;
      const wrapper = document.createElement("div");
      wrapper.innerHTML = parameterRow(index);
      rows.appendChild(wrapper.firstElementChild);
    };
  }
  function authoredSchema(values) {
    const properties = {},
      required = [];
    for (let n = 0; Object.hasOwn(values, "parameter_name_" + n); n++) {
      const key = values["parameter_name_" + n].trim();
      if (!key) continue;
      if (Object.hasOwn(properties, key))
        throw new Error(L("Duplicate parameter: ", "Tham số trùng: ") + key);
      Object.defineProperty(properties, key, {
        value: { type: values["parameter_type_" + n] },
        enumerable: true,
      });
      if (values["parameter_required_" + n] === "yes") required.push(key);
    }
    return {
      type: "object",
      properties,
      required,
      additionalProperties: false,
    };
  }
  const numberOrNull = (value) => (value === "" ? null : Number(value));
  const classifications = [
    "public",
    "internal",
    "confidential",
    "restricted",
  ].map((s) => [s, s]);
  const catalogue = (kind, id = null) =>
    apiGet("/console/catalogue", {
      kind,
      id,
      limit: 50,
      offset: id ? 0 : offset(),
    });
  const recordId = (r) =>
    r.section !== "overview" && r.section !== "page" ? r.section : null;
  Object.assign(STR.vi, {
    "record.empty": "Chưa có mục nào",
    "dept.controls": "Điều khiển agent",
    "nav.agent": "Điều khiển agent",
    "nav.processes": "Quy trình",
    "nav.resources": "Tài nguyên",
    "nav.library": "Thư viện",
    "nav.business": "Dữ liệu nghiệp vụ",
    "nav.manage": "Quản trị",
    "nav.operations": "Vận hành",
    "nav.settings": "Cài đặt",
  });
  async function allPages(path, params = {}) {
    const items = [],
      seen = new Set();
    for (let offset = 0; ; offset += 200) {
      const page = await apiGet(path, { ...params, limit: 200, offset });
      for (const item of page.items) {
        if (seen.has(item.id)) continue;
        seen.add(item.id);
        items.push(item);
      }
      if (offset + page.items.length >= page.total) return items;
      if (!page.items.length)
        throw new Error(
          L(
            "The catalogue changed while loading. Refresh and try again.",
            "Danh mục thay đổi trong khi tải. Làm mới và thử lại.",
          ),
        );
    }
  }
  return {
    schemaFields,
    schemaArguments,
    parameterBuilder,
    wireParameters,
    authoredSchema,
    allPages,
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
    offset,
    pager,
    searchRows,
    search,
    wireForm,
    patch,
    numberOrNull,
    classifications,
    catalogue,
    recordId,
  };
})();
