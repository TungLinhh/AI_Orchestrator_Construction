# Giai đoạn 4 — Công việc và hồ sơ task

Trang Công việc giữ hash routes, query org, ID điều khiển, form payload và các
handler chạy/thử lại/hủy/phê duyệt hiện có. Không đổi backend nghiệp vụ, DB,
schema hoặc thẩm quyền workflow. `page.py` chỉ ghép thêm `work-ui.js/css` sau
shell, trước boot; sản phẩm vẫn zero-build và dùng tài sản cục bộ.

## Cách sử dụng

- Dải chỉ số thống nhất, click để lọc. Chờ bạn là điểm nhấn 32px; số 0 yên tĩnh.
  “Đã xong” giữ bucket `settled` của API và ghi rõ bao gồm cả thất bại.
- Tìm tiêu đề, người giữ hoặc mã trên toàn bộ dữ liệu đã tải. Có xóa tìm kiếm,
  bộ lọc có số lượng và sắp xếp mới/cũ/tiêu đề/chờ bạn trước. Các thao tác này
  không tải lại API. Thanh công cụ sticky dưới breadcrumb.
- Ban đầu dựng 100 hàng, tải thêm mỗi lần 100. Đây là incremental rendering,
  không phải virtualization hoặc thay hợp đồng phân trang backend. Các trang
  API vẫn được đọc đủ để tìm kiếm/lọc không bỏ mất task ở cuối danh sách.
- Hàng có tiêu đề liên kết, badge trạng thái, mã rút gọn sao chép nguyên mã,
  loại, việc con, thời gian tương đối và người giữ. Avatar chỉ dùng chữ đầu từ
  tên thật. Lỗi một dòng mở rộng nguyên văn và sao chép được.
- Dùng `/` tìm kiếm, `j/k` chuyển hàng, Enter mở liên kết đang focus, `N` mở
  form. Phím tắt tắt khi nhập liệu hoặc có dialog/popover. Tải thêm giữ focus;
  quay về từ chi tiết khôi phục hàng chọn và vị trí cuộn.
- Form disclosure giữ lựa chọn agent/scenario/type, báo yêu cầu trống tại ô
  nhập và giữ dữ liệu khi lỗi. Nút disabled/aria-busy trong khi gửi. Vẫn tạo
  với `start_workflow:false`, sau đó gọi endpoint run hiện có; lỗi run không
  biến task đã tạo thành task đang chạy thành công.
- Hồ sơ có summary, callout trạng thái/lỗi, mã sao chép, panel kết quả/yêu cầu,
  các bước/phân công/quyết định và nhật ký. Output có `scripted:true` được
  ghi rõ là mô phỏng. Nhật ký dùng UI.logViewer cho xuống dòng/sao chép,
  giữ lọc/phân trang sự kiện cũ; nhãn nêu rõ chỉ sao chép trang đang hiển thị.

## Triển khai và giả định

`work-ui.js` giữ các hàng theo ID và signature dữ liệu/ngôn ngữ. Poll không
thay node không đổi, giữ focus và disclosure; hàng đã xóa được bỏ khỏi cache.
`loadRegister()` gộp request đồng thời, báo lỗi hoặc thiếu trang thay vì dựng
bốn chỉ số 0. Nếu corpus thay đổi giữa các GET, không bịa hàng để khớp total.
Danh sách chính dùng cuộn trang; danh sách phụ workflow/feed không phải cùng
register và chưa được chuyển toàn bộ renderer ở giai đoạn này.

`UI.logViewer` có tùy chọn `filter:false` và `copyLabel`; mặc định tương thích
style guide. Số theo vi-VN/en-GB; thời gian theo Intl.RelativeTimeFormat.
CSS mới scope trang, dùng layer/token semantic, không thêm `!important`.
SVG phân công là group có nhãn để screen reader tiếp cận từng liên kết task.
Không tạo bulk action/backend capability chưa có.

## Bằng chứng trình duyệt

- [Receipt chính](../../artifacts/ui-shots/04-work/stage-4-gate.json):
  **131 checks, 44 ảnh**, 24 tổ hợp màu/mode/density; axe không violation trong
  các scope được audit của danh sách/form và năm phần hồ sơ. Kiểm mobile
  360/390px, tablet 768px, clipboard, keyboard, selection, empty/error,
  5.000 hàng fixture, escaping và reduced motion. Không chứng nhận toàn app.
- [Đo trước/sau và form](../../artifacts/ui-shots/04-work/performance-and-form.json):
  **14 checks PASS**, thêm 1920px, pending GET/skeleton, create/run thành công
  và create thành công/run thất bại. POST được fulfill tại browser, không tới
  server; fixture `UI FIXTURE` không chứng minh model hoặc workflow thật.
- [Bộ ảnh](../../artifacts/ui-shots/04-work/index.html) có trước/sau, cả ma trận
  và các trạng thái. Browser chặn host ngoài localhost và các phương thức ghi
  không được fixture xử lý; không ghi nhận runtime pageerror.
- Node syntax/boot PASS, 13 request chỉ đọc. DOM shim không chứng minh layout.
- Lint PASS (417 files), typecheck PASS (177 sources), full suite một lượt:
  **3.477 passed, 8 skipped, 1 deselected**, 10 cảnh báo openpyxl, 547,38s.
  API khảo sát đã dừng; không thêm E2E broker vì không thay handler nghiệp vụ.

Phép đo một lượt Chromium trên cùng corpus **1.193 task**, baseline commit
`7a7c860`, sau khi font sẵn sàng:

| Phép đo | Trước | Sau |
|---|---:|---:|
| Điều hướng đến có hàng, gồm GET/font/boot | 1.882 ms | 1.326 ms |
| Số hàng DOM ban đầu | 1.193 | 100 |
| Dựng lại từ cache | 31 ms | 5,1 ms |
| Frame p95 khi cuộn hết 1.193 hàng | 16,9 ms | 20,6 ms |
| Long task trong đoạn cuộn | 0 | 0 |
| Cuộn lồng trong register | Có | Không |

Đây không phải benchmark nhiều lượt hoặc SLA: startup vẫn có long task, frame
p95 không cải thiện trong mẫu này. Các quan sát không được biến thành tuyên bố
“mọi thao tác dưới 16 ms”. Với 5.000 hàng fixture, tìm kiếm 12,9 ms, cập nhật
cache 14,3 ms; sau khi tải đủ, cập nhật 45,1 ms và batch cuối 80,4 ms (receipt
chính là nguồn số liệu). Tải đủ 5.000 hàng qua 49 lần bấm trong cùng JS turn
mất khoảng 2,83s; đây là stress fixture, không phải hành vi người dùng thường.

## Chạy lại

Với API khảo sát fake/hash, auth off trên localhost:8100:

```sh
uv run --with playwright python scripts/ui-work-gate.py --org ORG_ID --chromium CHROMIUM_PATH
uv run --with playwright python scripts/ui-work-performance.py --org ORG_ID --chromium CHROMIUM_PATH
```

Performance script đọc baseline từ Git vào thư mục tạm, ghép HTML và dùng GET
corpus hiện tại. Không checkout/reset hay tạo task trong DB. Các ảnh form chỉ
là frontend fixture. Không chạy full verify_page.mjs vì có POST vận hành.

## Tiếp theo

Giai đoạn 5 chuyển từng trang, một trang một commit: Sự cố → Phê duyệt → Tổ
chức/đơn vị → Quy trình và màn con → Thư viện → Dữ liệu nghiệp vụ → Vận hành →
Cài đặt. Giữ controller/approval authority; sửa deep link đơn vị tại frontend
theo key thật. Mỗi trang có trước/sau, empty/loading/error, VI/EN, keyboard,
mobile và gate trong scope. Giai đoạn 6 làm command palette/a11y/motion toàn
app; Giai đoạn 7 mới dọn CSS chết và đóng Định nghĩa Hoàn thành.
