# UI refresh — nghiệm thu Giai đoạn 5–7

Ngày kiểm chứng: 2026-10-07. Phạm vi: console zero-build của AI organization;
giữ quy trình, hash route và quyền quyết định ở backend. Test demo cũ có một
ngoại lệ ghi dữ liệu phát triển, được phát hiện và sửa trong phần kiểm chứng.

## Kết quả triển khai

| Giai đoạn | Thay đổi |
|---|---|
| 5 | Phê duyệt dùng inbox, drawer đọc đúng dự thảo và dialog xác nhận chung. Tổ chức/đơn vị, Quy trình, Thư viện, Dữ liệu nghiệp vụ, Vận hành và Cài đặt dùng chung panel, form, table, tab route, loading/empty/error/retry. Sửa liên kết agent → đơn vị; lỗi thực thi đọc/chép đầy đủ. |
| 6 | Ctrl/Cmd K tìm trang, task, người giữ và tùy chọn; `?` mở trợ giúp. Toast dùng chung, có liên kết và trả focus đúng khi đóng overlay. Mobile có bảng xếp chồng, drawer/bottom sheet và command palette toàn màn hình. Appearance có preview, hoàn tác, lưu và khôi phục khi tải lại. |
| 7 | Chuyển spacing/color về token; dọn CSS và handler không còn caller; thêm công cụ kiểm hợp đồng style và cổng tổng hợp bằng chứng. Rà ảnh desktop/mobile, tương phản, bàn phím, reduced-motion, forced-colors và reflow 200%. |

Receipt chốt: [`release-gate.json`](../../artifacts/ui-shots/07-final/release-gate.json):
**PASS, 531 kiểm tra tổng hợp, 420 ảnh có manifest khớp source.**
Chỉ `gate=passed` và `final_ui_acceptance=true` của receipt này chứng nhận UI
cuối. `business_live_acceptance=false`: test UI không chứng nhận đã tuyển người,
onboard, mua vật tư, gửi mail hoặc có người phê duyệt thật.

## Thiết kế và tự phản biện

- Giữ Inter cho nội dung tiếng Việt, JetBrains Mono cho ID/log; font 400/500/600
  và sprite Lucide tự phục vụ, giấy phép tại `THIRD_PARTY_NOTICES.md`.
- Sáu palette: Navy, Teal, Indigo, Forest, Copper, Graphite. Nền sáng/tối có
  bộ surface và text riêng; Graphite trung tính. Màu nhấn dành cho lựa chọn và
  hành động chính; trạng thái có nhãn/icon và không phụ thuộc màu.
- Khoảng cách dùng thang token 2/4/8/12/16/20/24/32/40/56; bo góc dùng token
  control/panel. Compact thay row/cell/field padding, không ép chiều cao nội dung
  nhiều dòng. Các kích thước hình học/breakpoint/stroke không phải spacing.
- Một khoảnh khắc chuyển động: lần mở Work đầu của phiên, tối đa bốn chỉ số và
  12 hàng. Chuỗi entry kết thúc trong 376ms, counter trong 480ms; không chạy lại
  khi polling/tìm kiếm/đổi trang. Reduced-motion bỏ cả entry và counter.
- Bỏ bộ toast cũ, bảng quản lý cũ, alias search không còn dùng và handler
  `data-kill` không có nơi gọi. Giữ class legacy còn caller thực tế và route
  tổng hợp `#/work`; không xóa chỉ vì coverage một lượt không quan sát thấy.
- Sửa race khi đóng/mở lại command palette, preview chưa lưu bị mang sang trang
  khác, scroll region thiếu focus và liên kết thông báo thiếu underline.
  Form dừng agent hiện chặn lý do toàn khoảng trắng trước khi gọi server.

## Ảnh trước/sau

Ảnh trước là baseline Giai đoạn 0; ảnh sau lấy từ manifest cuối. Các số liệu có
thể khác vì database vận hành tiếp tục thay đổi; không sửa dữ liệu để khớp ảnh.

| Màn hình | Trước | Sau |
|---|---|---|
| Công việc, sáng desktop | [Ảnh](../../artifacts/ui-shots/00-baseline/route-00-light-1440.png) | [Ảnh](../../artifacts/ui-shots/07-final/route-00-light-1440.png) |
| Công việc, tối desktop | [Ảnh](../../artifacts/ui-shots/00-baseline/route-00-dark-1440.png) | [Ảnh](../../artifacts/ui-shots/07-final/route-00-dark-1440.png) |
| Công việc, sáng mobile | [Ảnh](../../artifacts/ui-shots/00-baseline/route-00-light-390.png) | [Ảnh](../../artifacts/ui-shots/07-final/route-00-light-390.png) |
| Công việc, tối mobile | [Ảnh](../../artifacts/ui-shots/00-baseline/route-00-dark-390.png) | [Ảnh](../../artifacts/ui-shots/07-final/route-00-dark-390.png) |
| Phê duyệt, sáng desktop | [Ảnh](../../artifacts/ui-shots/00-baseline/route-03-light-1440.png) | [Ảnh](../../artifacts/ui-shots/07-final/route-03-light-1440.png) |
| Phê duyệt, tối mobile | [Ảnh](../../artifacts/ui-shots/00-baseline/route-03-dark-390.png) | [Ảnh](../../artifacts/ui-shots/07-final/route-03-dark-390.png) |
| Tổ chức, tối desktop | [Ảnh](../../artifacts/ui-shots/00-baseline/route-05-dark-1440.png) | [Ảnh](../../artifacts/ui-shots/07-final/route-05-dark-1440.png) |
| Tổ chức, sáng mobile | [Ảnh](../../artifacts/ui-shots/00-baseline/route-05-light-390.png) | [Ảnh](../../artifacts/ui-shots/07-final/route-05-light-390.png) |
| Quy trình, sáng desktop | [Ảnh](../../artifacts/ui-shots/00-baseline/route-08-light-1440.png) | [Ảnh](../../artifacts/ui-shots/07-final/route-08-light-1440.png) |
| Quy trình, tối mobile | [Ảnh](../../artifacts/ui-shots/00-baseline/route-08-dark-390.png) | [Ảnh](../../artifacts/ui-shots/07-final/route-08-dark-390.png) |
| Cài đặt, sáng desktop | [Ảnh](../../artifacts/ui-shots/00-baseline/route-29-light-1440.png) | [Ảnh](../../artifacts/ui-shots/07-final/route-29-light-1440.png) |
| Cài đặt, tối desktop | [Ảnh](../../artifacts/ui-shots/00-baseline/route-29-dark-1440.png) | [Ảnh](../../artifacts/ui-shots/07-final/route-29-dark-1440.png) |
| Cài đặt, sáng mobile | [Ảnh](../../artifacts/ui-shots/00-baseline/route-29-light-390.png) | [Ảnh](../../artifacts/ui-shots/07-final/route-29-light-390.png) |
| Cài đặt, tối mobile | [Ảnh](../../artifacts/ui-shots/00-baseline/route-29-dark-390.png) | [Ảnh](../../artifacts/ui-shots/07-final/route-29-dark-390.png) |

Toàn bộ trang, màn con, chủ đề và fixture: [mục lục offline](../../artifacts/ui-shots/07-final/index.html).
Command palette mobile: [360px](../../artifacts/ui-shots/06-interactions/palette-360.png).
Phê duyệt có dữ liệu: [fixture ghi rõ nhãn](../../artifacts/ui-shots/06-interactions/approval-inbox.png).

## Bằng chứng kiểm chứng

| Kiểm tra | Kết quả / phạm vi |
|---|---|
| Toàn app | 57 route × sáng/tối × 1440/390px; thêm mọi route ở 360px. Receipt cuối kiểm hash đầu/cuối; không có JS error, console warning/error, request ghi hoặc host ngoài loopback. |
| Foundation | 170 checks; 1.296 cặp màu tính từ computed style qua 24 tổ hợp; font Việt NFC/NFD, prepaint, storage, OS theme/reduced-motion. |
| Components | 148 checks; 24 tổ hợp style guide, focus/keyboard/dialog/menu/log/copy, mobile và axe. |
| Shell | 191 checks; drawer, workspace, appearance/language, tooltip, notification, auth banner và mobile. |
| Công việc | 131 checks; 5.000 task fixture, DOM ban đầu 100 hàng, loading/empty/error/no-results, tiêu đề/lỗi dài, ổn định focus/node. |
| Sự cố | 89 checks; đọc/copy/retry lỗi, fixture 409/503, loading/empty, bộ lọc, mobile và route tổng hợp cũ. |
| Tương tác GĐ5–6 | 71 checks; command/ID/tên/người giữ/tìm không dấu “Đ”, appearance, approval và agent-stop với POST fixture, reduced/forced-colors/200% reflow. |
| Commit trung gian | 9 commit × 9 trang = 81 boot checks; dựng HTML/JS từ blob của từng commit, Node syntax và browser thật, API chỉ đọc. |
| Lint / type | `make lint` và `make typecheck` passed; log giữ nguyên trong `verification/`. |
| Unit/integration | **3.482 passed, 3 skipped, 1 deselected**, 525,86s. 10 warning openpyxl khi đọc fixture spreadsheet; không phải lint failure. |
| E2E | **34 passed**, 108,17s; preflight PostgreSQL/NATS/Temporal thật passed. Model trong test là fake/hash. |
| Provider probe | Credential OpenRouter **valid** qua endpoint của provider; không gọi completion và không kiểm chất lượng đầu ra model. |
| Runtime sau restart | 23 checks; OpenRouter/DOTS primary, ready/least-privilege/RLS, 12 agent + 8 tool + 43 skill có ID hợp lệ; mở chín trang chính bằng browser thật, chỉ GET. |

**Ngoại lệ test cũ đã sửa:** `test_the_demo_script_reports_the_truth_about_delegation`
trước đây ép subprocess dùng database phát triển và tạo task “Board pack” bằng
ScriptedRuntime. Vì vậy không thể khẳng định lượt suite đầu không ghi dữ liệu
dev, dù mọi browser probe đều chặn request ghi. Không seed/reset/xóa dữ liệu
dev để che dấu vết; những task này là demo, không phải đầu ra model thật.

Test hiện dùng seed thực trong công ty riêng ở `ai_orchestrator_test`, commit
để subprocess thấy dữ liệu, truyền đúng slug đã lưu và kiểm lại task bằng query
độc lập ở database test. Không chỉ tin stdout của script. Module này cùng các
construction UI tests đã qua **28 test, 1 deselected** sau sửa; suite đầy đủ
đã chạy lại và passed trên mã sau sửa. Test `live_model` có ghi dev chỉ chạy khi opt-in,
vẫn được loại khỏi target test thông thường.

Ba skip: tenant test chưa có delegation để kiểm event stream/UI và chưa seed
process spine cho một test catalogue. Không tính các nhánh này là passed.
Các gate browser/Node/lint đã chạy lại sau chỉnh copy/breadcrumb và normalize
tìm kiếm. Suite backend cuối chạy lại sau sửa test isolation; không dùng log
trước sửa để đóng cổng.

Phép đo trước/sau riêng, cùng API/corpus **1.197 task**, Chromium 1440×900,
baseline commit `7a7c860` được dựng trong thư mục tạm; không checkout/reset:

| Chỉ số | Trước GĐ4 | Sau GĐ7 |
|---|---:|---:|
| DOM ban đầu | 1.197 hàng | 100 hàng |
| Navigation → hàng + font ready | 1.600,5ms | 1.337,7ms |
| Dựng lại từ cache | 19,5ms | 6,1ms |
| Hàng thực đã cuộn | 1.197 | 1.197 |
| Frame p95 khi cuộn | 16,8ms | 17,7ms |
| Long task khi cuộn | 0 | 0 |
| Cuộn lồng | Có | Không |

Đây là một mẫu trên máy hiện tại, không phải SLA hay benchmark nhiều máy.
Startup vẫn có long task (lớn nhất trước 413ms, sau 204ms); không diễn giải
“không long task khi cuộn” thành “không long task toàn phiên”. Receipt:
[`performance-and-form.json`](../../artifacts/ui-shots/07-final/performance/performance-and-form.json).
Baseline GĐ0 khác thời điểm có 1.189 task/588,2ms nên không dùng làm so sánh
thời gian trực tiếp với lượt này.

## File và ranh giới thay đổi

Trong `src/ai_orchestrator/web/`:

- Thêm `approvals-ui.js`, `commands.js`, `commands.css`.
- Sửa `management.js`, `navigation.js`, `components.js`, `work.js`,
  `organization.js`, `workflows.js`, `library.js`, `business.js`, `operations.js`,
  `settings.js`, `preferences.js`, `core.js`, `work-ui.js`, `index.html`.
- Chuẩn hóa `components.css`, `console.css`, `foundation.css`, `legacy-base.css`,
  `management.css`, `shell.css`, `tokens.css`, `work-ui.css`.
- `page.py` chỉ thêm thứ tự ghép JS/CSS. **Giai đoạn 5–7 không sửa API, backend
  nghiệp vụ, schema, migration hoặc model.** Ngoại lệ backend của toàn đợt là
  GET font whitelist đã có từ Giai đoạn 1, không mở rộng trong lần này.

Trong `scripts/`: thêm `ui-final-gate.py`, `ui-interactions-gate.py`,
`ui-style-contract.py`, `ui-release-gate.py`; cập nhật selector/đợi animation
trong `ui-foundation-gate.py`, `ui-components-gate.py`, `ui-issues-gate.py`,
`verify_console_browser.py`, `verify_page.mjs`. Không bỏ assertion hoặc thêm axe exclusion để
né lỗi. Các tài liệu mới/cập nhật nằm trong `docs/ui-refresh/`; ảnh/receipt
nằm trong `artifacts/ui-shots/05-pages`, `06-interactions`, `07-final`.
Không xóa file source ngoài phạm vi UI.

Ngoài source UI, sửa duy nhất
`tests/integration/test_seeded_company_runs.py` để cách ly subprocess demo;
không sửa `scripts/demo_hierarchy_run.py` hoặc hành vi runtime của sản phẩm.

Thêm `ui-commit-boot-gate.py` để kiểm commit trung gian; bổ sung `--out` và
source/screenshot receipt cho `ui-work-performance.py` để giữ nguyên bằng chứng
lịch sử GĐ4 khi đo lại. Assertion metric của gate Sự cố đã cập nhật sang inbox
hai chỉ số, đồng thời bổ sung test bốn chỉ số cho route tổng hợp cũ.
Thêm `ui-runtime-check.py` để kiểm readiness, ID đã lưu và browser sau restart;
probe không gọi completion hoặc thao tác nghiệp vụ.

## Giới hạn và bước tiếp theo

- Browser đã thực thi: Chromium được cài tại máy. Chưa chứng nhận Safari,
  Firefox hoặc hỗ trợ phiên bản trình duyệt khác bằng kết quả này.
- Approval và agent-stop POST trong browser gate đều là fixture tại browser,
  gồm lỗi quyền 403 có chủ ý. Test backend/E2E hiện dùng database test/model fake
  sau khi sửa ngoại lệ demo đã nêu;
  không coi chúng là bằng chứng OpenRouter, email hay phê duyệt con người thật.
- Các task cũ có lỗi từ provider, kể cả content-filter refusal, vẫn hiện đúng
  trong Sự cố. UI refresh không sửa lịch sử hoặc tự retry công việc vận hành.
- Bước sản phẩm kế tiếp vẫn là tuyển MEP → onboarding, rồi procurement, theo
  `FUTURE_WORK.md`: smoke riêng OpenRouter và từng connector, thu chứng cứ đầu
  ra, người có quyền duyệt thật, đối chiếu độc lập trước khi bật tự chủ rộng.
  Không đưa Bãi Tràm vào phạm vi này.
- Nếu muốn xu hướng/sparkline, cần API time series có timestamp và định nghĩa
  chỉ số; hiện tại không tạo lịch sử giả từ số đếm tức thời.

## Chạy lại và hoàn tác

Lệnh, dependency và trust boundary tại [README](README.md). Gate tổng hợp kiểm
hash source từng receipt, ảnh tồn tại, từng assertion và log lint/type/test/E2E.
`release-gate.json` không được sửa bằng tay để chuyển sang passed.

Các commit theo trang nằm trên `ui/refresh`. Trước khi merge, có thể bỏ nhánh
này để trở về nhánh trước. Sau khi merge/push, dùng `git revert <commit>` theo
thứ tự ngược dependency; không dùng reset/force-push trên main. Revert không
khôi phục dữ liệu vì đợt UI này không migrate. Revert code không xóa các task
demo mà test cũ đã tạo; giữ phân biệt chúng với kết quả vận hành thật.

Sản phẩm đã khởi động lại với `openrouter`, primary
`dots-studio/dots-3-note-preview:free`, embedding `hash`; `/health` và `/ready`
passed. Console tại `http://127.0.0.1:8100/api/v1/ui?org=org_01m3ycsehgwz7v1fk2ahswkwhm#/give`.
Runtime receipt và chín ảnh sau restart nằm cùng thư mục `07-final/`.
Lượt suite sau sửa test giữ nguyên corpus dev 1.197 task quan sát trước/sau;
không xóa các demo cũ. Có thể dừng server bằng `make serve-stop`.
