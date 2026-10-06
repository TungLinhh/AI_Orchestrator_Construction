# Kết quả nghiệm thu packet v1 — 2026-10-06

Đây là nghiệm thu **simulation** với model OpenRouter và SMTP/IMAP thật.
CV, ứng viên, phỏng vấn, quyết định HR/Boss/vật tư, onboarding, NCC và giao nhận
đều hư cấu. Không có Approval người thật, tuyển người thật, PO phát hành,
thanh toán hoặc quyền truy cập production.

| Lượt | Kết quả | Kiểm toán độc lập |
| --- | --- | --- |
| MEP đầu `tsk_01m49qxzw9479yd9bdpt3rnmay` | Failed, 12/21; Dots content filter từ chối selection | Auditor từ chối vì root chưa hoàn thành; lỗi và điểm 100/42/13 giữ nguyên |
| MEP phục hồi `tsk_01m49rbvz150bgarv6zaetx28m` | Completed, 21/21; điểm 90/50/3; chỉ CV mạnh vượt ngưỡng 70 | PASS 112 kiểm tra, gồm hash packet, CV gửi/nhận/tải về, trích dẫn, tính lại điểm, execution/event/audit và năm file onboarding |
| Procurement `tsk_01m49qyv1fv2naxt39acw0rz7j` | Completed, 13/13; đủ ba vật tư; tổng dự thảo 77.000.000 VND | PASS 76 kiểm tra, gồm packet, nguồn model, loại NCC đỏ, tính lại tiền và đối chiếu PO–GRN–Invoice |

Chỉ thực hiện một lượt phục hồi cấp workflow, không lặp đến khi xanh. Giữ nguyên
brief/mode/nguồn/ngưỡng. Controller kiểm tra lại và tái sử dụng JD/rubric có nguồn
model thật; lần nhận mail mới làm prior thay đổi nên scoring không được tái sử
dụng. Lượt phục hồi gửi/nhận lại CV và model chấm lại. Điểm biến thiên giữa hai
lượt được giữ nguyên, không chỉnh để khớp kỳ vọng. Dots tự trả artifact hợp lệ
ở selection trong lượt phục hồi; không thay bộ lọc hoặc model chính.

Có 24 phản hồi được ghi usage trên ba lượt, gồm một `finish_reason=error`.
Bảy submission bị từ chối do artifact thiếu, trích dẫn không khớp, quotation
không hợp lệ, ngôn ngữ không nhất quán hoặc phản hồi lỗi/cắt dở. Validator và
retry/fallback có giới hạn xử lý các submission này. Content-filter refusal
khiến lượt đầu thất bại, không kích hoạt fallback tự động.

Tất cả model có usage là OpenRouter `:free`: Dots, Ling và Nemotron. Ledger ghi
170.977 token input + completion, gồm usage của phản hồi lỗi; 6.336 cache-read
là phần con của input, không cộng lần nữa. Chi phí USD 0 được ghi nhận. Đây không
phải tổng mọi HTTP attempt: refusal/transport failure không có usage không cung
cấp số token đáng tin để cộng. Không suy ra số liệu hóa đơn ngoài từ các row này.

Sản phẩm model vẫn cần chuyên gia kiểm tra nội dung. Ví dụ bản đề xuất thêm điều
kiện thử việc ba tháng khi chưa có chính sách HR đã duyệt trong packet. Đó là đề
xuất chưa xác minh trong sandbox, không phải điều khoản hợp đồng được phép áp
dụng thật. Nghiệm thu này kiểm chứng workflow và các căn cứ có thể kiểm tra,
không chứng minh mọi khuyến nghị văn bản đã đạt chuẩn nghiệp vụ/pháp lý.

Năm hành động onboarding tạo file sandbox có hash/read-back; không tạo tài khoản
thật. Mốc 30/60/90 chỉ scheduled. Procurement chọn A cho PIPE/PUMP, B cho CABLE;
NCC C không được trao hàng. PO vẫn `draft_not_issued`, payment false.

Các báo cáo đầy đủ nằm ở workspace:

- `.devdata/reports/packet-v1-mep.json` và log cùng tên: lượt thất bại.
- `.devdata/reports/packet-v1-mep-recovery.json`, `packet-v1-mep-recovery-audit.json`.
- `.devdata/reports/packet-v1-procurement.json`, `packet-v1-procurement-audit.json`.
- `.devdata/reports/packet-v1-readback.json`: đọc lại ledger PostgreSQL.
- `.devdata/logs/packet-v1-*.log`: từng stage, phản hồi, refusal và phục hồi.

[Lệnh tái hiện và nguồn mẫu](README.md). Mọi lượt chạy lại phải giữ báo cáo lỗi;
nghiệm thu thật tiếp theo cần nguồn thật, người duyệt thật, policy nội dung đã
xác nhận và adapter có receipt/idempotency theo FUTURE_WORK.md.
