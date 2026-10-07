/* Features share presentation and guarded forms, not resource caches. */
const Management = (() => {
  const host = () => document.getElementById("managementContent");
  const L = (en, vi) => (state.lang === "vi" ? vi : en);
  const record = valueHTML;
  const empty = (message) => UI.empty({title: L("No records yet", "Chưa có bản ghi"), description: message || L("Records will appear here when available.", "Bản ghi sẽ hiển thị ở đây khi có dữ liệu.")});
  const badge = (value) => UI.badge(value ?? "—");
  const link = (href, name) => `<a href="${esc(href)}">${esc(name)}</a>`;
  const card = (title, content) => UI.panel({title, content});
  const details = (title, value) => `<details class="ui-disclosure"><summary>${UIIcon('chevron-down')}<span>${esc(title)}</span></summary><div class="ui-disclosure-body">${record(value)}</div></details>`;
  const fields = (entries) => `<dl class="ui-record-fields">${entries.map(([name, value]) => `<div><dt>${esc(name)}</dt><dd>${record(value)}</dd></div>`).join("")}</dl>`;
  // Attribute strings are trusted, existing form contracts (min/max/pattern/required).
  const input = (name, title, value = "", type = "text", attrs = "") => UI.field({name, label:title, value, type}).replace('class="ui-input"', `class="ui-input" ${attrs}`);
  const area = (name, title, value = "", attrs = "") => `<div class="wide">${UI.textarea({name, label:title, value}).replace('class="ui-input"', `class="ui-input" ${attrs}`)}</div>`;
  const select = (name, title, options, selected = "") => UI.select({name, label:title, value:selected, options:options.map(([value,label]) => ({value,label}))});
  const form = (id, content, title, disabled = false, note = "") => `<form id="${id}" class="manage-form ui-form">${content}${note ? `<div class="wide ui-muted">${esc(note)}</div>` : ''}<div class="wide ui-toolbar">${UI.button({label:title,variant:'primary',disabled}).replace('type="button"', 'type="submit"')}</div><div class="wide form-result" role="status" aria-live="polite" hidden></div></form>`;
  const adminNote = (ctx) =>
    ctx.can_administer
      ? ""
      : L(
          "Changing configuration requires a privileged human credential. Use Token to select one.",
          "Thay đổi cấu hình cần thông tin xác thực của người có quyền quản trị. Dùng nút Token để chọn.",
        );
  const page = (title, intro, tabs = [], active = "") => `<div class="page-heading"><div><h1>${esc(title)}</h1><p>${esc(intro)}</p></div>${UI.button({label:L("Refresh", "Làm mới"),icon:'loader-circle'}).replace('<button ', '<button data-manage-refresh ')}</div>${tabs.length ? `<nav class="ui-route-tabs" aria-label="${esc(title)}">${tabs.map(([key,label,href]) => `<a href="${esc(href)}"${key === active ? ' aria-current="page"' : ''}>${esc(label)}</a>`).join('')}</nav>` : ''}<div id="managementBody" aria-busy="true">${UI.skeleton(L("Loading records", "Đang tải bản ghi"))}</div>`;
  function paint(html, guard) {
    if (!guard.current()) return false;
    host().classList.add('ui-page');
    host().dataset.page = parseHash().name;
    host().innerHTML = html;
    host().querySelector("[data-manage-refresh]").onclick = () => route();
    return true;
  }
  function body(html, guard) {
    if (!guard.current()) return;
    const target = document.getElementById("managementBody");
    target.setAttribute('aria-busy','false');
    target.innerHTML = html;
    enhance(target);
  }
  // Upgrade legacy action markup without replacing nodes or bound listeners.
  function enhance(target) {
    target.querySelectorAll(".readable-text").forEach(el=>{el.tabIndex=0;});
    target.querySelectorAll('.btn').forEach(el => {
      el.classList.add('ui-btn', el.classList.contains('danger') ? 'ui-btn-danger' : el.classList.contains('primary') ? 'ui-btn-primary' : 'ui-btn-secondary', el.classList.contains('sm') ? 'ui-btn-sm' : 'ui-btn-md');
      el.classList.remove('btn','primary','danger','sm');
      if(el.tagName === 'BUTTON' && !el.hasAttribute('type')) el.type='button';
    });
    target.querySelectorAll('.card').forEach(el => {el.classList.remove('card');el.classList.add('ui-panel');});
    target.querySelectorAll('.ui-panel > header').forEach(el => el.classList.add('ui-section-header'));
    target.querySelectorAll('.ui-panel > .body').forEach(el => {el.classList.remove('body');el.classList.add('ui-panel-body');el.removeAttribute('style');});
    target.querySelectorAll('.dept-tree').forEach(el => el.classList.add('ui-hierarchy'));
    target.querySelectorAll('.stats').forEach(el => el.classList.add('ui-metrics-host'));
    target.querySelectorAll('details:not(.ui-disclosure)').forEach(el => el.classList.add('ui-disclosure'));
    target.querySelectorAll('input:not([type="radio"]):not([type="checkbox"]),select,textarea').forEach(el => el.classList.add('ui-input'));
  }
  const table = (headers, rows) => rows.length ? UI.table({label:headers.join(' / '),columns:headers.map(label=>({label})),rows:rows.map(cells=>({cells}))}) + '<div class="ui-table-feedback ui-muted" data-table-feedback role="status" aria-live="polite"></div>' : empty();
  function failure(err, guard) {
    if (!guard.current()) return;
    const target = document.getElementById('managementBody');
    if(!target) return;
    target.setAttribute('aria-busy','false');
    target.innerHTML=UI.callout({title:L('Could not load records','Chưa tải được bản ghi'),description:err.message || String(err),status:'danger',actions:UI.button({label:L('Retry loading','Tải lại danh sách'),action:'reload-management'})});
    target.querySelector('[data-ui-action="reload-management"]').onclick=()=>route();
  }
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
      host().querySelectorAll('[data-table-feedback]').forEach(el => {
        const rows=[...el.previousElementSibling.querySelectorAll('tbody tr')];
        const count=rows.filter(row=>!row.hidden).length;
        el.textContent=count ? L(`${num(count)} records on this page`, `${num(count)} bản ghi trong trang này`) : L('No matching records on this page. Clear the search or use pagination.','Không có bản ghi phù hợp trong trang này. Xóa tìm kiếm hoặc dùng phân trang.');
      });
    };
  }
  const search = () => UI.field({label:L("Search this page", "Tìm trong trang này"),type:'search',hint:L('Search the records on this page. Use pagination for other records.','Tìm trong các bản ghi của trang này. Dùng phân trang để xem bản ghi khác.')}).replace('<input ', '<input data-table-search ');
  function wireForm(id, action, guard, renderResult = record) {
    const el = document.getElementById(id);
    if (!el || !guard.current()) return;
    el.onsubmit = async (event) => {
      event.preventDefault();
      if (el.dataset.submitting === "true" || !el.reportValidity()) return;
      el.dataset.submitting = "true";
      el.setAttribute("aria-busy", "true");
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
        el.dataset.submitting = "false";
        el.setAttribute("aria-busy", "false");
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
    enhance,
    failure,
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
