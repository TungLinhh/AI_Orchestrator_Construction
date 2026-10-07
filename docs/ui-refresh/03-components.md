# API component — Giai đoạn 2

Source: `web/components.js` và `components.css`. `UI` dùng chung với các renderer
hiện có; không bundler, dependency sản phẩm mới hoặc thay API nghiệp vụ.
`legacy-base.css` chứa element defaults đã chuyển từ CSS legacy xuống layer
`base`, để không ghi đè màu/weight/error của component ở layer `components`.

## Nguyên tắc gọi

Text/label/value/error được escape. `content`, `actions`, `item.content` và
`row.cells` là **markup đáng tin cậy do renderer tạo**, không phải chuỗi trả về
trực tiếp từ model/API. Escape dữ liệu bằng `esc()` trước khi đưa vào vị trí này;
hoặc tạo markup bằng helper UI. Không dùng nội dung model làm `innerHTML`.

Hàm trả HTML dùng `setHTML()` của app hoặc host renderer. Gọi `UI.hydrate(host)`
sau khi đưa HTML vào DOM để đo sliding indicator và ghi source log. Event được
delegate với namespace `data-ui-*`; không cần bind lại trên từng hàng.
Component không fetch dữ liệu, không tự duyệt hồ sơ và không thay controller.

```js
UI.button({label: tr('save', 'Save'), variant: 'primary', icon: 'check'});
UI.input({label: 'Tên vị trí', name: 'title', value: brief.title, required: true});
UI.badge('Cần xét duyệt', 'warning');
UI.panel({title: 'Kết quả', content: UI.listRow({title: result.title, meta: result.owner})});
```

Chuỗi trang sản phẩm phải dùng key VI/EN của `tr()`; component chỉ cung cấp các
chuỗi hành vi chung qua `UI.L(en, vi)`. Không lấy chữ kỹ thuật trong style guide
làm copy sản phẩm. Label của control và title của section/dialog là bắt buộc.

## Các helper

| API | Hành vi / trạng thái |
|---|---|
| `button`, `iconButton` | primary/secondary/ghost/danger; sm/md/lg; icon, pressed, disabled, loading. Loading dùng disabled + aria-busy; icon-only cần label. |
| `input`, `select`, `textarea` | value, name, hint, error/aria-invalid, disabled, required; readonly cho input/textarea. Select nhận options `{value,label}`. ID/help liên kết tự sinh. |
| `choice` | kind checkbox/radio/switch; checked, disabled. Radio cùng nhóm phải dùng cùng name; dùng fieldset/legend mô tả nhóm. |
| `tabs` | items `{label,content,disabled}`; roving tabindex, Left/Right/Home/End, bỏ mục disabled. `segmented:true` dùng group + aria-pressed. Indicator đo kích thước thật. |
| `badge`, `avatar`, `kbd` | Trạng thái có chữ + icon, palette-independent tone; avatar từ initials tên thật; kbd cho gợi ý phím. |
| `panel`, `toolbar`, `callout`, `empty`, `skeleton` | Vùng/nội dung/hành động; callout tone; empty/no-results/error có hướng dẫn; loading có aria-busy/status. |
| `listRow`, `table` | Hàng không lồng control trong link; lỗi nhiều dòng, trạng thái/selected. Table có caption, th/scope, aria-sort và sort DOM bằng locale/numeric. Chỉ dùng sort cục bộ khi đã có toàn bộ dữ liệu; phân trang server do renderer sở hữu. |
| `menu`, `popover`, `tooltip` | Menu có disabled items/Arrow/Home/End/Tab/Escape/outside dismiss; native popover; tooltip khi hover/focus, Escape đóng, định vị trong viewport. Menu action do renderer xử lý `data-ui-action`. |
| `dialog`, `drawer` | Native modal, title/description, trap Tab, Escape/cancel, trả focus, scroll lock; nested modal đóng từ trên xuống. Drawer và dialog thành bottom sheet ở mobile. |
| `toast`, `copy`, `copyId` | Toast dismissible/live, pause timer khi hover/focus. Copy báo lỗi nếu clipboard thất bại; không báo thành công giả. ID chỉ rút gọn phần hiển thị, sao chép đủ mã. |
| `breadcrumb`, `backButton` | Chỉ link hash/relative an toàn; breadcrumb có label riêng cho mỗi landmark. Back dùng navigation hiện có. |
| `logViewer`, `codeBlock` | Text được escape, mono 12px, lọc dòng, tùy chọn wrap/cuộn ngang. Copy luôn chép **toàn bộ source**, kể cả đang lọc; log source được lưu theo DOM instance bằng WeakMap. |

Tone hợp lệ: neutral/info/success/warning/danger. Button variant/size và icon đều
được whitelist. Dữ liệu tự do không được trở thành tên class, SVG hoặc raw link.

## Dialog và callback

```js
const modal = UI.dialog({
  title: 'Xác nhận', description: 'Giải thích hệ quả của hành động.',
  content: UI.textarea({label: 'Ghi chú', name: 'note'}),
  confirmLabel: 'Xác nhận',
  onConfirm: async (element) => {
    // Renderer gọi API/controller hiện có tại đây; kiểm quyền ở server.
    // Throw lỗi để giữ input, mở lại nút và hiển thị lỗi ngay trong modal.
  },
});
// modal.element / modal.close(); không tự hủy tác vụ backend khi đóng modal.
```

Callback đang chờ khóa nút xác nhận, có aria-busy. Lỗi hiển thị trong top layer
để người dùng thấy và thử lại; toast ngoài body có thể bị modal che/inert nên
không dùng nó làm kênh lỗi duy nhất. Renderer chịu trách nhiệm xử lý kết quả
async nếu người dùng đã đóng modal; UI không hủy API thay renderer.

## Style guide và kiểm chứng

Mở `http://127.0.0.1:8100/api/v1/ui?org=ORG_ID#/_styleguide`. Route chỉ đăng ký
trên hostname loopback, không ở sidebar; host remote không có route này.
Đây là điều kiện hiển thị công cụ local, không thay cơ chế authentication.
Các dữ liệu/điểm/log đều được ghi rõ là mẫu và không gọi business write.

Palette/mode/density ở guide chỉ preview; rời route khôi phục lựa chọn trước
đó, không ghi khóa preferences. VI/EN vẫn dùng công tắc ngôn ngữ đã có.

```sh
node scripts/ui-components-contract.mjs
uv run --with playwright python scripts/ui-components-gate.py --org ORG_ID --chromium CHROMIUM_PATH
```

Gate chặn host ngoài/phương thức ghi, chạy axe-core cục bộ trên 24 tổ hợp và
trạng thái mở của overlays; kiểm bàn phím, clipboard thật trong context test,
log/sort, lỗi confirmation giữ input, VI/EN, mobile, reduced-motion.
Receipt/ảnh: `artifacts/ui-shots/02-components/`. Không suy rộng cổng style guide
thành a11y toàn app, không chứng nhận Safari/Firefox hoặc zoom 200% đầy đủ.

Các trang legacy được chuyển dần từ GĐ3–5. Tooltip/popover thêm vào shell cần
kiểm lại anchoring lúc scroll và resize của layout thực; code block dài vẫn
được cuộn ngang hoặc wrap, không tự cắt nội dung.
