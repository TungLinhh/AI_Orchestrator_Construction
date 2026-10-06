# Kế hoạch workflow O-Nexus

Ngày 2026-10-06. Thứ tự do chủ sở hữu xác nhận: tuyển kỹ sư MEP đến onboarding, procurement, rồi các phòng ban còn lại. Không dùng dữ liệu Bãi Tràm.

## Điều khiển hiện có

Console → Processes → Workflows có hai cách tạo: bài nghiệm thu dùng dữ liệu hư cấu, hoặc Boss nhập brief tuyển MEP thật (một vị trí, khoảng lương, ngày bắt đầu, yêu cầu). Tạo chưa tự chạy. Mỗi lượt có nút chạy/tiếp tục, cây task, log, sản phẩm, nguồn O-Nexus và link tới agent phụ trách. Đợt MEP hiển thị hộp thư đã kết nối cùng subject riêng; luồng thật chờ quyết định ở Approvals và cho người phụ trách nộp hồ sơ nguồn tại bước cần chứng cứ. Đợt lỗi hiển thị nguyên nhân và tạo lượt retry riêng, giữ lịch sử cũ.

Nghiệm thu model thật bằng `AO_MODEL_PROVIDER_DEFAULT=openrouter uv run python scripts/run_business_workflow.py --org <org_id> --kind mep_hiring --output .devdata/reports/mep.json` (hoặc `--kind procurement`). `--resume <root_id>` tiếp tục lượt chưa kết thúc; `--retry <failed_root_id>` tạo lượt mới và chỉ dùng lại sản phẩm được kiểm chứng. Kiểm toán độc lập bằng `uv run python scripts/audit_business_workflow.py --org <org_id> --root <root_id> --output .devdata/reports/audit.json`. Mật khẩu mail chỉ nằm trong cấu hình bí mật; không nhập vào brief hay sản phẩm workflow.

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


## Các mốc tiếp theo và task giao được

Các task dưới đây là backlog có điều kiện nghiệm thu, chưa phải execution đã chạy. Chỉ tăng quyền tự chủ sau khi bộ đánh giá của mốc đó đạt yêu cầu; mức phê duyệt thật không được thay bằng log mô phỏng.

### Mốc 1 — Tuyển MEP với người và CV thật

1. **HR-01, Boss/HR:** nhập brief đợt tuyển bằng form: chuyên ngành HVAC/điện/cấp thoát nước, số lượng, kinh nghiệm, địa điểm, dải lương, ngày bắt đầu, người duyệt. Đầu ra: brief có phiên bản, JD và rubric được duyệt đúng hash. Sửa brief tạo phiên bản và yêu cầu duyệt lại sản phẩm bị ảnh hưởng.
2. **HR-02, IT/HR:** cấu hình subject/mã đợt và hiển thị địa chỉ tiếp nhận, receipt, lỗi extraction và CV trùng. Native monitor tiếp tục luồng khi có CV đúng mã; kiểm tra PDF có text, DOCX và URL HTTPS công khai với fixtures riêng. PDF ảnh chuyển trạng thái cần OCR/kiểm tra, không coi file rỗng là CV đã đọc.
3. **HR-03, HR:** từng CV được đánh giá riêng theo rubric đã duyệt; hệ thống tính tổng và lưu căn cứ, phần chưa xác minh, câu hỏi phỏng vấn. Bộ đánh giá có CV mạnh/yếu, thiếu kinh nghiệm, injection và căn cứ sai; không dùng tên/tuổi/giới tính làm điểm.
4. **HR-04, người phỏng vấn/HR:** form hai vòng lưu ngày, người, câu trả lời, đánh giá và nguồn; shortlist thay đổi phải có lý do. Chưa đủ cả hai vòng hoặc chưa đạt ngưỡng không chuyển sang đề xuất tuyển.
5. **HR-05, CEO/HR:** hộp phê duyệt thật có thông báo số việc đang chờ, bản xem trước, chấp thuận/từ chối/hỏi thêm; quyết định gắn đúng sản phẩm. Soạn offer, ghi nhận chấp nhận đúng offer hash; phát hành và ký là hành động riêng sau duyệt thật.
6. **HR-06, HR/IT/HSE/mentor:** checklist ngày đầu ghi hồ sơ, induction, quyền theo vai trò và kiểm thử quyền, bàn giao mentor. Nhắc mốc 30/60/90 và ghi đánh giá vào đúng ngày. Chỉ đóng onboarding thật khi có chứng cứ thật; tài khoản sandbox không được tính là tài khoản production.
7. **HR-07, QA:** audit độc lập cây task/execution/event/audit, file hash, điểm, cổng duyệt và kết quả onboarding. Kiểm tra resume sau restart, từ chối, thông tin bổ sung, thay đổi offer và email lỗi. Báo cáo phân biệt hành động đã xảy ra, dự thảo và lịch tương lai.

### Mốc 2 — Procurement với hồ sơ vật tư thật

1. **PRC-01, Boss/Procurement:** nhận BOQ có material_id, đơn vị, số lượng, spec, mốc cần hàng và long-lead. Dòng thiếu thông tin tạo yêu cầu bổ sung, chưa phát RFQ.
2. **PRC-02, Procurement:** lập RFQ và sổ ba báo giá; giữ file/hash, ngày hiệu lực, tiền tệ, thuế, giao hàng, bảo hành. Thiếu ba NCC phải có quyết định ngoại lệ, không tạo báo giá giả.
3. **PRC-03, Procurement/Finance/HSE:** thẩm định pháp lý, tài chính, HSE; mỗi kết luận xanh/vàng/đỏ dẫn hồ sơ nguồn. NCC đỏ bị chặn; NCC vàng có điều kiện và người chịu trách nhiệm.
4. **PRC-04, người phòng vật tư/QA:** đánh giá từng cặp NCC–vật tư theo spec/chứng chỉ, lưu chấp thuận chất lượng thật. Không dùng tổng giá thấp để vượt cổng chất lượng.
5. **PRC-05, Procurement/CEO theo DOA:** bảng so sánh đủ mọi dòng, tổng giá và điều kiện thương mại; nhật ký đàm phán gắn báo giá sửa đổi; người có thẩm quyền duyệt phương án đúng hash. PO chỉ phát hành sau quyết định thật.
6. **PRC-06, kho/QA/Finance:** ghi GRN từ bằng chứng nhận hàng độc lập, inspection và invoice từ nhà cung cấp. Đối chiếu từng dòng PO–GRN–invoice; giao thiếu/đổi spec/lệch giá giữ pending hoặc tạo NCR, không điền bằng dữ liệu PO.
7. **PRC-07, QA:** audit đủ BOQ/NCC/chất lượng/DOA/PO/GRN/invoice và thử các ngoại lệ. Kết quả chuẩn bị mua, phát hành PO, nhận hàng và thanh toán là bốn trạng thái khác nhau; chỉ ghi trạng thái có chứng cứ.

### Mốc 3 — Design

**DES-01** Boss giao brief và bộ bản vẽ có phiên bản → **DES-02** Design lập register, kiểm tra thiếu bản vẽ và yêu cầu kỹ thuật → **DES-03** rà phối hợp HVAC/điện/nước, mỗi clash gắn sheet/vị trí/nguồn → **DES-04** tạo RFI và phương án sửa có tác động khối lượng/tiến độ → **DES-05** người thiết kế duyệt phương án → **DES-06** soạn bộ phát hành, kiểm tra phiên bản và bảng thay đổi → **DES-07** QA kiểm toán mọi clash đã giải quyết hoặc có lý do giữ mở. Nghiệm thu: không có kết luận không chỉ được nguồn, không tự phát hành thiết kế thật, không bỏ clash để báo hoàn thành.

### Mốc 4 — QA/QC-HSE

**QA-01** nhận spec/ITP đã duyệt → **QA-02** tạo hold/witness points và checklist có tiêu chí đo → **QA-03** thu chứng chỉ, kết quả inspection và ảnh/biên bản nguồn → **QA-04** đối chiếu và phát hiện NCR/rủi ro → **QA-05** đề xuất biện pháp khắc phục, owner và hạn → **QA-06** người chịu trách nhiệm duyệt, ghi inspection lại độc lập → **QA-07** nghiệm thu/giữ pending và bàn giao sổ chất lượng. Nghiệm thu: thiếu phép đo/chứng chỉ không được ghi đạt; NCR chỉ đóng sau kiểm tra lại; quyền dừng thi công và chấp nhận rủi ro do người quyết.

### Mốc 5 — Finance

**FIN-01** nhận hồ sơ đề nghị thanh toán → **FIN-02** kiểm đủ hợp đồng/PO/GRN/invoice và phiên bản → **FIN-03** tính số lượng, giá, thuế, giữ lại và tổng từ chứng từ → **FIN-04** đối chiếu 3-way và DOA → **FIN-05** tách ngoại lệ thành yêu cầu bổ sung, không tự đổi giá → **FIN-06** người có quyền duyệt đề nghị đúng hash → **FIN-07** lập đề xuất thanh toán, lưu đối chiếu và audit. Nghiệm thu: mọi số có công thức và nguồn, sai/thiếu chứng từ bị chặn, không thực hiện chuyển tiền từ simulation.

### Mốc 6 — Sales

**SAL-01** tiếp nhận yêu cầu/đơn khiếu nại và nguồn → **SAL-02** đối chiếu phạm vi hợp đồng và cam kết → **SAL-03** phân loại, giao đơn vị chuyên môn → **SAL-04** tổng hợp trả lời có căn cứ và phần chưa rõ → **SAL-05** tạo phương án, trách nhiệm, hạn xử lý → **SAL-06** người có quyền duyệt cam kết và bản gửi → **SAL-07** theo dõi xác nhận, đóng với chứng cứ hoặc giữ pending. Nghiệm thu: không suy diễn điều khoản, không tự gửi cam kết ngoài phạm vi được phép, không coi bản trả lời dự thảo là khách đã nhận.

### Mốc 7 — IT

**IT-01** nhận yêu cầu onboarding từ HR đã duyệt → **IT-02** lập quyền tối thiểu theo vai trò và danh mục hệ thống → **IT-03** người quản lý truy cập duyệt → **IT-04** cấp trong staging qua adapter, lưu receipt → **IT-05** kiểm đăng nhập và quyền cho phép/cấm → **IT-06** bàn giao, hướng dẫn và nhật ký → **IT-07** rà soát/thu hồi theo thay đổi nhân sự. Nghiệm thu: có bằng chứng cấp và kiểm thử thực tế, thử quyền từ chối, không cấp production bằng quyết định mô phỏng.

Ở mỗi mốc, bổ sung bộ input có phiên bản, kết quả chuẩn do người xét và ít nhất một lỗi buộc dừng ở từng cổng; đánh giá theo việc hoàn thành bước và căn cứ, không chỉ theo số trường JSON. Các adapter production và giao tiếp với ứng viên/NCC/khách hàng cần phạm vi phát hành được xác nhận cho đợt thực tế.

Tham chiếu cơ chế nộp artifact: [OpenRouter tool choice](https://openrouter.ai/docs/guides/features/tool-calling). Runtime yêu cầu đúng hàm submit_evidence và vẫn kiểm tra nội dung độc lập; ép gọi hàm không phải bảo đảm chất lượng kết luận.
