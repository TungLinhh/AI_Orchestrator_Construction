# Tiến độ UI refresh

## Phạm vi và điểm bắt đầu

- Brief: `C:/Users/vutun/Downloads/ui-refresh-brief.md`, đã đọc toàn bộ.
- Nhánh: `ui/refresh`; điểm gốc: `68e4718ab9deacd220cdd354fed21f85b07035eb`.
- Working tree sạch trước khi bắt đầu. Không reset, dọn checkout hoặc sửa dữ liệu vận hành.
- **Giai đoạn 0 hoàn tất**: khảo sát, baseline thật, thử font và kế hoạch thiết kế.
- **Giai đoạn 1 hoàn tất**: foundation và browser gate PASS, đã qua kiểm chứng
  cuối. Thay đổi frontend và thêm GET font public; không đổi API nghiệp vụ/DB/schema.
- **Giai đoạn 2 hoàn tất**: component API và style guide local-only đã qua cổng.
  Không đổi backend/API/schema hoặc hành vi nghiệp vụ.
- **Giai đoạn 3 hoàn tất**: khung ứng dụng, browser gate và kiểm chứng cuối PASS.

## Quyết định và giả định

1. Brief được dùng làm đặc tả theo yêu cầu trực tiếp của người dùng. Các quyết định
   cũ về nghiệp vụ giữ nguyên; lần này chỉ thay đổi trình bày và tương tác UI.
2. `AGENTS.md` coi `docs/` là tài liệu tham chiếu chỉ đọc. Yêu cầu mới cho phép tạo
   riêng `docs/ui-refresh/`; không sửa `CURRENT_STATE.md`, `FAILED_APPROACHES.md`
   hoặc hồ sơ nghiệp vụ có sẵn.
3. Giữ zero-build và thứ tự ghép JS hiện tại. Công cụ phát triển chạy qua `uv --with`;
   trình duyệt sản phẩm không phụ thuộc Playwright, fontTools hoặc axe.
4. Chưa tìm thấy chính sách phiên bản trình duyệt riêng. Mục tiêu thiết kế là hai
   phiên bản ổn định gần nhất; bằng chứng thực thi hiện tại dùng Chromium đã cài.
   Không coi kết quả Chromium là chứng nhận Safari/Firefox.
5. Số liệu 1.188 task, 351 chờ bạn, 0 agent đang làm và 837 đã xong đã được xác nhận
   lại qua API. Chỉ số “đã xong” hiện dùng bucket `settled`, bao gồm trạng thái terminal;
   không đổi cách tính nghiệp vụ trong đợt UI này.
6. Baseline chặn toàn bộ phương thức ghi và host ngoài localhost. Chỉ thay tùy chọn
   trong browser context cô lập; fixture dùng chặn GET, không ghi DB.
7. Font/icon tải từ mạng ở bước phát triển được lưu cục bộ cùng giấy phép. Font
   ứng viên trong `artifacts/` phục vụ nghiên cứu, không tự động đưa cả sáu họ vào app.
8. `verify_page.mjs` có POST thật ở một số nhánh. Không dùng nó với dữ liệu vận hành
   trong khảo sát chỉ đọc; bộ kiểm tra browser mới cưỡng chế GET/HEAD/OPTIONS.

## Phát hiện cần xử lý sau cổng Giai đoạn 0

- Công việc tải mọi trang API, ghép 1.188 hàng bằng `innerHTML`, rồi cuộn trong
  `.queue-scroll`. Tìm kiếm dựng lại toàn bộ hàng mỗi lần gõ.
- `organization.js` tạo liên kết `#/dept/<agent_id>` nhưng `findByKey()` chỉ nhận
  key của đơn vị. Liên kết “Công việc của đơn vị” có thể quay về sơ đồ và báo không
  có phòng ban. Sửa tại frontend khi chuyển trang Tổ chức, giữ route hợp lệ theo key.
- CSS không có layer; có sáu `!important` và nhiều selector bị ghi đè. Những rule
  chưa được browser sử dụng chỉ là ứng viên kiểm tra, không mặc nhiên là CSS chết.
- Preferences được áp dụng sau CSS/body, chưa có script trong head chống sai màu
  ở khung hình đầu. Graphite hiện vẫn mang sắc xanh và cần làm trung tính thật.

## Kiểm chứng đã hoàn tất

- Baseline lint và typecheck đã qua; suite unit/integration: 3.476 passed,
  3 skipped, 1 deselected trong 621,98s; 10 cảnh báo openpyxl được giữ trong log
  tại `artifacts/ui-shots/00-baseline/`.
- Bộ chụp hoàn tất: 57 URL, 338 ảnh; 24 trường hợp palette/mode/density.
  Receipt `stage-0-gate.json` có `gate=passed`, `final_ui_acceptance=false`.
- Sáu font đã thử ở 1440/390px, kiểm cmap không thiếu các ký tự tiếng Việt của brief.
- Kiểm toàn bộ chữ Việt dựng sẵn và kiểm shaping chuỗi NFD bằng CDP: sáu font
  dùng đúng custom font, không lấy dấu từ system fallback. Cmap thiếu một số
  combining codepoint riêng không đồng nghĩa trình duyệt thiếu chữ đã compose.
- Đã tạo ba prototype cấu trúc dùng dữ liệu GET, chọn dải chỉ số + hàng có vùng.

## Bước tiếp theo

1. Giai đoạn 4: trang Công việc và detail — dải chỉ số, form giao việc,
   toolbar/filter/sort, hàng task, kết quả/log/approval và hiệu năng danh sách.
2. Công việc/detail → từng trang → keyboard/mobile/a11y → nghiệm thu,
   theo cổng trong `01-design-plan.md`.

## Tự phản biện Giai đoạn 0

- Bỏ ý tưởng thêm trang dashboard/tổng quan mới: trùng Công việc và Phê duyệt, từng
  được ghi nhận trong F247. Cải thiện thao tác trên dữ liệu hiện có.
- Không chọn font vì giống Linear. Dùng hình chữ Việt, độ dài tiêu đề ở mobile và
  khả năng đọc ID/log để quyết định.
- Không coi test Python xanh là chứng minh UI đạt a11y hoặc hiệu năng.

## Tiếp tục khảo sát — 2026-10-07

- Người dùng yêu cầu hoàn tất Giai đoạn 0, chưa yêu cầu triển khai Giai đoạn 1.
- Working tree khi tiếp quản chỉ chứa hồ sơ/font/ảnh/script của khảo sát chưa
  commit. Tiếp tục đúng các file đó theo yêu cầu; không ghi đè code sản phẩm.
- Đã đọc lại brief trực tiếp và lưu nguyên bản tại `BRIEF.md`; SHA-256:
  `85b9c391f3cc564a17ad3c22e08169152c076697b7c011ab52f7eb4be3529975`.
- Khởi động PostgreSQL hiện có và API localhost với provider fake/hash. Không
  seed/migrate/reset, không khởi động workflow dispatch hoặc mail monitor.
- Rerun baseline browser để khép đủ ma trận; giữ kết quả 3.476 test đã đạt vì
  source/test/dependency nghiệp vụ không thay đổi. Lint/typecheck chạy lại sau
  bổ sung công cụ khảo sát. Cổng kiểm tra đối chiếu cả hash file UI và diff source
  với commit gốc; thiếu ảnh hoặc thiếu phép đo cuộn sẽ thất bại.

## Đóng cổng — 2026-10-07

- Giai đoạn 0: **PASS**. Source sản phẩm và hash UI giữ nguyên commit gốc.
- Lint cuối: 411 file đã formatted; typecheck: 177 source file, không lỗi.
- Giữ baseline 3.476 tests đã đạt, không chạy lại full suite khi source/test không đổi.
- Snapshot mới 1.189 task, khác mốc 1.188 ban đầu. Phép đo dùng số hàng thật:
  588,2ms tải ban đầu, 60 frame cuộn/977,8ms, max 17,4ms, 0 long task quan sát.
- 0 request ghi/host ngoài, 0 pageerror JS và overflow trong ma trận đã chụp.
  Console có một HTTP 503 từ fixture error; bốn nhóm lỗi axe giữ trong báo cáo.
- Đã xem specimen Việt, prototype và ảnh trang/màn phụ/list sau cuộn. Bỏ bố cục
  card KPI đồng trọng lượng và bảng nén mất lý do lỗi; giữ một dải chỉ số.
- 71 ảnh không thuộc manifest hoàn chỉnh được lưu riêng trong `.devdata/`,
  tránh nhầm checkpoint cũ với lượt mới. Mục lục chỉ hiển thị ảnh có metadata.
- `stage-0-gate.json` và index offline là bằng chứng đóng cổng, không phải chứng
  nhận UI cuối. Các Giai đoạn 1–7 còn nguyên; không tự chuyển sang làm sản phẩm.

## Giai đoạn 1 đang triển khai — 2026-10-07

- Tiếp tục nhánh `ui/refresh`, working tree sạch sau commit Giai đoạn 0.
- Giữ zero-build; layer `pages` chứa CSS legacy theo đúng thứ tự cũ. Các token,
  font/reset và override nền tảng có layer riêng; chưa dựng lại từng trang.
- Preferences chạy trong head, dùng cùng nguồn palette với runtime. Giữ nguyên
  năm field và khóa lưu cũ; không ghi lại cấu hình khi chỉ đọc/normalize.
- Ngoại lệ backend duy nhất: GET public `/api/v1/ui/assets/{filename}` cho 18
  WOFF2 được whitelist. Không phục vụ JS/HTML/license hoặc filesystem tùy ý.
- Font sản phẩm chỉ Inter/JetBrains Mono, 400/500/600; giữ license riêng. Sprite
  Lucide 0.468.0 có 30 symbol, raw SVG/license/hash từ package cố định.
- Gỡ root/token/density/motion legacy đã chuyển; không dùng !important. Component
  và shell mới vẫn thuộc Giai đoạn 2/3. Chưa chứng nhận toàn app đạt accessibility.

## Browser gate và tự phản biện Giai đoạn 1

- `stage-1-gate.json`: PASS, 170 checks, 24 ảnh desktop + 6 ảnh mobile 360/390px.
  1.296 cặp semantic đã đo qua computed style cho 12 tổ hợp × 2 mật độ; chữ
  thường/on-accent ≥4,5:1, focus/control/status border ≥3:1. Nút primary thật
  cũng được đo. Không suy rộng kết quả token thành chứng nhận mọi trang legacy.
- Graphite có nền/chữ/accent achromatic; status giữ màu riêng. Tăng độ đậm chữ
  phụ sáng sau khi phát hiện nền hover chỉ đạt 4,36–4,41:1 ở lượt đầu.
- Inter/JetBrains Mono 400/500/600 dùng custom font cho chuỗi Việt NFC/NFD theo
  CDP; cả 18 WOFF2 từ endpoint trùng byte asset cục bộ.
- Head prepaint khôi phục theme/palette/density/motion, VI/EN và sidebar ở lúc
  body đầu tiên xuất hiện. Kiểm reload, storage hỏng/bị chặn, auto theo OS,
  manual không đổi theo OS và không transition khi đổi theme; reduced-motion đạt.
- Chụp/xem lại graphite dark compact và mobile: sửa flex-shrink khiến hàng
  Công việc co xuống minimum và chồng chữ. Gate kiểm containment 30 hàng đầu
  trên toàn bộ ma trận. Compact là khoảng cách gọn, không ép nội dung nhiều dòng.
- Bỏ glyph icon sidebar/nav/bell đã chuyển sang Lucide và các font-weight giả
  650/750/800 trong CSS. Không thêm KPI/gradient/hiệu ứng trang trí. Icon ký tự
  trong các renderer legacy còn được chuyển khi làm component/từng trang.
- 0 request ghi/host ngoài, 0 JS/console error trong lượt gate. Không chạy model,
  mail, approval hoặc workflow thật; API khảo sát dùng fake/hash với dữ liệu GET.
- Node syntax check cho cả hai script ghép theo thứ tự đã qua; harness/test đọc
  HTML được cập nhật để không nuốt script head vào body. Giữ assertion nghiệp vụ.

## Đóng cổng Giai đoạn 1 — 2026-10-07

- **PASS, sẵn sàng Giai đoạn 2**. Browser receipt có hash source hiện tại, không
  có drift; ảnh/bảng tương phản tại `artifacts/ui-shots/01-foundation/index.html`.
- Lint: 413 file formatted; typecheck: 177 source file, không lỗi.
- Full suite chạy một lượt: **3.477 passed, 8 skipped, 1 deselected**, 10 cảnh báo
  openpyxl, 630,98s. Năm skip bổ sung so với baseline là event pipeline cần NATS
  chưa chạy; ba skip cũ là hai delegation-event UI/stream và hard-block catalogue.
  Không coi các nhánh skip là đã được xác minh trong lượt này.
- Focused UI/asset tests: 74 passed, 1 skipped. Node kiểm cú pháp JS ghép và boot
  harness PASS (12 request chỉ đọc); không chạy toàn bộ harness có POST trên DB
  vận hành. Các kiểm tra browser cuối bao phủ phần CSS đã tinh chỉnh sau khi
  full suite bắt đầu; không chạy lại suite cho các chỉnh sửa trình bày đó.
- Không đổi schema, workflow, model, mail hoặc approval; không seed/reset dữ liệu
  vận hành. Server khảo sát fake/hash được dừng sau khi đóng cổng.
- Giai đoạn 2–7 còn nguyên. Giai đoạn 1 không phải bản UI refresh hoàn chỉnh.

## Triển khai Giai đoạn 2 — 2026-10-07

- API `UI` cho bộ thành phần trong mục 8.1: control/form/choice, status/initials,
  panel/row/table/toolbar, tabs/segmented với sliding indicator, overlay/dialog/
  drawer/popover/menu/tooltip, toast/skeleton/empty/callout, breadcrumb/back/kbd,
  code/log và ID sao chép. Hướng dẫn/trust boundary tại `03-components.md`.
- `#/_styleguide` chỉ đăng ký khi hostname loopback; không thêm nav item. Host
  remote không có route. Specimen ghi rõ dữ liệu mẫu; không submit business API.
  Palette/mode/density chỉ preview và được khôi phục khi rời route.
- Không chuyển hàng loạt renderer sang component ở giai đoạn này; GĐ3–5 dùng
  lại API đã kiểm chứng. Source CSS mới ở layer components; defaults cũ của
  button/input/heading/dialog/kbd chuyển xuống base, giữ class/id cũ.
- Phát hiện generic legacy ở layer pages ghi đè foreground nút primary và error
  border. Chuyển defaults xuống base và gỡ override border utility quá rộng;
  không dùng !important để thắng selector.
- Dialog trap Tab, trả focus, Escape đóng trước navigation; nested modal giữ
  scroll lock đến lúc cuối. Confirm đang chờ khóa nút/aria-busy; khi lỗi giữ
  input, mở lại nút và hiển thị alert trong modal, tránh toast ngoài top layer.
- Menu bỏ disabled item và hỗ trợ Tab/Escape; tooltip định vị trong viewport.
  Clipboard dùng API thật trong context browser; thất bại được báo lỗi, không
  ghi toast thành công giả. Log copy cả source kể cả đang lọc.
- Axe dùng bản cục bộ 4.10.3 đã lưu từ GĐ0. Audit root style guide và overlays;
  không tuyên bố shell/trang legacy hoặc Safari/Firefox đã đạt a11y.

## Tự phản biện Giai đoạn 2

- Dùng section/đường chia để trình bày specimen; không bọc mỗi nhóm trong card
  giống nhau, không thêm KPI/ảnh avatar giả hoặc dashboard mới.
- Khi xem ảnh đã sửa tooltip làm tràn ngang và drawer mobile có khoảng trống
  bên phải do legacy/native max-width. Bottom sheet phải đủ chiều rộng viewport,
  vừa chiều cao nội dung; gate kiểm cả width thay vì chỉ kiểm vị trí đáy.
- Bỏ chữ monospace nhỏ thêm 0,9 lần do base `code`; log mới giữ đúng 12px.
- Compact điều khiển cả padding nút, không chỉ minimum height: sm/md/lg có
  kích thước riêng, md thực tế 32/40px; coarse pointer giữ control ≥44px.
- Link helper kiểm cùng origin sau khi URL được normalize, bao gồm biến thể
  backslash/control-character, thay vì chỉ nhìn ký tự đầu của href.
- Lỗi landmark-unique giữa vùng log và nội dung log được sửa bằng tên riêng;
  không tắt rule axe. Tooltip được audit sau khi fade đã ổn định; không dùng
  trạng thái đang mờ giữa animation để coi là palette sai hoặc tắt contrast rule.

## Đóng cổng Giai đoạn 2 — 2026-10-07

- **PASS, sẵn sàng Giai đoạn 3**. Browser gate: 148 checks, 24 palette/mode/
  density combinations, 34 ảnh. Axe không có violation ở root style guide của
  ma trận, bản EN/mobile, dialog/drawer và tooltip/popover/toast khi mở.
- Lint: 414 file formatted; typecheck: 177 source file, không lỗi. Full suite
  một lượt: **3.477 passed, 8 skipped, 1 deselected**, 10 cảnh báo openpyxl,
  474,67s. Skip như GĐ1: 5 cần NATS chưa chạy, 3 nhánh cũ được ghi rõ ở trên.
- Node contract: text/attribute escaping, loading/disabled, whitelist icon,
  link an toàn và route không đăng ký trên remote host đã qua. Node syntax và
  boot harness PASS (12 request chỉ đọc); không chạy full harness có POST.
- Browser kiểm thao tác bằng selector thật của guide; không chỉnh JSON prefs
  để chụp ảnh lệch lựa chọn hiển thị. Confirm lỗi giữ input; nested modal/Tab/
  Escape/restore focus, clipboard, sort/log filter/wrap/copy, reduced-motion,
  mobile bottom sheet vừa nội dung/full width và coarse Compact đều đạt.
- Các tinh chỉnh UI cuối được xác minh bằng browser; không chạy lại full suite
  cho thay đổi CSS/trình bày sau khi suite bắt đầu. Receipt lưu hash source;
  không lấy test Python làm chứng nhận model hoặc a11y toàn app.
- Server khảo sát fake/hash được dừng sau cổng; không seed/reset/submit business
  data, không dùng model/mail thật. Nhánh `ui/refresh`, commit theo giai đoạn.


## Triển khai Giai đoạn 3 — 2026-10-07

- Shell có sidebar 216/64px, icon gap 8px, nhãn sentence case, một indicator
  trượt 2px và tooltip hover/focus khi thu gọn. Footer connection/schema mono.
- Topbar sticky: ghost Back/Esc, breadcrumb, unread dot và số lượng ở aria-label,
  segmented VI/EN, menu mode/sáu màu/density preview và lưu trên thiết bị.
- Workspace lấy tên thật từ GET tổ chức; phiên có một mục. Tenant hiện tại tên
  `Autonomous Demo Company`; không đổi tên backend hoặc thêm tổ chức giả để khớp
  nhãn O-Nexus. Nếu GET lỗi, giữ ID và báo chưa tải được tên.
- Auth-off callout ở trước mọi view, mở rộng có thông tin dev:no-auth và khuyến
  nghị service token. Thu gọn details giữ cảnh báo; auth-on giữ nút Token.
- Mobile drawer chuyển nguyên sidebar và workspace popover vào native dialog,
  tránh ID trùng và popover bị inert ngoài modal. Esc đóng lớp trên trước;
  Tab giữ trong drawer; chọn trang đưa focus vào main; đóng thường trả focus
  về nút mở. Resize desktop trả node/focus phù hợp.
- Giữ route/query org, kho prefs/locale/collapse, inbox và hành vi API cũ.
  Không viết DB/seed/reset, đổi workflow/controller hoặc chạy model/mail thật.
- Handoff `04-shell.md`; ảnh và receipt ở `artifacts/ui-shots/03-shell/`.
  Corpus GET tại lúc chụp có 1.191 task; thay đổi corpus so với GĐ2 không phải
  do gate (mọi phương thức ghi bị chặn).

## Tự phản biện Giai đoạn 3

- Không tạo KPI hoặc trang tổng quan mới; phần hàng task/metrics còn nguyên,
  để GĐ4 thay đổi một cách nhất quán thay vì trộn hai giai đoạn.
- Khi dọn CSS đã bỏ nhầm display:grid cùng rule khung cũ. Xem ảnh thực phát hiện
  ngay; sửa vào shell và thêm assert vị trí/độ rộng desktop ở đủ 24 tổ hợp.
  Không lấy axe sạch làm bằng chứng bố cục đã đúng.
- Drawer.close phát sự kiện close sau click. Restore focus mặc định ghi đè focus
  main khi chọn route; sửa theo điểm đến, gate chờ event thật rồi kiểm focus.
- Chuyển workspace popover vào modal cùng node sidebar; không clone nav hoặc
  đặt một popup bên ngoài dialog mà người dùng không thể thao tác.
- Menu nhỏ gọn bằng block flow; không giữ khoảng trống 16px grid cho từng node
  tiêu đề/fieldset. Gỡ các rule khung cũ đã thay thế, không thêm !important.
- Browser gate ban đầu mất axe sau reload; instrument được nạp bằng init script
  cho mỗi document. Không bỏ audit mobile hoặc vô hiệu hóa rule để đạt cổng.
- Node harness thêm DOM methods/comment/microtask để boot được khung mới;
  geometry shim bằng zero và không dùng làm bằng chứng layout/keyboard.
  Chỉ chạy boot harness có guard GET, không chạy harness đầy đủ có POST thật.

## Đóng cổng Giai đoạn 3 — 2026-10-07

- Browser PASS: **191 checks, 24 tổ hợp, 35 ảnh**, cộng hai ảnh trước và một
  ảnh sau desktop. Axe không violation trong shell/menu/drawer được audit.
- Keyboard VI/EN/mode, Esc/focus/Tab, rail tooltip/indicator, Auto OS change,
  reduced motion, reload prefs, drawer 390/320px, workspace GET lỗi và auth-on
  frontend variant đều qua. Không chứng nhận Safari/Firefox hoặc toàn bộ app.
- Notification fixture đi qua notify/paintBell thật ở browser nhưng không là
  approval thật. Auth-on fixture không gửi token; GET tổ chức lỗi là fixture.
- **PASS, sẵn sàng Giai đoạn 4**. Lint: 415 files formatted; typecheck: 177
  source files không lỗi. Full suite một lượt: **3.477 passed, 8 skipped,
  1 deselected**, 10 cảnh báo openpyxl, 601,83s. Các skip như GĐ2: NATS chưa
  chạy và các nhánh cũ đã nêu trong hồ sơ trước.
- Node boot PASS, 13 request chỉ đọc; syntax JS ghép PASS. Node DOM shim
  không chứng minh hình học/keyboard; browser gate là bằng chứng phần này.
- Receipt khớp hash source UI cuối. Sau gate chỉ gỡ một trailing space bên ngoài
  string ở shell.js; receipt giữ hash trước/sau và ghi rõ thay đổi định dạng.
  Không chạy lại full suite cho chỉnh sửa whitespace/tài liệu.
- API khảo sát fake/hash đã dừng, không để server này giả làm runtime model thật.
  GĐ4–7 chưa hoàn tất; không coi cổng shell là nghiệm thu toàn bộ sản phẩm.
