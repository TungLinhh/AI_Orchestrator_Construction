/* Hidden local specimen route. All records below are explicitly labelled examples. */
const UIStyleguide = (() => {
  const local = ["localhost", "127.0.0.1", "[::1]"].includes(
    location.hostname || new URL(location.href).hostname,
  );
  const sampleLog =
    "Mẫu / Sample — không phải nhật ký vận hành\nệ ữ ặ ẫ ỗ ử — Tuyển kỹ sư MEP đến onboarding\nĐề xuất lựa chọn sau hai vòng\nPhê duyệt: chờ người phụ trách\n" +
    "Dòng dài để kiểm tra xuống hàng: ".repeat(12);
  const specimenId = "sample_mep_0123456789_abcdefgh";
  const L = UI.L;
  const section = (title, content) =>
    `<section class="ui-guide-section"><h2>${esc(title)}</h2>${content}</section>`;
  const group = (content, label) => UI.toolbar(content, label);
  function renderGuide() {
    const B = UI.button;
    const buttons = ["primary", "secondary", "ghost", "danger"]
      .map((variant) =>
        group(
          ["sm", "md", "lg"]
            .map((size) =>
              B({
                label: variant + " · " + size,
                variant,
                size,
                icon: variant === "danger" ? "x" : "plus",
              }),
            )
            .join("") +
            B({
              label: L("Disabled", "Không khả dụng"),
              variant,
              disabled: true,
            }) +
            B({ label: L("Processing", "Đang xử lý"), variant, loading: true }),
          variant,
        ),
      )
      .join("");
    const forms = group(
      UI.input({
        label: L("Job title", "Tên vị trí"),
        value: L("MEP engineer", "Kỹ sư MEP"),
        hint: L("Sample field", "Ô nhập mẫu"),
        required: true,
      }) +
        UI.input({
          label: L("Invalid email", "Email chưa hợp lệ"),
          type: "email",
          value: "sample",
          error: L(
            "Enter a valid email address.",
            "Nhập địa chỉ email hợp lệ.",
          ),
        }) +
        UI.input({
          label: L("Read only", "Chỉ đọc"),
          value: "MEP",
          readonly: true,
        }) +
        UI.input({
          label: L("Disabled field", "Ô không khả dụng"),
          value: "MEP",
          disabled: true,
        }) +
        UI.select({
          label: L("Discipline", "Chuyên môn"),
          value: "mechanical",
          options: [
            { value: "mechanical", label: L("Mechanical", "Cơ khí") },
            { value: "electrical", label: L("Electrical", "Điện") },
          ],
        }) +
        UI.select({
          label: L("Disabled select", "Danh sách không khả dụng"),
          disabled: true,
          options: [{ value: "sample", label: L("Sample", "Mẫu") }],
        }) +
        UI.textarea({
          label: L("Notes", "Ghi chú"),
          value:
            "ệ ữ ặ ẫ ỗ ử — " +
            L("Sample recruitment brief", "Brief tuyển dụng mẫu"),
        }) +
        UI.textarea({
          label: L("Invalid notes", "Ghi chú cần bổ sung"),
          error: L(
            "Describe the responsibilities.",
            "Mô tả trách nhiệm của vị trí.",
          ),
        }),
      L("Fields", "Ô nhập"),
    );
    const choices = group(
      UI.choice({
        label: L("Selected checkbox", "Checkbox được chọn"),
        checked: true,
      }) +
        UI.choice({ label: L("Unchecked checkbox", "Checkbox chưa chọn") }) +
        UI.choice({
          label: L("Disabled checkbox", "Checkbox không khả dụng"),
          checked: true,
          disabled: true,
        }) +
        `<fieldset class="ui-guide-radio"><legend>${L("One choice", "Chọn một mục")}</legend>${UI.choice({ label: L("Option A", "Mục A"), name: "sample-radio", kind: "radio", checked: true })}${UI.choice({ label: L("Option B", "Mục B"), name: "sample-radio", kind: "radio" })}${UI.choice({ label: L("Unavailable option", "Mục không khả dụng"), name: "sample-radio", kind: "radio", disabled: true })}</fieldset>` +
        UI.choice({
          label: L("Switch on", "Công tắc bật"),
          kind: "switch",
          checked: true,
        }) +
        UI.choice({ label: L("Switch off", "Công tắc tắt"), kind: "switch" }) +
        UI.choice({
          label: L("Disabled switch", "Công tắc không khả dụng"),
          kind: "switch",
          disabled: true,
        }),
      L("Choices", "Lựa chọn"),
    );
    const statuses = group(
      ["neutral", "info", "success", "warning", "danger"]
        .map((tone, i) =>
          UI.badge(
            [
              L("Waiting", "Đang chờ"),
              L("Running", "Đang làm"),
              L("Completed", "Hoàn tất"),
              L("Needs review", "Cần xem xét"),
              L("Failed", "Thất bại"),
            ][i],
            tone,
          ),
        )
        .join("") +
        UI.avatar("HR Agent") +
        UI.avatar("Executive Agent"),
      L("Status and initials", "Trạng thái và chữ viết tắt"),
    );
    const tabs =
      UI.tabs({
        label: L("Sample tabs", "Tab mẫu"),
        items: [
          {
            label: L("Overview", "Tổng quan"),
            content: "ệ ữ ặ ẫ ỗ ử — " + L("Sample overview", "Tổng quan mẫu"),
          },
          {
            label: L("Results", "Kết quả"),
            content: UI.empty({
              title: L("No sample results", "Chưa có kết quả mẫu"),
              description: L(
                "Switch back to the overview.",
                "Chuyển lại tab tổng quan.",
              ),
              kind: "empty",
            }),
          },
          { label: L("Disabled", "Chưa mở"), disabled: true },
        ],
      }) +
      UI.tabs({
        label: L("Sample segmented control", "Lựa chọn dạng nhóm"),
        segmented: true,
        items: [
          { label: L("All", "Tất cả") },
          { label: L("Waiting", "Đang chờ") },
          { label: L("Disabled", "Chưa mở"), disabled: true },
        ],
      });
    const rows =
      UI.listRow({
        title: L("Sample: recruit an MEP engineer", "Mẫu: tuyển kỹ sư MEP"),
        meta: "HR Agent · " + L("Sample only", "Chỉ là mẫu"),
        status: L("Needs review", "Cần xem xét"),
        statusTone: "warning",
        url: "#/_styleguide",
        actions: B({ label: L("View sample", "Xem mẫu"), action: "dialog" }),
      }) +
      UI.listRow({
        title: L("Sample selected row", "Hàng mẫu đang chọn"),
        meta: "ệ ữ ặ ẫ ỗ ử",
        status: L("Failed", "Thất bại"),
        statusTone: "danger",
        selected: true,
        error: L(
          "Sample error: missing evidence. Add the required document.",
          "Lỗi mẫu: thiếu bằng chứng. Bổ sung tài liệu cần thiết.",
        ),
        actions: B({
          label: L("Retry sample", "Thử lại mẫu"),
          variant: "ghost",
          action: "toast",
        }),
      });
    const table = UI.table({
      label: L("Sample sortable records", "Bản ghi mẫu có thể sắp xếp"),
      columns: [
        { label: L("Record", "Bản ghi"), sortable: true },
        { label: L("Status", "Trạng thái") },
        { label: L("Score", "Điểm"), sortable: true },
      ],
      rows: [
        {
          cells: [
            "MEP sample B",
            UI.badge(L("Waiting", "Đang chờ"), "neutral"),
            "72",
          ],
        },
        {
          selected: true,
          cells: [
            "MEP sample A",
            UI.badge(L("Completed", "Hoàn tất"), "success"),
            "96",
          ],
        },
        {
          cells: [
            "MEP sample C",
            UI.badge(L("Failed", "Thất bại"), "danger"),
            "9",
          ],
        },
      ],
    });
    const overlays = group(
      B({ label: L("Open dialog", "Mở dialog"), action: "dialog" }) +
        B({ label: L("Open drawer", "Mở drawer"), action: "drawer" }) +
        B({
          label: L("Confirmation error", "Lỗi khi xác nhận"),
          action: "dialog-error",
        }) +
        UI.menu({
          label: L("Sample menu", "Menu mẫu"),
          items: [
            {
              label: L("Copy sample ID", "Sao chép mã mẫu"),
              icon: "copy",
              action: "copy-sample",
            },
            { label: L("Unavailable", "Không khả dụng"), disabled: true },
            {
              label: L("Show notification", "Hiện thông báo"),
              icon: "bell",
              action: "toast",
            },
          ],
        }) +
        UI.popover({
          label: L("Sample popover", "Popover mẫu"),
          content: UI.input({
            label: L("Sample note", "Ghi chú mẫu"),
            value: L("Local sample", "Mẫu cục bộ"),
          }),
        }) +
        UI.tooltip(
          UI.iconButton({
            label: L("Copy sample", "Sao chép mẫu"),
            icon: "copy",
            action: "copy-sample",
          }),
          L("Copies only the sample ID.", "Chỉ sao chép mã mẫu."),
        ),
      L("Overlays", "Lớp nổi"),
    );
    const emptyStates =
      UI.empty({
        title: L("No sample work", "Chưa có công việc mẫu"),
        description: L(
          "Create a local example to see the component.",
          "Tạo ví dụ cục bộ để xem component.",
        ),
        actions: B({
          label: L("Open example", "Mở ví dụ"),
          variant: "primary",
          action: "dialog",
        }),
      }) +
      UI.empty({
        title: L("No matching sample", "Không có mẫu phù hợp"),
        description: L("Clear the sample filter.", "Xóa bộ lọc mẫu."),
        kind: "no-results",
        actions: B({
          label: L("Clear sample filter", "Xóa bộ lọc mẫu"),
          action: "toast",
        }),
      }) +
      UI.empty({
        title: L("Sample load error", "Lỗi tải mẫu"),
        description: L(
          "Sample state only. You can try again.",
          "Chỉ là trạng thái mẫu. Bạn có thể thử lại.",
        ),
        kind: "error",
        actions: B({ label: L("Try again", "Thử lại"), action: "toast" }),
      }) +
      UI.skeleton();
    return `<div id="uiStyleguide" class="ui-guide" data-ui-guide lang="${state.lang}"><header class="ui-guide-heading"><h1>${L("Component style guide", "Style guide component")}</h1><p>${L("Local examples only. No workflow, approval or task is created by this page.", "Chỉ là ví dụ cục bộ. Trang này không tạo workflow, phê duyệt hoặc task.")}</p></header><div class="ui-guide-controls">${UI.select({ label: L("Palette", "Bảng màu"), name: "guide-palette", value: UIPreferences.get().palette, options: UIPalettes.items.map((p) => ({ value: p.id, label: state.lang === "vi" ? p.vi : p.en })) })}${UI.select(
      {
        label: L("Mode", "Chế độ"),
        name: "guide-mode",
        value: document.documentElement.dataset.colorMode,
        options: [
          { value: "light", label: L("Light", "Sáng") },
          { value: "dark", label: L("Dark", "Tối") },
        ],
      },
    )}${UI.select({
      label: L("Density", "Mật độ"),
      name: "guide-density",
      value: UIPreferences.get().density,
      options: [
        { value: "comfortable", label: "Comfortable" },
        { value: "compact", label: "Compact" },
      ],
    })}${B({ label: state.lang === "vi" ? "English" : "Tiếng Việt", action: "language" })}</div><p class="ui-muted">${L("Controls preview this tab only; saved preferences remain unchanged. Hover, press and focus are interactive. Disabled controls cannot run actions.", "Các lựa chọn chỉ xem trước trong tab này; không ghi cài đặt đã lưu. Hover, nhấn và focus có thể thao tác thật. Nút không khả dụng không chạy hành động.")}</p>${section(L("Buttons", "Nút"), buttons + B({ label: L("Selected action", "Hành động đã chọn"), pressed: true }) + UI.iconButton({ label: L("Settings", "Cài đặt"), icon: "settings" }) + UI.iconButton({ label: L("Loading action", "Hành động đang tải"), icon: "settings", loading: true }) + UI.iconButton({ label: L("Disabled icon action", "Nút icon không khả dụng"), icon: "settings", disabled: true }))}${section(L("Fields and choices", "Ô nhập và lựa chọn"), forms + choices)}${section(L("Status, tabs and shortcuts", "Trạng thái, tabs và phím tắt"), statuses + tabs + UI.kbd("Esc") + " " + UI.kbd("Ctrl + K"))}${section(L("Section and toolbar", "Section và toolbar"), UI.panel({ title: L("Sample section", "Section mẫu"), description: L("One object with its own actions.", "Một đối tượng có hành động riêng."), actions: B({ label: L("Sample action", "Hành động mẫu"), variant: "ghost", action: "toast" }), content: UI.toolbar(B({ label: L("Sample primary action", "Hành động chính mẫu"), variant: "primary", action: "toast" }) + UI.backButton(), L("Sample toolbar", "Toolbar mẫu")) }))}${section(L("Sample metric filters", "Bộ lọc chỉ số mẫu"), UI.metrics({label:L("Sample metrics", "Chỉ số mẫu"),items:[12,7,0,5].map((value,i)=>({key:String(i),label:L("Sample ","Mẫu ")+(i+1),value,note:L("UI example only", "Ví dụ giao diện"),selected:i===0,emphasized:i===0}))}))}${section(L("Rows and table", "Hàng và bảng"), rows + table)}${section(L("Overlays and notifications", "Lớp nổi và thông báo"), overlays + group(["info", "success", "warning", "danger"].map((status) => B({ label: L("Notify: ", "Thông báo: ") + status, action: "toast-" + status })).join(""), L("Notification examples", "Ví dụ thông báo")))}${section(L("Callouts and states", "Callout và trạng thái"), ["info", "success", "warning", "danger"].map((status) => UI.callout({ title: L("Sample: ", "Mẫu: ") + status, description: L("This message explains a sample state and the next action.", "Thông báo giải thích trạng thái mẫu và hành động tiếp theo."), status })).join("") + emptyStates)}${section(L("Breadcrumb and copyable ID", "Đường dẫn và mã sao chép"), UI.breadcrumb([{ label: L("Work", "Công việc"), href: "#/give" }, { label: L("Sample", "Mẫu") }]) + UI.copyId(specimenId))}${section(L("Code and log viewer", "Code và log viewer"), UI.logViewer({ label: L("Sample log", "Nhật ký mẫu"), text: sampleLog }))}</div>`;
  }
  let savedPreview;
  function mount() {
    if (!local) return;
    savedPreview ||= UIPreferences.get();
    document.getElementById("managementContent").innerHTML = renderGuide();
    UI.hydrate(document.getElementById("uiStyleguide"));
  }
  if (local) {
    ROUTES._styleguide = { label: "Style guide" };
    MANAGEMENT_VIEWS.set("_styleguide", mount);
  }
  document.addEventListener("change", (event) => {
    if (!event.target.closest("[data-ui-guide]")) return;
    const names = {
        "guide-palette": "palette",
        "guide-mode": "theme",
        "guide-density": "density",
      },
      key = names[event.target.name];
    if (key) {
      UIPreferences.preview({
        ...UIPreferences.get(),
        [key]: event.target.value,
      });
      UI.hydrate();
    }
  });
  document.addEventListener("click", (event) => {
    const target = event.target.closest("[data-ui-action]");
    if (!target?.closest("[data-ui-guide]") || target.disabled) return;
    const action = target.dataset.uiAction;
    if (action === "language") {
      langSet(state.lang === "vi" ? "en" : "vi");
      mount();
    }
    if (action === "copy-sample") UI.copy(specimenId);
    if (action?.startsWith("toast"))
      UI.toast(
        L(
          "Sample notification: action completed.",
          "Thông báo mẫu: hành động đã hoàn tất.",
        ),
        { status: action.split("-")[1] || "info" },
      );
    if (["dialog", "drawer", "dialog-error"].includes(action))
      UI.dialog({
        title: L("Sample confirmation", "Xác nhận mẫu"),
        description: L(
          "This dialog demonstrates focus and confirmation. It does not approve a real task.",
          "Dialog minh họa focus và xác nhận. Không phê duyệt task thật.",
        ),
        content: UI.input({
          label: L("Sample feedback", "Feedback mẫu"),
          value: "ệ ữ ặ ẫ ỗ ử",
        }),
        kind: action === "drawer" ? "drawer" : "dialog",
        confirmLabel: L("Confirm sample", "Xác nhận mẫu"),
        onConfirm: async () => {
          if (action === "dialog-error")
            throw Error(
              L(
                "Sample failure. Review the input and try again.",
                "Lỗi mẫu. Kiểm tra nội dung và thử lại.",
              ),
            );
          UI.toast(L("Sample confirmed", "Đã xác nhận mẫu"), {
            status: "success",
          });
        },
      });
  });
  window.addEventListener("hashchange", () => {
    if (parseHash().name !== "_styleguide" && savedPreview) {
      UIPreferences.preview(savedPreview);
      savedPreview = null;
    }
  });
  return { local, mount };
})();
