# Tiến độ UI refresh

## Phạm vi và điểm bắt đầu

- Brief: `C:/Users/vutun/Downloads/ui-refresh-brief.md`, đã đọc toàn bộ.
- Nhánh: `ui/refresh`; điểm gốc: `68e4718ab9deacd220cdd354fed21f85b07035eb`.
- Working tree sạch trước khi bắt đầu. Không reset, dọn checkout hoặc sửa dữ liệu vận hành.
- **Giai đoạn 0 hoàn tất**: khảo sát, baseline thật, thử font và kế hoạch thiết kế.
- Chưa thay đổi file sản phẩm, backend, API, DB hoặc schema.

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

1. Giai đoạn 1: layers/tokens, Inter + JetBrains Mono, icon sprite, density,
   motion và preferences trước khi vẽ. Đo tương phản đủ 12 tổ hợp trên cặp thực tế.
2. Giai đoạn 2: component API và style guide local-only, đủ variant/state/axe.
3. Shell → Công việc/detail → từng trang → keyboard/mobile/a11y → nghiệm thu,
   theo cổng trong `01-design-plan.md`. Chưa triển khai Giai đoạn 1 trong đợt này.

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
