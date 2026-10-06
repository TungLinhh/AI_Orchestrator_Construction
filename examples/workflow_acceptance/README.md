# Hồ sơ nghiệm thu tổng hợp O-Nexus

Bộ v1 là hồ sơ **hư cấu** do dự án tự soạn, theo yêu cầu nghiệm thu bằng tài liệu
mẫu của người dùng. Không chứa CV người thật, chữ ký người thật, chứng chỉ thật
hoặc tài liệu của Bãi Tràm. Mọi quyết định HR/Boss/vật tư và tiếp nhận nhân sự,
giao nhận hàng đều là mô phỏng. Mẫu công khai chỉ gợi ý cấu trúc; SOP O-Nexus
đã seed mới là nguồn quy trình trong ứng dụng.

Nguồn đã đọc ngày 2026-10-06:

- [Acas: job description templates](https://www.acas.org.uk/job-description-templates):
  tham khảo cách tổ chức thông tin vai trò, trách nhiệm và năng lực.
- [Acas: job offer templates](https://www.acas.org.uk/job-offer-templates):
  tham khảo phần điều kiện, ngày bắt đầu và xác nhận acceptance.
- [Acas: planning an induction programme](https://www.acas.org.uk/inductions/planning-an-induction-programme):
  tham khảo mentor, thiết bị, HSE, đào tạo và review sau ngày đầu.
- [World Bank: Evaluating Bids and Proposals, February 2025](https://thedocs.worldbank.org/en/doc/9dcb7971706bf29b2732779c39922b77-0290012025/original/Evaluating-Bids-and-Proposals-with-Rated-Criteria-Feb-4-2025.pdf):
  tham khảo việc xác định tiêu chí trước, kiểm tra năng lực/kỹ thuật, rồi so sánh
  thương mại và lưu căn cứ đánh giá. Không tuyên bố tuân thủ pháp luật hay quy
  định đấu thầu World Bank cho giao dịch Việt Nam.

## Hồ sơ và kỳ vọng

`v1/manifest.json` liệt kê nguồn tham khảo và SHA-256 của từng file. Hash kiểm tra
sự nhất quán của bộ hồ sơ, không chứng minh chữ ký hoặc chứng chỉ là thật.
`.gitattributes` giữ nguyên byte của packet trên Windows/Linux; CV C giữ một
khoảng trắng nguồn đã nằm trong hash/receipt, không được trim sau nghiệm thu.

- `hiring/`: nhu cầu định biên, CV mạnh, CV yếu, CV chứa prompt injection, nguồn
  vai trò/induction, hai vòng phỏng vấn, acceptance và chính sách duyệt mô phỏng.
  CV mạnh có dẫn chứng kỹ thuật hư cấu chi tiết; CV yếu và injection giữ thiếu sót
  có chủ ý. Không thay ngưỡng 70 hay sửa điểm sau khi model chấm.
- `procurement/`: BOQ ba vật tư, ba bộ hồ sơ pháp lý/tài chính/HSE/báo giá,
  chứng chỉ mô phỏng và GRN/invoice nguồn. NCC C rẻ hơn nhưng không đủ điều kiện;
  quy trình phải từ chối trao hàng cho NCC đỏ.
- `briefs/`: dữ liệu chạy tương ứng. Controller tiếp tục giữ lại nguồn của bước
  tương lai, chỉ đưa cho model khi bước đó cần.
- `expectations.json`: kỳ vọng độc lập, không nhập vào brief/model. Auditor tính
  lại điểm/tổng tiền, kiểm tra trích dẫn nguyên văn, hash CV, thứ tự bước,
  execution/event/audit, nguồn model thật và không có Approval giả.

## Chạy lại

Tạo/kiểm tra bộ dữ liệu có kết quả xác định:

```sh
uv run -m scripts.workflow_acceptance_packet --out examples/workflow_acceptance/v1
uv run pytest tests/unit/test_workflow_acceptance_packet.py -q
```

Mỗi lần chạy lệnh dưới tạo campaign mới ở **simulation**, dùng provider thật.
MEP gửi ba email CV thử về chính hộp thư đã kết nối; cần cấu hình mail đang hoạt
động. Không gửi đến ứng viên/NCC. Quyền và secret giữ ngoài Git. Không kết hợp
`--packet` với `--retry`/`--resume`; không sửa nguồn của lượt cũ.

```sh
AO_MODEL_PROVIDER_DEFAULT=openrouter uv run -m scripts.run_business_workflow \
  --org <org_id> --kind mep_hiring --packet examples/workflow_acceptance/v1/manifest.json \
  --output .devdata/reports/packet-v1-mep.json
AO_MODEL_PROVIDER_DEFAULT=openrouter uv run -m scripts.run_business_workflow \
  --org <org_id> --kind procurement --packet examples/workflow_acceptance/v1/manifest.json \
  --output .devdata/reports/packet-v1-procurement.json
uv run -m scripts.audit_business_workflow --org <org_id> --root <mep_root> \
  --packet examples/workflow_acceptance/v1/manifest.json \
  --output .devdata/reports/packet-v1-mep-audit.json
uv run -m scripts.audit_business_workflow --org <org_id> --root <procurement_root> \
  --packet examples/workflow_acceptance/v1/manifest.json \
  --output .devdata/reports/packet-v1-procurement-audit.json
```

Giữ cả báo cáo thất bại và artifact bị từ chối. Model free có thể lỗi/chậm;
controller chỉ nhận sản phẩm qua validation và retry/fallback có giới hạn.
Nghiệm thu này chứng minh đường chạy phần mềm với hồ sơ thử; chưa chứng minh
năng lực tuyển dụng/mua hàng thật. Review 30/60/90 ngày chỉ được lên lịch,
không đánh dấu hoàn thành sớm. Bài học từ dữ liệu thử không được tự xuất bản.
