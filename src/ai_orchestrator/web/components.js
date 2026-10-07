/* Presentation only. Text is escaped; content/actions are trusted component markup. */
const UI = (() => {
  const L = (en, vi) => (state.lang === "vi" ? vi : en);
  const E = (value) => esc(String(value ?? ""));
  let serial = 0;
  const modalStack = [];
  let modalOverflow = "";
  const id = (prefix) => `ui-${prefix}-${++serial}`;
  const tones = ["neutral", "info", "success", "warning", "danger"];
  const tone = (value) => (tones.includes(value) ? value : "neutral");
  const attr = (name, value) => (value == null ? "" : ` ${name}="${E(value)}"`);
  const href = (value) => {
    if (!/^(#|\/(?!\/))/.test(value || "")) return "#";
    try {
      return new URL(value, location.href).origin ===
        new URL(location.href).origin
        ? value
        : "#";
    } catch {
      return "#";
    }
  };
  const icon = (name) => (name ? UIIcon(name) : "");
  function button({
    label = "",
    variant = "secondary",
    size = "md",
    icon: symbol,
    disabled = false,
    loading = false,
    action,
    iconOnly = false,
    pressed,
  } = {}) {
    const v = ["primary", "secondary", "ghost", "danger"].includes(variant)
      ? variant
      : "secondary";
    const s = ["sm", "md", "lg"].includes(size) ? size : "md";
    return `<button type="button" class="ui-btn ui-btn-${v} ui-btn-${s}${iconOnly ? " ui-icon-button" : ""}"${attr("aria-label", iconOnly ? label : null)}${attr("data-ui-action", action)}${attr("aria-pressed", pressed)}${loading ? ' aria-busy="true"' : ""}${disabled || loading ? " disabled" : ""}>${icon(loading ? "loader-circle" : symbol)}${iconOnly ? '<span class="ui-sr">' + E(label) + "</span>" : E(label)}</button>`;
  }
  const iconButton = (options) => button({ ...options, iconOnly: true });
  function field({
    label,
    name,
    value = "",
    type = "text",
    kind = "input",
    options = [],
    hint = "",
    error = "",
    disabled = false,
    required = false,
    readonly = false,
  } = {}) {
    const key = id("field"),
      description = error || hint;
    const attributes = ` id="${key}" name="${E(name || key)}"${disabled ? " disabled" : ""}${required ? " required" : ""}${readonly ? " readonly" : ""}${error ? ' aria-invalid="true"' : ""}${description ? ` aria-describedby="${key}-help"` : ""}`;
    const control =
      kind === "select"
        ? `<select class="ui-input"${attributes}>${options.map((o) => `<option value="${E(o.value)}"${String(o.value) === String(value) ? " selected" : ""}>${E(o.label)}</option>`).join("")}</select>`
        : kind === "textarea"
          ? `<textarea class="ui-input" rows="3"${attributes}>${E(value)}</textarea>`
          : `<input class="ui-input" type="${["text", "search", "email", "number", "password"].includes(type) ? type : "text"}" value="${E(value)}"${attributes}>`;
    return `<div class="ui-field"><label for="${key}">${E(label)}${required ? ` <span class="ui-muted">(${L("required", "bắt buộc")})</span>` : ""}</label>${control}${description ? `<p class="ui-help${error ? " ui-error" : ""}" id="${key}-help">${E(description)}</p>` : ""}</div>`;
  }
  function choice({
    label,
    name,
    value = "",
    kind = "checkbox",
    checked = false,
    disabled = false,
  } = {}) {
    const key = id("choice"),
      radio = kind === "radio",
      sw = kind === "switch";
    return `<label class="ui-choice" for="${key}"><input id="${key}" name="${E(name || key)}" value="${E(value)}" type="${radio ? "radio" : "checkbox"}"${sw ? ' role="switch"' : ""}${checked ? " checked" : ""}${disabled ? " disabled" : ""}><span>${E(label)}</span></label>`;
  }
  const badge = (label, status = "neutral") =>
    `<span class="ui-badge" data-tone="${tone(status)}">${icon({ neutral: "clock", info: "info", success: "circle-check", warning: "circle-alert", danger: "circle-alert" }[tone(status)])}${E(label)}</span>`;
  const avatar = (name) =>
    `<span class="ui-avatar" role="img" aria-label="${E(name)}">${E(
      String(name)
        .trim()
        .split(/\s+/)
        .slice(0, 2)
        .map((w) => Array.from(w)[0])
        .join("")
        .toLocaleUpperCase(),
    )}</span>`;
  const panel = ({
    title,
    description = "",
    content = "",
    actions = "",
  } = {}) =>
    `<section class="ui-panel"><header class="ui-section-header"><div><h2>${E(title)}</h2>${description ? `<p class="ui-muted">${E(description)}</p>` : ""}</div>${actions}</header><div class="ui-panel-body">${content}</div></section>`;
  const toolbar = (content, label) =>
    `<div class="ui-toolbar" role="group" aria-label="${E(label)}">${content}</div>`;
  const callout = ({
    title,
    description = "",
    status = "info",
    actions = "",
  } = {}) =>
    `<div class="ui-callout" data-tone="${tone(status)}">${icon(status === "danger" || status === "warning" ? "circle-alert" : "info")}<div><strong>${E(title)}</strong>${description ? `<p>${E(description)}</p>` : ""}${actions}</div></div>`;
  const empty = ({
    title,
    description = "",
    kind = "empty",
    actions = "",
  } = {}) =>
    `<div class="ui-empty" data-state="${E(kind)}">${icon(kind === "error" ? "circle-alert" : kind === "no-results" ? "search" : "list-todo")}<h3>${E(title)}</h3><p class="ui-muted">${E(description)}</p>${actions}</div>`;
  const skeleton = (label) =>
    `<div class="ui-skeleton" role="status" aria-label="${E(label || L("Loading", "Đang tải"))}" aria-busy="true"><span class="ui-sr">${E(label || L("Loading", "Đang tải"))}</span><span></span><span></span><span></span></div>`;
  const kbd = (text) => `<kbd class="ui-kbd">${E(text)}</kbd>`;
  const copyId = (value) =>
    `<button type="button" class="ui-copy-id" data-ui-copy="${E(value)}" title="${E(value)}" aria-label="${E(L("Copy ID: ", "Sao chép mã: ") + value)}"><span>${E(value.length > 26 ? value.slice(0, 12) + "…" + value.slice(-8) : value)}</span>${icon("copy")}</button>`;
  const listRow = ({
    title,
    meta = "",
    status = "",
    statusTone = "neutral",
    url,
    error = "",
    actions = "",
    selected = false,
  } = {}) =>
    `<article class="ui-list-row"${selected ? ' data-selected="true"' : ""}><div class="ui-row-main"><h3>${url ? `<a href="${E(href(url))}">${E(title)}</a>` : E(title)}</h3><p class="ui-muted">${E(meta)}</p>${error ? `<p class="ui-error">${E(error)}</p>` : ""}</div><div class="ui-row-actions">${status ? badge(status, statusTone) : ""}${actions}</div></article>`;
  const table = ({ label, columns = [], rows = [] } = {}) =>
    `<div class="ui-table-scroll" role="region" tabindex="0" aria-label="${E(label)}"><table class="ui-table"><caption class="ui-sr">${E(label)}</caption><thead><tr>${columns.map((c, i) => `<th scope="col"${c.sortable ? ' aria-sort="none"' : ""}>${c.sortable ? `<button type="button" data-ui-sort="${i}" aria-label="${E(L("Sort by ", "Sắp xếp theo ") + c.label)}">${E(c.label)}${icon("chevron-down")}</button>` : E(c.label)}</th>`).join("")}</tr></thead><tbody>${rows.map((row) => `<tr${row.selected ? ' data-selected="true"' : ""}>${row.cells.map((cell) => `<td>${cell}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
  function tabs({ label, items = [], selected = 0, segmented = false } = {}) {
    const key = id("tabs");
    if (segmented)
      return `<div class="ui-tabs" role="group" aria-label="${E(label)}" data-ui-segmented><span class="ui-tab-indicator" aria-hidden="true"></span>${items.map((item, i) => `<button type="button" aria-pressed="${i === selected}"${item.disabled ? " disabled" : ""}>${E(item.label)}</button>`).join("")}</div>`;
    return `<div data-ui-tabs><div class="ui-tabs" role="tablist" aria-label="${E(label)}"><span class="ui-tab-indicator" aria-hidden="true"></span>${items.map((item, i) => `<button type="button" role="tab" id="${key}-${i}" aria-controls="${key}-panel-${i}" aria-selected="${i === selected}" tabindex="${i === selected ? "0" : "-1"}"${item.disabled ? " disabled" : ""}>${E(item.label)}</button>`).join("")}</div>${items.map((item, i) => `<div role="tabpanel" id="${key}-panel-${i}" aria-labelledby="${key}-${i}" tabindex="0" class="ui-tab-panel"${i === selected ? "" : " hidden"}>${item.content || ""}</div>`).join("")}</div>`;
  }
  const tooltip = (content, text) => {
    const key = id("tooltip");
    return `<span class="ui-tooltip-host">${content.replace("<button ", '<button aria-describedby="' + key + '" ')}<span role="tooltip" id="${key}" class="ui-tooltip">${E(text)}</span></span>`;
  };
  const menu = ({ label, items = [] } = {}) => {
    const key = id("menu");
    return `<div class="ui-menu-host">${button({ label, icon: "chevron-down" }).replace("<button ", '<button data-ui-menu-trigger aria-haspopup="menu" aria-expanded="false" aria-controls="' + key + '" ')}<div class="ui-menu" role="menu" id="${key}" hidden aria-label="${E(label)}">${items.map((item) => `<button type="button" role="menuitem" tabindex="-1"${item.disabled ? " disabled" : ""}${attr("data-ui-action", item.action)}>${icon(item.icon)}${E(item.label)}</button>`).join("")}</div></div>`;
  };
  const popover = ({ label, content = "" } = {}) => {
    const key = id("popover");
    return `<div class="ui-popover-host">${button({ label }).replace("<button ", '<button popovertarget="' + key + '" ')}<div class="ui-popover" id="${key}" popover="auto" role="dialog" aria-label="${E(label)}">${content}${button({ label: L("Close", "Đóng") }).replace("<button ", '<button popovertarget="' + key + '" popovertargetaction="hide" ')}</div></div>`;
  };
  const breadcrumb = (
    items,
    label = L("Component breadcrumb", "Đường dẫn thành phần"),
  ) =>
    `<nav class="ui-breadcrumb" aria-label="${E(label)}"><ol>${items.map((item, i) => `<li>${i ? '<span aria-hidden="true">/</span>' : ""}${item.href ? `<a href="${E(href(item.href))}">${E(item.label)}</a>` : `<span aria-current="page">${E(item.label)}</span>`}</li>`).join("")}</ol></nav>`;
  const backButton = (label) =>
    button({
      label: label || L("Back", "Quay lại"),
      variant: "ghost",
      icon: "arrow-left",
      action: "back",
    });
  const logViewer = ({ label, text, filter = true, copyLabel } = {}) => {
    const key = id("log");
    return `<section class="ui-log" aria-labelledby="${key}"><h3 id="${key}">${E(label)}</h3><div class="ui-toolbar">${filter ? `<label>${E(L("Filter lines", "Lọc dòng"))}<input class="ui-input" type="search" data-ui-log-filter></label>` : ""}<label class="ui-choice"><input type="checkbox" data-ui-log-wrap checked><span>${E(L("Wrap lines", "Xuống dòng"))}</span></label>${button({ label: copyLabel || L("Copy all", "Sao chép toàn bộ"), icon: "copy", action: "copy-log" })}</div><pre data-wrap="true" tabindex="0" role="region" aria-label="${E(L("Log content: ", "Nội dung nhật ký: ") + label)}"><code>${E(text)}</code></pre><p class="ui-muted" data-ui-log-count aria-live="polite"></p></section>`;
  };
  const logs = new WeakMap();
  const logSource = (root) => {
    if (!logs.has(root)) logs.set(root, root.querySelector("code").textContent);
    return logs.get(root);
  };
  function toast(message, { status = "info", duration = 6000 } = {}) {
    let host = document.getElementById("uiToastRegion");
    if (!host) {
      host = document.createElement("div");
      host.id = "uiToastRegion";
      host.className = "ui-toast-region";
      host.setAttribute("role", "region");
      host.setAttribute("aria-label", L("Notifications", "Thông báo"));
      document.body.append(host);
    }
    const item = document.createElement("div");
    item.className = "ui-toast";
    item.dataset.tone = tone(status);
    item.setAttribute("role", status === "danger" ? "alert" : "status");
    item.innerHTML = `${icon(status === "danger" ? "circle-alert" : "circle-check")}<span>${E(message)}</span>${iconButton({ label: L("Dismiss notification", "Đóng thông báo"), icon: "x" })}`;
    host.append(item);
    let timer;
    const close = () => {
      clearTimeout(timer);
      item.remove();
    };
    const schedule = () => {
      clearTimeout(timer);
      if (duration > 0)
        timer = setTimeout(() => {
          if (!item.matches(":hover,:focus-within")) close();
          else schedule();
        }, duration);
    };
    item.querySelector("button").onclick = close;
    item.onmouseenter = () => clearTimeout(timer);
    item.onmouseleave = schedule;
    item.onfocusin = () => clearTimeout(timer);
    item.onfocusout = schedule;
    schedule();
    return close;
  }
  async function copy(text) {
    try {
      await navigator.clipboard.writeText(text);
      toast(L("Copied", "Đã sao chép"), { status: "success" });
      return true;
    } catch {
      toast(
        L(
          "Copy failed. Select the text and copy it manually.",
          "Không sao chép được. Chọn văn bản và sao chép thủ công.",
        ),
        { status: "danger", duration: 0 },
      );
      return false;
    }
  }
  function dialog({
    title,
    description = "",
    content = "",
    confirmLabel,
    kind = "dialog",
    onConfirm,
  } = {}) {
    const trigger = document.activeElement,
      key = id("dialog"),
      el = document.createElement("dialog");
    el.className = "ui-dialog";
    el.dataset.uiModal = kind === "drawer" ? "drawer" : "dialog";
    el.setAttribute("aria-labelledby", key);
    if (description) el.setAttribute("aria-describedby", key + "-description");
    el.innerHTML = `<header class="ui-section-header"><h2 id="${key}">${E(title)}</h2>${iconButton({ label: L("Close", "Đóng"), icon: "x", action: "close-modal" })}</header><div class="ui-dialog-body">${description ? `<p id="${key}-description" class="ui-muted">${E(description)}</p>` : ""}${content}</div><footer class="ui-toolbar">${button({ label: L("Cancel", "Hủy"), action: "close-modal" })}${confirmLabel ? button({ label: confirmLabel, variant: "primary", action: "confirm-modal" }) : ""}</footer>`;
    if (!modalStack.length)
      modalOverflow = document.documentElement.style.overflow;
    modalStack.push(el);
    document.documentElement.style.overflow = "hidden";
    document.body.append(el);
    const close = () => el.close();
    el.addEventListener(
      "close",
      () => {
        modalStack.splice(modalStack.indexOf(el), 1);
        if (!modalStack.length)
          document.documentElement.style.overflow = modalOverflow;
        el.remove();
        if (trigger?.isConnected) trigger.focus();
      },
      { once: true },
    );
    el.addEventListener("cancel", (event) => {
      event.preventDefault();
      close();
    });
    el.addEventListener("click", async (event) => {
      const action = event.target.closest("[data-ui-action]")?.dataset.uiAction;
      if (action === "close-modal") close();
      if (action === "confirm-modal") {
        const control = event.target.closest("button");
        control.disabled = true;
        control.setAttribute("aria-busy", "true");
        try {
          await onConfirm?.(el);
          if (el.open) close();
        } catch (error) {
          control.disabled = false;
          control.removeAttribute("aria-busy");
          let message = el.querySelector("[data-ui-modal-error]");
          if (!message) {
            message = document.createElement("div");
            message.dataset.uiModalError = "true";
            message.setAttribute("role", "alert");
            el.querySelector(".ui-dialog-body").append(message);
          }
          message.innerHTML = callout({
            title: L("Could not complete", "Không thể hoàn tất"),
            description: error.message || String(error),
            status: "danger",
          });
        }
      }
    });
    el.showModal();
    el.addEventListener("keydown", (event) => {
      if (event.key !== "Tab") return;
      const controls = [
        ...el.querySelectorAll(
          'button:not(:disabled),input:not(:disabled),select:not(:disabled),textarea:not(:disabled),a[href],[tabindex="0"]',
        ),
      ].filter((node) => node.getClientRects().length);
      const first = controls[0],
        last = controls[controls.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    });
    el.querySelector("input,textarea,select,button")?.focus();
    return { element: el, close };
  }
  function indicator(group) {
    const active = group.querySelector(
      '[aria-selected="true"],[aria-pressed="true"]',
    );
    if (!active) return;
    group.style.setProperty("--ui-tab-x", active.offsetLeft + "px");
    group.style.setProperty("--ui-tab-width", active.offsetWidth + "px");
  }
  function hydrate(root = document) {
    root.querySelectorAll(".ui-tabs").forEach(indicator);
    root.querySelectorAll(".ui-log").forEach(logSource);
  }
  function selectTab(button) {
    const group = button.closest(".ui-tabs"),
      tabs = [...group.querySelectorAll("button")],
      isTabs = group.getAttribute("role") === "tablist";
    tabs.forEach((tab) => {
      tab.setAttribute(
        isTabs ? "aria-selected" : "aria-pressed",
        String(tab === button),
      );
      if (isTabs) {
        tab.tabIndex = tab === button ? 0 : -1;
        document.getElementById(tab.getAttribute("aria-controls")).hidden =
          tab !== button;
      }
    });
    indicator(group);
  }
  function closeMenu(host, restore = false) {
    const trigger = host.querySelector("[data-ui-menu-trigger]");
    host.querySelector('[role="menu"]').hidden = true;
    trigger.setAttribute("aria-expanded", "false");
    if (restore) trigger.focus();
  }
  document.addEventListener("click", (event) => {
    const target = event.target.closest("button"),
      host = event.target.closest(".ui-menu-host");
    document.querySelectorAll(".ui-menu-host").forEach((other) => {
      if (other !== host) closeMenu(other);
    });
    if (!target || target.disabled) return;
    if (target.closest(".ui-tabs")) selectTab(target);
    if (target.hasAttribute("data-ui-copy")) copy(target.dataset.uiCopy);
    if (target.dataset.uiAction === "copy-log")
      copy(logSource(target.closest(".ui-log")));
    if (target.hasAttribute("data-ui-menu-trigger")) {
      const menu = host.querySelector('[role="menu"]');
      menu.hidden = !menu.hidden;
      target.setAttribute("aria-expanded", String(!menu.hidden));
      if (!menu.hidden) menu.querySelector("button:not(:disabled)")?.focus();
    } else if (target.getAttribute("role") === "menuitem")
      closeMenu(host, true);
    if (target.dataset.uiAction === "back") backWithinConsole();
  });
  document.addEventListener("input", (event) => {
    const root = event.target.closest(".ui-log");
    if (!root) return;
    if (event.target.hasAttribute("data-ui-log-filter")) {
      const term = event.target.value.toLocaleLowerCase();
      const lines = logSource(root)
        .split("\n")
        .filter((line) => line.toLocaleLowerCase().includes(term));
      root.querySelector("code").textContent = lines.join("\n");
      root.querySelector("[data-ui-log-count]").textContent = L(
        `${lines.length} matching lines`,
        `${lines.length} dòng phù hợp`,
      );
    }
    if (event.target.hasAttribute("data-ui-log-wrap"))
      root.querySelector("pre").dataset.wrap = String(event.target.checked);
  });
  document.addEventListener(
    "keydown",
    (event) => {
      const target = event.target,
        group = target.closest?.(".ui-tabs"),
        host = target.closest?.(".ui-menu-host");
      if (
        group &&
        ["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)
      ) {
        const options = [...group.querySelectorAll("button:not(:disabled)")];
        let index = options.indexOf(target);
        index =
          event.key === "Home"
            ? 0
            : event.key === "End"
              ? options.length - 1
              : (index +
                  (event.key === "ArrowRight" ? 1 : -1) +
                  options.length) %
                options.length;
        event.preventDefault();
        options[index].focus();
        selectTab(options[index]);
      }
      if (host && ["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
        event.preventDefault();
        const menu = host.querySelector('[role="menu"]');
        menu.hidden = false;
        host
          .querySelector("[data-ui-menu-trigger]")
          .setAttribute("aria-expanded", "true");
        const items = [...menu.querySelectorAll("button:not(:disabled)")];
        let i = items.indexOf(target);
        i =
          event.key === "Home"
            ? 0
            : event.key === "End"
              ? items.length - 1
              : (i + (event.key === "ArrowUp" ? -1 : 1) + items.length) %
                items.length;
        items[i]?.focus();
      }
      if (host && event.key === "Tab") closeMenu(host, true);
      if (event.key === "Escape") {
        const modal = [
          ...document.querySelectorAll("dialog[data-ui-modal][open]"),
        ].at(-1);
        if (modal) {
          event.preventDefault();
          event.stopImmediatePropagation();
          modal.close();
          return;
        }
        const popover = document.querySelector(".ui-popover:popover-open");
        if (popover) {
          event.preventDefault();
          event.stopImmediatePropagation();
          popover.hidePopover();
          document
            .querySelector('[popovertarget="' + popover.id + '"]')
            ?.focus();
          return;
        }
        if (host && !host.querySelector('[role="menu"]').hidden) {
          event.preventDefault();
          event.stopImmediatePropagation();
          closeMenu(host, true);
          return;
        }
        const tooltip = document.querySelector(
          ".ui-tooltip-host:hover,.ui-tooltip-host:focus-within",
        );
        if (tooltip) {
          event.preventDefault();
          event.stopImmediatePropagation();
          tooltip.dataset.dismissed = "true";
        }
      }
    },
    true,
  );
  document.addEventListener("focusout", (event) => {
    const host = event.target.closest?.(".ui-tooltip-host");
    if (host) delete host.dataset.dismissed;
  });
  document.addEventListener("mouseover", (event) => {
    const host = event.target.closest?.(".ui-tooltip-host");
    if (host) {
      if (!host.contains(event.relatedTarget)) delete host.dataset.dismissed;
      positionFloating(host.querySelector(".ui-tooltip"), host);
    }
  });
  function positionFloating(element, anchor) {
    if (!element || !anchor) return;
    const rect = anchor.getBoundingClientRect(),
      bounds = element.getBoundingClientRect();
    const gap = parseFloat(
      getComputedStyle(document.documentElement).getPropertyValue("--space-8"),
    );
    element.style.left =
      Math.max(gap, Math.min(rect.left, innerWidth - bounds.width - gap)) +
      "px";
    element.style.top =
      (rect.bottom + bounds.height + gap > innerHeight
        ? Math.max(gap, rect.top - bounds.height - gap)
        : rect.bottom + gap) + "px";
  }
  document.addEventListener("focusin", (event) => {
    const host = event.target.closest?.(".ui-tooltip-host");
    if (host) positionFloating(host.querySelector(".ui-tooltip"), host);
  });
  document.addEventListener(
    "toggle",
    (event) => {
      if (event.target.matches?.(".ui-popover") && event.newState === "open")
        positionFloating(
          event.target,
          document.querySelector('[popovertarget="' + event.target.id + '"]'),
        );
    },
    true,
  );
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-ui-sort]");
    if (!button) return;
    const table = button.closest("table"),
      th = button.closest("th"),
      ascending = th.getAttribute("aria-sort") !== "ascending",
      index = Number(button.dataset.uiSort);
    table
      .querySelectorAll("[aria-sort]")
      .forEach((cell) => cell.setAttribute("aria-sort", "none"));
    th.setAttribute("aria-sort", ascending ? "ascending" : "descending");
    const rows = [...table.tBodies[0].rows];
    rows.sort(
      (a, b) =>
        a.cells[index].textContent.localeCompare(
          b.cells[index].textContent,
          state.lang,
          { numeric: true },
        ) * (ascending ? 1 : -1),
    );
    rows.forEach((row) => table.tBodies[0].append(row));
  });
  window.addEventListener("resize", () => hydrate());
  window.addEventListener("hashchange", () => {
    document
      .querySelectorAll("dialog[data-ui-modal][open]")
      .forEach((el) => el.close());
    document
      .querySelectorAll(".ui-popover:popover-open")
      .forEach((el) => el.hidePopover());
    document
      .querySelectorAll(".ui-menu-host")
      .forEach((host) => closeMenu(host));
  });
  return {
    L,
    button,
    iconButton,
    field,
    input: (options) => field(options),
    select: (options) => field({ ...options, kind: "select" }),
    textarea: (options) => field({ ...options, kind: "textarea" }),
    choice,
    badge,
    avatar,
    panel,
    toolbar,
    callout,
    empty,
    skeleton,
    kbd,
    copyId,
    listRow,
    table,
    tabs,
    tooltip,
    menu,
    popover,
    breadcrumb,
    backButton,
    logViewer,
    codeBlock: logViewer,
    toast,
    copy,
    dialog,
    drawer: (options) => dialog({ ...options, kind: "drawer" }),
    hydrate,
  };
})();
