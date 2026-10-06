# Kiểm tra chất lượng phòng ban

Bộ dữ liệu v1 có 21 tình huống tổng hợp cho HR, Procurement, Design, QA/QC-HSE,
Finance, Sales và IT hỗ trợ onboarding. Không dùng hồ sơ người thật hoặc dữ liệu
Bãi Tràm. Bảy tình huống đủ nguồn dùng cho luyện tập. Mười bốn tình huống thiếu
nguồn hoặc có chỉ dẫn độc hại dùng để kiểm tra khả năng áp dụng bài học.

## Chạy và đọc kết quả

1. Tạo lại dữ liệu bằng `uv run python -m scripts.evaluate_organization --generate`.
   `tests/unit/test_organization_eval.py` xác nhận dữ liệu đã lưu khớp generator.
2. Chọn tenant có bảy agent mặc định. Chạy kiểm tra cơ chế:

```sh
uv run python -m scripts.evaluate_organization --org <org_id> --runtime mock \
  --lessons --output .devdata/reports/organization-mock.json
```

3. Đo chất lượng model qua OpenRouter trên cùng dữ liệu:

```sh
AO_MODEL_PROVIDER_DEFAULT=openrouter uv run python -m scripts.evaluate_organization \
  --org <org_id> --runtime live --lessons \
  --output .devdata/reports/organization-live.json
```

4. Đánh giá riêng phần holdout với bài học của các ca train đạt yêu cầu:

```sh
AO_MODEL_PROVIDER_DEFAULT=openrouter uv run python -m scripts.evaluate_organization \
  --org <org_id> --runtime live --split holdout --candidate-lessons \
  --output .devdata/reports/organization-candidate.json
```

Mỗi dòng in case, PASS hoặc FAIL và task ID. Báo cáo JSON giữ kết quả gốc, lỗi
chấm, thời gian chạy và liên kết bài học. Mã thoát khác 0 khi có ca không đạt.
Dùng `--case finance-normal-v1` để chạy lại một ca. Mỗi lượt tạo task mới và giữ
lịch sử. Cùng corpus, runtime và kết quả kiểm tra thì dùng lại đề xuất bài học.
Kết quả kiểm tra thay đổi tạo đề xuất riêng, giữ bài học thất bại trước đó.
Số model call đếm các phản hồi có ModelUsage; HTTP rejection chưa có usage được
ghi riêng qua lỗi execution, không được coi là model đã trả lời.

## Phân biệt kết quả

`mock` phát lại đáp án mẫu. Nó kiểm tra task, execution, audit, bộ chấm và việc
lưu bài học. Nó không đo suy luận của model. `live` chạy model thật trên dữ liệu
tổng hợp và chỉ giữ các candidate OpenRouter có hậu tố `:free`. Không fallback
sang đáp án mẫu hoặc model trả phí. Hai chế độ đều không thực hiện hành động
gửi mail, mua hàng, thanh toán hoặc cấp tài khoản.

Bộ chấm kiểm tra quyết định theo nguồn, các giá trị nghiệp vụ, trích dẫn đúng
tài liệu, nguồn còn thiếu và việc giữ cổng người duyệt. Nó chấp nhận bảng giải
thích bổ sung khi các giá trị cần kiểm tra đúng. Chất lượng diễn giải và các
khẳng định bổ sung vẫn cần người chuyên môn đánh giá. Đáp án mẫu do kỹ thuật
thiết kế, chưa phải chuẩn chất lượng được trưởng phòng ký duyệt.

## Bài học

`--lessons` lưu một SkillVersion chưa xuất bản từ mỗi ca train, gồm quy tắc
luyện tập và kết quả kiểm tra quan sát được. Đây là đề xuất theo chương trình
luyện tập, không phải model tự viết lại hướng dẫn hay cập nhật trọng số.
`derived_from` ghi task ID, hash corpus, runtime, kết quả và `development_only`.
`test_results` không được tự gán PASS để vượt cổng xuất bản.

`--candidate-lessons` đưa các đề xuất train đạt yêu cầu vào đầu vào của một
thí nghiệm tổng hợp. Mỗi ca train dùng tối đa một đề xuất đạt yêu cầu mới nhất,
không lặp lại hướng dẫn từ các bản lịch sử. Nó không xuất bản hoặc gắn skill
vào agent production.
Ca holdout không sinh bài học. Bài học từ mock không được dùng để đánh giá live.
So sánh kết quả holdout trước và sau, có kiểm tra của người chuyên môn, trước
khi quyết định xuất bản. Một lượt chạy tốt không chứng minh đã học bền vững.

Protocol 2 ghi hash output schema theo từng case và khai báo vocabulary Sales
`draft`, `sent`, `blocked`. Quyết định chờ người duyệt nằm ở `decision`, không
ghép thêm vào tên trạng thái. Không so trực tiếp kết quả từ contract khác nhau
để tuyên bố bài học làm model tốt hơn. Injection holdout vẫn dùng dữ kiện gốc
của train; cần hồ sơ và số liệu độc lập do chuyên gia duyệt cho mốc tiếp theo.

Workflow MEP 21 bước và procurement 13 bước còn được chạy bằng controller gốc
trong `tests/integration/test_business_workflow.py`. Test MEP dùng transport
file-drop được ghi rõ, đọc nội dung CV và ghi/đọc lại năm hồ sơ onboarding trong
sandbox. SMTP và IMAP thật cần một đợt nghiệm thu tích hợp riêng.

## So sánh candidate từ feedback

Candidate phải là SkillVersion chưa xuất bản, `lesson_scope=proposal_only`, có
campaign HR/procurement nguồn. Runner lấy đúng phòng ban, giữ gold ngoài task
input, đổi thứ tự baseline/candidate mỗi lượt và không bind hướng dẫn vào agent:

```sh
AO_MODEL_PROVIDER_DEFAULT=openrouter uv run -m scripts.evaluate_feedback_skill \
  --org <org_id> --version <skill_version_id> --runtime live --rounds 2 \
  --output .devdata/reports/feedback-skill-live-paired.json
```

Thay `--runtime mock` để kiểm tra cơ chế, không cần provider live. Live thiếu
adapter OpenRouter bị từ chối trước khi tạo task. Báo cáo có task/hash output,
round/arm/split, lỗi, độ trễ, token và chi phí. Receipt lưu trong SkillVersion và
audit cùng transaction; file JSON là bản xuất, không thay ledger database.

`receipt.passed` mô tả candidate qua corpus và không có regression quan sát được.
Nó không có nghĩa đã được phép publish: cổng publish còn yêu cầu mọi task hai
nhánh hoàn thành, usage OpenRouter free thật, receipt audit đúng hash, ít nhất hai
lượt và nguồn xác nhận chuyên gia. Vì thế baseline lỗi hạ tầng vẫn chặn publication
ngay cả khi candidate đạt. Corpus tổng hợp v1 chưa được chuyên gia ký duyệt.
Development-only luôn bị từ chối publish; `test_results.passed=true` gửi từ client
không thể bỏ qua cổng của feedback. Chỉ version đã publish mới được bind qua API.
Giữ cả lượt thất bại; khi thử lại tạo task/receipt mới và audit giữ lịch sử trước.
