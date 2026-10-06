# Kế hoạch workflow O-Nexus

Ngày 2026-10-06. Thứ tự do chủ sở hữu xác nhận: tuyển kỹ sư MEP đến onboarding, procurement, rồi các phòng ban còn lại. Không dùng dữ liệu Bãi Tràm.

## Tuyển MEP

Boss giao brief và định biên → xác nhận nhu cầu → JD sáu phần → duyệt JD → rubric có trọng số và căn cứ → duyệt rubric → nhận email theo mã đợt tuyển → tải/extract CV → chấm từng tiêu chí kèm trích dẫn chính xác → HR xem shortlist → phỏng vấn vòng kỹ thuật → phỏng vấn vòng HR → CEO duyệt lựa chọn và lương → soạn offer → xác nhận nhận offer → onboarding ngày đầu và kế hoạch 30–60–90 → kiểm tra đầy đủ chứng cứ và đóng workflow.

Nguồn: ONX-BO-HR-SOP-004 trong application/playbook.py; JD/Rubric từ domain/hiring_process.py. Nội dung JD MEP, trọng số và ngưỡng được thiết kế cho đợt tuyển, không được gán thành nguyên văn SOP. Thiếu bằng chứng ghi chưa xác minh; không suy diễn từ tên, tuổi, giới tính. Điểm được tính lại bằng mã, căn cứ phải tồn tại trong CV. Hai vòng phỏng vấn và offer/acceptance ở bản thử dùng fixture được ghi rõ.

Gmail: SMTP SSL gửi CV thử về chính hộp thư được chủ sở hữu chỉ định; IMAP SSL mở INBOX read-only và BODY.PEEK theo subject mã đợt. Không đọc email khác, không đánh dấu đã đọc, không đưa mật khẩu vào báo cáo/repository. Attachments được giới hạn dung lượng, đặt tên theo hash; CV là nội dung không đáng tin, không phải chỉ dẫn cho agent. Chỉ tải URL HTTPS công khai, không mạng nội bộ hoặc redirect chưa kiểm tra. Chứng cứ gồm Message-ID, UIDVALIDITY/UID, SHA-256, tên file và trạng thái extraction.

Nguồn kỹ thuật: https://developers.google.com/workspace/gmail/imap/imap-smtp ; https://support.google.com/accounts/answer/185833 ; https://docs.python.org/3/library/imaplib.html ; https://docs.python.org/3/library/smtplib.html .

## Procurement

BOQ và yêu cầu kỹ thuật → kiểm tra đầy đủ vật tư → RFQ tối thiểu ba NCC → hồ sơ pháp lý/tài chính/HSE → phân loại xanh/vàng/đỏ → QA đánh giá chứng chỉ và từng vật tư → người phòng vật tư duyệt chất lượng → so sánh tổng giá, bảo hành, giao hàng và long-lead → đàm phán → cấp DOA duyệt đề xuất → PO dự thảo → theo dõi giao hàng/GRN → đối chiếu PO–GRN–Invoice → hồ sơ sẵn sàng mua.

Nguồn ONX-MO-PRC-SOP-005/006. Không shortlist NCC đỏ; mọi điểm dưới ngưỡng có lý do; không phát hành đơn mua thật hoặc thanh toán trong thử nghiệm. Fixture phải đủ mỗi dòng BOQ, ba báo giá, chứng chỉ, GRN và invoice; thiếu hoặc lệch phải dừng và ghi lỗi, không tự điền dữ kiện.

## Quy tắc chạy và nghiệm thu

Mỗi bước là task bền vững, có chủ sở hữu, phụ thuộc, input từ bước trước, output và execution/event/audit. Chạy lại dùng bước đã hoàn thành; bước lỗi hoặc chưa duyệt ngăn bước sau. Phê duyệt thử ghi “phê duyệt mô phỏng”, actor system, mode simulation; không tạo quyết định của người giả. Live chờ approval thật gắn hash bản được duyệt. Hoàn thành simulation không được tính là hoàn thành không cần người ở production.

Chạy model thật cho JD, rubric, đánh giá, đề xuất, offer và onboarding; lưu model_usage thực. Email phải được gửi và nhận thật; lỗi SMTP/IMAP không được thay bằng fixture rồi báo thành công. Onboarding simulation hoàn thành các hành động trong môi trường thử, còn đánh giá 30/60/90 ngày ở tương lai được ghi lịch dự kiến; không tuyên bố đã trôi qua 90 ngày.

Nghiệm thu: mọi bước bắt buộc xong, không task bỏ dở, điểm tính đúng, mỗi kết luận có căn cứ, mỗi quyết định review ghi đúng chế độ, sản phẩm cuối đủ hồ sơ. Các kiểm tra lỗi gồm CV không đọc được, prompt injection, điểm/căn cứ giả, email trùng, không đủ ba NCC, NCC đỏ, thiếu vật tư, lệch 3-way match, chạy vượt cổng duyệt và cross-tenant. Kiểm tra lint → typecheck → toàn suite → E2E → page; sau đó chạy thực tế và lưu báo cáo riêng.

## Các phòng ban tiếp theo

| Phòng ban | Workflow cụ thể | Điều kiện chốt |
|---|---|---|
| Design | Brief → kiểm tra phiên bản bản vẽ → rà phối hợp MEP → clash/RFI → đề xuất sửa → người thiết kế duyệt → bộ bản vẽ phát hành dự thảo | Mỗi clash gắn bản vẽ và vị trí; không tự phát hành thiết kế thật |
| QA/QC-HSE | ITP → chứng chỉ → inspection checklist → NCR → biện pháp sửa → kiểm tra lại → người nghiệm thu | Thiếu bằng chứng giữ pending; lệnh dừng thi công do người quyết |
| Finance | Hồ sơ PO/GRN/invoice → 3-way match → tính thuế/tổng → kiểm tra DOA → người duyệt → đề xuất thanh toán → đối chiếu | Không trả tiền; sai số hoặc thiếu chứng từ chặn luồng |
| Sales | Yêu cầu khách hàng → đối chiếu hợp đồng → phân loại → hỏi đơn vị chuyên môn → soạn phản hồi → duyệt cam kết → lưu follow-up | Mọi cam kết có nguồn, không gửi ngoài phạm vi được phép |
| IT | Yêu cầu onboarding từ HR → quyền theo vai trò → duyệt truy cập → cấp trong staging → kiểm thử đăng nhập/quyền → bàn giao → rà soát | Không cấp production từ quyết định mô phỏng |

Sau hai workflow đầu, mở bộ đánh giá có phiên bản cho từng phòng ban và thử ngoại lệ trước khi tăng quyền. Điều kiện triển khai vẫn cần shadow thật ít nhất 28 ngày, đủ bốn tuần, đồng thuận ≥95%; không dùng simulation để thay traffic thật.
