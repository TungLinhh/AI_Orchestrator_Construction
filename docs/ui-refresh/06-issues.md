# Giai đoạn 5 — trang Sự cố

Đây là trang đầu tiên của Giai đoạn 5, không phải nghiệm thu toàn giai đoạn.
Mốc kế tiếp là Phê duyệt; sau đó Tổ chức/đơn vị, Quy trình cùng các màn con,
Thư viện, Dữ liệu nghiệp vụ, Vận hành và Cài đặt. Mỗi trang một commit.

## Thay đổi

- `#/work/issues` có renderer riêng `IssuesUI`; đọc register để dựng sự cố,
  không phải chờ inbox phê duyệt mới hiển thị. Luồng Phê duyệt cũ được giữ lại.
- Dải chỉ số là `UI.metrics` dùng chung: Cần xem, Thất bại, Bị chặn, Chờ xử lý.
  Click lọc, số 0 có chủ ý; số lấy từ đầy đủ register, không tạo số thay thế khi
  GET lỗi. Hủy/cancelled vẫn thuộc Tất cả, như predicate cũ.
- Nhãn “Chờ xử lý” thay “Chưa có người giữ” vì predicate cũ là created và
  waiting_on=you, không chứng minh owner_name trống. Không đổi membership.
- Tìm theo tên, người giữ, mã và toàn bộ lỗi. Filter/search dùng cache đã tải
  đủ các trang API. Ban đầu 100 hàng, tải thêm 100; DOM keyed giữ disclosure và
  focus khi dữ liệu không đổi. Không thêm API, schema hoặc build step.
- Hàng dùng shared list-row/metadata/badge/copy-ID/disclosure. Lỗi mở rộng dùng
  LogViewer mono, xuống dòng và copy nguyên văn. Màu danger cho failed/blocked,
  warning cho created, neutral cho canceled/cancelled.
- Có loading, empty, no-results và error/retry. Khi refresh lỗi, giữ dữ liệu
  đang có cùng cảnh báo; lọc cache không làm mất cảnh báo. Lần mở đầu bị lỗi
  không dựng chỉ số 0. Incomplete pagination được báo rõ.
- Nút “Thử lại bằng task mới” giữ `data-retry` và `retryTask()`/endpoint cũ.
  Server vẫn kiểm tra quyền và workflow. Không thêm bypass hay hứa chắc rằng
  retry sẽ chạy. Luồng từ chối giữ hàng cũ và báo lý do bằng toast hiện có.
- `navIssueCount` trên trang Sự cố đếm cùng membership với danh sách, gồm task
  mới đang chờ bạn. Renderer Phê duyệt vẫn dùng badge cũ; sẽ đồng bộ trong mốc
  chuyển Phê duyệt, không đổi approval action trong commit này.

## Thành phần dùng chung

`UI.metrics({label,items})` nhận key/label/value/note/selected/emphasized.
Value null hiển thị —; số theo locale. Các button có aria-pressed; component
đổi selection, trang sở hữu thao tác lọc. Style guide có bộ chỉ số **mẫu**.

CSS thêm vào `components.css`, không tạo stylesheet riêng cho Sự cố:
metric-strip, metadata, disclosure, detail-row, search-field và segmented có
thể wrap. Mobile metric/filter 2×2; summary có target 44px khi pointer coarse.
Layout `.ui-page` bỏ margin cộng gap và eyebrow thừa, dùng chung cho các trang
tiếp theo. Màu dùng token semantic, không thêm `!important`.

Các ID và data-i/data-task/data-retry hiện có được tra toàn repo trước khi đổi
markup; giữ nguyên để handler/harness vẫn tìm được. Không xóa khóa i18n hoặc
prefs. Chuỗi mới dùng VI/EN qua UI.L và số qua num(). Hash/query/deep links giữ
nguyên, điều khiển workflow/approval backend không sửa.

## Bằng chứng

[Receipt](../../artifacts/ui-shots/05-issues/gate.json),
[ảnh trước/sau và trạng thái](../../artifacts/ui-shots/05-issues/index.html).

- **88 checks PASS, 16 ảnh cổng**, thêm hai ảnh trước (18 file riêng; 21 lượt chụp cổng ghi đè loading): sáu màu × sáng/tối,
  desktop 1440px, 360/390/768px sáng/tối, VI/EN, empty/no-results/loading,
  initial/stale error, cache/filter/search, copy và incremental load.
- Axe không violation trong scope Sự cố được audit; metric mới trong style
  guide cũng qua axe/selection. Không chứng nhận toàn style guide lần nữa,
  toàn app hoặc các trình duyệt khác.
- GET corpus khi đóng cổng: **1.194 task, 344 sự cố**. Corpus có thể thay đổi
  do hoạt động bên ngoài; browser gate không gửi phương thức ghi đến server.
- Retry fixture POST trả 409 tại browser: kiểm disabled, payload {}, thông
  báo từ chối và dữ liệu giữ lại. Không thực hiện retry, model hoặc workflow
  thật. GET 503 và empty fixture cũng chỉ tồn tại ở context trình duyệt.
- Không pageerror, không warning/error console bất ngờ, không host ngoài
  localhost hoặc write đi tới API. Ba HTTP console error có nguồn fixture
  409/503 được ghi riêng trong receipt, không tắt console audit.
- Quay về Công việc kiểm danh sách/chỉ số; quay sang Phê duyệt kiểm panel và
  bốn chỉ số cũ. Node syntax/boot PASS, 13 GET; shim không chứng minh hình học.
- Lint PASS: 418 files; typecheck PASS: 177 sources. Full suite một lượt PASS:
  **3.477 passed, 8 skipped, 1 deselected**, 10 cảnh báo openpyxl, **493,28s**.
  Chi tiết cuối tại PROGRESS/verification.json.

## Tự phản biện và giới hạn

Không thêm dashboard, trend hay “nguyên nhân do AI đoán”; chỉ trình bày nguyên
văn lỗi đã có. Bộ lọc mobile từng bị ép thành bốn cột nhỏ; xem screenshot thật
rồi chuyển 2×2. Thông báo hủy không tô đỏ như một failure. Không xóa cảnh báo
GET lỗi khi chỉ đang tìm trong cache. Guide sample có nhãn rõ, không là KPI.

Gate ban đầu dùng nhầm `mode` thay `theme` của preferences; sửa instrument,
assert colorMode thật trước axe và chạy lại ma trận. Guide được mount trong
`#uiStyleguide`, không có view-styleguide; sửa selector và chạy lại cổng.
Không ghi nhận nhãn screenshot “dark” làm bằng chứng nếu mode chưa áp dụng.

Retry toast vẫn dùng handler/shared toast cũ của hệ thống. Chuyển toàn bộ toast
và keyboard/a11y toàn app thuộc Giai đoạn 6. Quy tắc authority không thể suy ra
đầy đủ từ register; server tiếp tục quyết định retry, như trước. Full suite
không được coi là chứng minh model trả kết quả đúng hoặc email/live onboarding.

## Chạy lại

Với API khảo sát auth-off, fake/hash trên localhost:8100:

```sh
uv run --with playwright python scripts/ui-issues-gate.py --org ORG_ID --chromium CHROMIUM_PATH
```

Không chạy `make page` hoặc full verify_page.mjs với tenant vận hành vì có
POST thật. Không seed/reset/migrate để tạo trạng thái chụp ảnh.
