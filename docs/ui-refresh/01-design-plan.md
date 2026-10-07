# Kế hoạch thiết kế

Trạng thái: đã chốt sau khi đối chiếu baseline và qua cổng Giai đoạn 0.
Không sửa code sản phẩm trước khi bản khảo sát và kế hoạch này hoàn tất.

## 1. Thiết kế cho công việc vận hành

Người dùng cần trả lời ba câu: công việc nào cần mình, ai đang giữ việc, và bằng
chứng nào giải thích kết quả. Công việc, Sự cố và Phê duyệt vẫn là ba lối vào.
Quy trình nghiệp vụ giữ thứ tự SOP, log và cột điều khiển hiện có. Không thêm KPI
tài chính, xu hướng hoặc avatar thiếu nguồn dữ liệu.

Ba prototype tĩnh đã so sánh từ dữ liệu GET hiện tại ([ảnh sáng](../../artifacts/ui-shots/00-baseline/layout-study-light-1800.png), [ảnh tối](../../artifacts/ui-shots/00-baseline/layout-study-dark-1800.png)):

| Phương án | Điểm được | Vấn đề | Quyết định |
|---|---|---|---|
| Grid dashboard, mỗi chỉ số và vùng là card | Dễ tái dùng CSS cũ | Quá nhiều hộp, chỉ số cạnh tranh với công việc; lặp thông tin | Bỏ |
| Danh sách nén, bỏ dòng lý do lỗi | Quét nhanh, thấy nhiều hàng | Phải mở từng task mới biết lỗi; thiếu bằng chứng khi quyết định | Bỏ cho Công việc; bảng chỉ dành cho dữ liệu có cột thật |
| Không gian thao tác: dải chỉ số, toolbar, hàng có vùng rõ; detail theo bằng chứng | Phù hợp công việc dày và quy trình tuần tự | Cần component row/dialog/log rõ ràng | Chọn |

App shell tiết chế: sidebar trung tính, topbar mỏng, một đường chỉ báo chọn.
Trang Công việc có một dải bốn chỉ số; “Chờ bạn” nhấn bằng trọng lượng chữ và nhãn
hành động, không tô cả bốn thẻ. Toolbar nằm trên luồng cuộn của trang.

## 2. Font đã thử bằng dữ liệu chữ Việt

Đã tải cục bộ Fontsource 5.3.0 cho Inter, Geist, Be Vietnam Pro, JetBrains Mono,
Geist Mono và IBM Plex Mono. Có ảnh thật và kiểm cmap:

- [So sánh desktop](../../artifacts/ui-shots/00-baseline/font-study/specimens-1440.png)
- [So sánh mobile](../../artifacts/ui-shots/00-baseline/font-study/specimens-390.png)
- [Kiểm tra ký tự](../../artifacts/ui-shots/00-baseline/font-study/coverage.json)
- [Phiên bản, URL và SHA-256](../../artifacts/ui-shots/00-baseline/font-study/sources.json)

**Chọn Inter cho UI, JetBrains Mono cho ID/log.** Cả sáu ứng viên chứa đủ các
ký tự trong ba tiêu đề và chuỗi `ệ ữ ặ ẫ ỗ ử`. Trong specimen 390px, Inter và
Geist giữ tiêu đề thử 20px trên một dòng; Be Vietnam Pro xuống hai dòng. Đây là
quan sát trên specimen, không phải kết luận rằng Be Vietnam Pro không phù hợp
tiếng Việt. Inter hợp vùng dữ liệu dày; hình dấu vẫn rõ ở 13/14px. JetBrains Mono
cho nhịp ký tự và số rõ ở log 12px; không dùng mono cho thân bài.

Phát hành chỉ hai họ, weight 400/500/600, normal WOFF2 latin/latin-ext/vietnamese.
`font-display: swap`; preload subset cần cho màn đầu, không preload cả sáu font.
Fallback được hiệu chỉnh bằng metric font thật và kiểm layout shift. Thang cỡ:
12/13/14/16/20/24/32; body 1.5–1.6, heading ít nhất 1.25; số liệu tabular.

Nguồn gốc: [Inter](https://github.com/rsms/inter),
[Be Vietnam Pro](https://github.com/bettergui/BeVietnamPro),
[Geist](https://github.com/vercel/geist-font),
[JetBrains Mono](https://github.com/JetBrains/JetBrainsMono),
[IBM Plex](https://github.com/IBM/plex),
[Fontsource về self-host](https://fontsource.org/docs/getting-started/introduction).
Giấy phép của các bản đã tải được giữ nguyên cạnh file, không dựa vào giấy phép
của website để suy ra giấy phép font.

## 3. Tokens và CSS

Thứ tự bắt buộc: `@layer reset, tokens, base, layout, components, pages, utilities`.
Không thêm `!important`; loại sáu rule cũ theo đợt chuyển caller, không xóa `[hidden]`
trước khi xác minh mọi display rule đã tôn trọng thuộc tính đó.

| Tầng | Token | Quy tắc |
|---|---|---|
| Primitive | `--neutral-*`, `--navy-*`, `--teal-*`... | Chỉ file tokens được dùng màu literal |
| Semantic | `--color-bg`, `--color-surface-1/2/3`, `--color-text`, `--color-text-muted` | Nền/text khác nhau giữa sáng và tối |
| Semantic | `--color-border-subtle`, `--color-border-control`, `--color-focus` | Viền chia vùng nhẹ; viền điều khiển và focus đạt 3:1 |
| Semantic | `--color-accent`, `--color-accent-hover/soft/border`, `--color-on-accent` | Sáu palette × hai mode; nút đạt 4.5:1 |
| Semantic | `--color-success/warning/danger/info/neutral-{fg,bg,border}` | Độc lập palette, đi kèm icon và chữ |
| Component | `--btn-height`, `--row-min-height`, `--field-padding` | Density thay token, không nhân đôi component CSS |
| Layout | `--sidebar-width`, `--content-max`, `--toolbar-offset`, `--z-*` | Không dùng z-index tùy ý ở từng page |

Giữ alias tên biến hiện có trong thời gian caller chưa chuyển; component mới chỉ
đọc semantic hoặc component token. Cuối mỗi đợt chuyển, bỏ alias không còn caller.
`UIPalettes` vẫn là nguồn màu chuẩn dùng bởi preferences và bộ kiểm tương phản.

Nền sáng: trắng trên xám ấm rất nhẹ; phân lớp bằng đường kẻ và bóng thấp.
Nền tối: charcoal nhiều cấp sáng, phân lớp bằng bề mặt và viền; không dùng đen
thuần. Chỉ pha sắc nền khoảng 1–3%; Graphite hoàn toàn trung tính, primary có
tương phản đen/trắng. Focus là một ring 2px và offset 2px, không thêm viền chọn
chồng lên sidebar.

Điểm xuất phát cụ thể cho neutral tokens (đề xuất, chưa đưa vào sản phẩm):

| Semantic token | Light | Dark |
|---|---|---|
| `--color-bg` | `#f6f6f4` | `#141619` |
| `--color-surface-1` | `#ffffff` | `#191c20` |
| `--color-surface-2` | `#f0f1ef` | `#20242a` |
| `--color-text` | `#20252b` | `#e7e9ed` |
| `--color-text-muted` | `#606873` | `#a6afb9` |
| `--color-border-subtle` | `#d2d6da` | `#343a43` |
| `--color-border-control` | `#858c96` | `#697482` |

Viền chia vùng không thay thế viền điều khiển. Giai đoạn 1 sẽ đo cặp thực tế
trên mỗi surface, điều chỉnh bằng mắt và kiểm lại cả 12 tổ hợp; bảng này không
phải chứng nhận tương phản. Accent/status có fg/bg/border riêng theo mode.

Spacing: 2/4/8/12/16/20/24/32/40/56. Radius: chip nhỏ 6, button/input 8, panel 12,
dialog/popover 14, avatar tròn. Hàng compact tối thiểu 36px, comfortable 56px;
metadata/lỗi được phép tăng chiều cao. Thiết bị cảm ứng giữ target ít nhất 44px.

## 4. Ranh giới mã và tương thích

Giữ `page.py` ghép tài sản đáng tin cậy, không thêm bundler. CSS có manifest rõ
thứ tự layer; component CSS ngắn theo trách nhiệm. JS component hỗ trợ markup,
focus, sao chép, dialog, toast và trạng thái; không sở hữu dữ liệu nghiệp vụ.
`work.js` và các renderer tiếp tục gọi API/controller hiện có.

Đã cân nhắc ES modules độc lập và một framework SPA. Cả hai làm thay đổi cách
nạp trang, vòng đời auth/static và Node verifier nhiều hơn nhu cầu UI. Chọn ghép
source hiện có; chỉ bổ sung route static font/icon với allowlist/path cố định nếu
cần, ghi rõ trong báo cáo. Không thay API nghiệp vụ.

Giữ `?org=`, hash routes, escape dữ liệu, chống phản hồi cũ ghi đè route mới,
polling draft theo stage/hash/revision và quyền server. Giữ các khóa
`onx-ui-preferences-v1`, `ao-lang-v1`, `ao-sidebar-collapsed`,
`ao-notif-seen-v2:<org>` và session token `ao_token`.
Trước khi đổi class/id/data attribute: `rg` toàn repo, chuyển caller và verifier
cùng commit hoặc giữ alias có thời hạn; không “fix” test bằng bỏ assertion.

Script head đọc preferences an toàn, áp thuộc tính và color-scheme trước CSS;
meta theme-color theo mode. Không transition trong khung đầu hoặc đổi theme.

## 5. Component và trạng thái

- Điều khiển: Button/IconButton, Input/Select/Textarea, Checkbox/Radio/Switch,
  SegmentedControl/Tabs; selected/hover/press/focus/disabled/loading/error.
- Dữ liệu: StatusPill, Avatar chữ đầu từ tên thật, CopyableId, ListRow/TableRow,
  Panel/Section/Toolbar, Breadcrumb/BackButton, CodeBlock/LogViewer.
- Lớp phủ: Dialog, Drawer/BottomSheet, Popover/Menu, Tooltip, Toast, Kbd.
- Trạng thái: Skeleton, EmptyState, NoResults, ErrorState, Callout.

Map status lấy từ enum/API trong audit; không dùng một màu accent cho tất cả.
Status mới không biết vẫn hiện nguyên tên trong neutral pill; không suy đoán
thành “hoàn tất”. Style guide local-only hiển thị mọi variant và các trạng thái
dài/ngắn, đủ light/dark/palette/density. Không giả làm màn nghiệp vụ.

## 6. Công việc, log và hiệu năng

Hàng task có vùng trạng thái, tên, owner và chờ ai; meta tách ID/type/children.
Tên là liên kết mở đúng task, ID có nút chép riêng, không lồng button vào link.
Lỗi có tóm tắt một dòng, mở toàn bộ và chép; bỏ `slice(0,120)` làm mất thông tin.

Giữ cách lấy dữ liệu hiện tại ở bước UI, thêm `content-visibility:auto` và
`contain-intrinsic-size` cho hàng khi phù hợp. Không cuộn trong card. Tìm kiếm
có debounce ngắn, giữ focus và tránh rebuild khi snapshot không đổi. Đo lại với
1.188 hàng thật; nếu không đạt, dùng tải thêm ở client với tổng đếm đúng, không
đổi contract API. Không chọn virtualization từ đầu vì chiều cao lỗi mở rộng,
tìm kiếm trình duyệt và điều hướng bàn phím dễ mất tính đúng.

GET thất bại không được biến thành bốn số 0. Baseline cho thấy renderGive bắt
lỗi rồi tiếp tục với object rỗng. Chuyển sang ErrorState hoặc giữ snapshot cũ
có nhãn chưa cập nhật; phân biệt empty thật, no-results và dữ liệu chưa tải.

Detail giữ năm section `overview/steps/assignment/decisions/log`; log có tìm kiếm,
chép, xuống dòng tùy chọn, phân trang đúng offset. Quy trình giữ thứ tự stage,
mọi link đi tới task/stage/evidence đã bấm. Approval dialog mô tả hệ quả nhưng
quyết định và quyền vẫn do API hiện tại xử lý.

## 7. Chuyển động, mobile, bàn phím

Tokens: 80/140/200/320/480ms; easing chuẩn `cubic-bezier(.2,0,0,1)`, spring nhẹ
có fallback. Một khoảnh khắc duy nhất: dải chỉ số và tối đa 12 hàng đầu vào lần
đầu mỗi phiên, tổng không quá 400ms. Counter kết thúc dưới 600ms và không chạy
lại khi poll. Chấm kết nối chỉ thở khi stream thật hoạt động.

Các phần còn lại: sliding indicator, disclosure grid 0fr/1fr, lớp phủ opacity
và dịch 4–8px, chuyển trang tối đa 6px khoảng 160ms. Reduced motion bỏ transform,
scale, shimmer, counter, chỉ fade dưới 100ms. Không animate hàng nghìn hàng.

Mobile sidebar offcanvas, dialog bottom sheet, palette fullscreen; danh sách 2–3
dòng, bảng chuyển cấu trúc xếp chồng. Test 360/390/640/768/1024/1280/1440/1536/1920,
safe area, input 16px và zoom 200%. Command palette Ctrl/Cmd+K hỗ trợ focus trap,
restore, combobox/listbox, active descendant; `/`, `j/k`, Enter, `?` không chạy
trong input/editor. Esc ưu tiên đóng lớp phủ, sau đó giữ hành vi quay lại.

## 8. Thứ tự và cổng kiểm tra

| Giai đoạn | Thay đổi | Bằng chứng bắt buộc |
|---|---|---|
| 0 | Khảo sát, ảnh, font, kế hoạch | Audit/plan hoàn tất, baseline tests đã chạy |
| 1 | Layers/tokens/font/icons/density/motion/prepaint | App chạy, 12 palette đạt contrast, font local |
| 2 | Component + style guide local-only | Đủ variant/state, axe không serious/critical |
| 3 | Shell và preferences menu | Before/after, keyboard, old keys/deep links |
| 4 | Work + task detail + create disclosure | 1.188 rows/perf, fixture states, log và copy |
| 5 | Mỗi trang còn lại, mỗi trang một commit | Component chung, không đổi nghiệp vụ |
| 6 | Palette/shortcuts/toast/mobile/a11y | Matrix đầy đủ, 360px/zoom/reduced motion |
| 7 | Dọn CSS, tự phản biện, hướng dẫn, báo cáo | Toàn bộ checklist brief, tests và lint xanh |

Baseline ghi lỗi hiện hữu để có mốc thật. Strict gate cuối phải thất bại khi có
lỗi; không dùng exit code baseline để tuyên bố giao diện đã đạt. Bộ kiểm sẽ bổ
sung style guide, clipping, keyboard, prepaint, zoom và so sánh hiệu năng khi
các tính năng tương ứng được triển khai. Chưa chứng nhận những phần chưa chạy.

## 9. Tự phản biện và điều chỉnh

- Bỏ eyebrow “O-NEXUS” lặp trên mọi màn và chữ nhóm in hoa: không giúp chọn việc.
- Bỏ đề xuất mỗi KPI một card có icon lớn; dùng dải thống nhất và đếm có ngữ cảnh.
- Bỏ nền accent trên toàn sidebar và mọi viền; giữ accent ở hành động/focus/chọn.
- Không thêm trang dashboard hoặc tab chưa có API. Thư viện và dữ liệu nghiệp vụ
  rỗng phải giải thích đầu vào còn thiếu, không thêm biểu đồ mẫu.
- Không thiết kế mọi màn thành bảng. Quy trình và log cần cấu trúc tuần tự khác
  với danh sách task nhưng dùng cùng typography, surface và component.
- Phân biệt “settled” với “completed” trong audit trước khi chốt copy, tránh một
  nhãn đẹp làm người vận hành hiểu sai task thất bại đã thành công.
