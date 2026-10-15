# Kiểm tra tích hợp và khả năng hoàn thành task

> Cập nhật triển khai 15-10-2026: xem [SOURCE_WORKFLOW_STATUS.md](SOURCE_WORKFLOW_STATUS.md)
> cho luồng nguồn/mock, upload, executor và export đã viết, giới hạn và bằng chứng.
> Chuỗi execution hiện hành: NVIDIA Nemotron Ultra free → DOTS free; Ling/Inkling
> trong các snapshot bên dưới là lịch sử, đã bị thay thế. Các số test cũ không
> thay cho cổng kiểm chứng của thay đổi nguồn hiện tại.

Bằng chứng gốc: 2026-10-07; kiểm tra lại ngày 15-10-2026, giờ Việt Nam. Checkout:
`/home/vutun/.codex/worktrees/23e2/ai_orchestrator`.
Tenant được cấu hình và chạy probe: `org_01m3ycsehgwz7v1fk2ahswkwhm`.

Hệ thống đã hoàn thành được procurement tổng hợp qua controller và model thật.
Chưa nghiệm thu vận hành nghiệp vụ với tài liệu, người duyệt và connector thật.
Upload đa định dạng đã được triển khai trong đợt nguồn, xem liên kết cập nhật ở đầu
tài liệu. Provisioning theo nhu cầu và canary connector/tenant thật còn phải hoàn thiện.

## Kiểm tra lại ngày 15-10-2026

- Lỗi provenance đã sửa: `AgentRunResult` không có `model_name`; tên model ở
  `run.response.model_name`. Regression kiểm tra typed result; HTTP probe xác nhận
  output `winner=A`, `total_vnd=1520000`, execution và model usage đều có model.
- [Task có ledger](artifacts/product-review/2026-10-15/http-task-with-ledger.json)
  completed bằng DOTS free, ledger ghi `routing_reason=fallback`, provider OpenRouter,
  chi phí báo cáo $0. Probe dùng dữ liệu tổng hợp và không gửi mail/PO.
- [Benchmark mới](artifacts/product-review/2026-10-15/benchmark-current.json):
  Inkling 0/3 do 403; Ling không còn trong catalogue, không gọi và không suy luận
  chất lượng; DOTS 2/3, một ca bị từ chối do usage không nhất quán. Hai ca đạt
  khoảng 4,59–6,73 giây. Benchmark giờ lưu candidate không khả dụng, tiếp tục với
  model còn lại và chỉ tính percentile latency trên ca đạt.
- [Runtime snapshot](artifacts/product-review/2026-10-15/runtime.json): 11 checks
  PASS. Đây là GET/socket readiness; không thay acceptance nghiệp vụ.
- [Task backlog](artifacts/product-review/2026-10-15/task-backlog.json): 714 completed,
  176 failed, 181 canceled; 224 chưa terminal và 0 execution running. Có 11 root
  chưa terminal, 199 assigned children dưới parent assigned/waiting_for_input,
  14 approval children dưới parent failed. Backlog có cả fixture và campaign thử
  nghiệm. Cần reconciliation các approval cũ;
  không tự quyết thay người hoặc tự xóa lịch sử thất bại.
- Clean-install test preparation phát hiện thiếu role catalogue cho register:
  demo + process spine chưa đủ cho `seed-test-reference`. Gate lần này dùng
  catalogue sẵn có ở cluster gốc; đây là gap onboarding còn mở, không phải lỗi
  được che bằng test skip hoặc tạo role tùy ý.
- [Inventory source](artifacts/product-review/2026-10-15/surface.json) ghi hai chuỗi
  business: hiring 21 stages, procurement 13 stages; bốn HR templates và tám
  built-in tools. Có 123 route khai báo; không xem đó là 123 feature hoạt động.

Regression hiện có ba skip đã xác minh riêng bằng `-rs`: delegation frame không
phát sinh delegation; forbidden-zone dùng tenant chưa seed process spine;
delegation UI không có published event để render. Đây là coverage gap còn mở,
không phải ba ca đạt. Receipt nằm trong `skipped-details.log` cùng thư mục.

Kế hoạch mở rộng có contract và nghiệm thu nằm ở
[PRODUCT_COMPLETION_PLAN.md](PRODUCT_COMPLETION_PLAN.md). Upload/source picker,
artifact export thống nhất, tenant identity/provisioning, generic workflow definition
và các workflow pack mới chưa được triển khai trong lần audit này.


### Cổng kiểm chứng đã chạy lại ngày 15-10-2026

`make lint` và `make typecheck` đạt. `make test`: **3.508 passed, 3 skipped,
1 deselected**, 10 cảnh báo parser Excel. `make test-e2e`: **34 passed**, preflight
PostgreSQL/NATS/Temporal đạt. Regression tập trung bridge/fallback: **44 passed**.
Ba skip vẫn là gap đã mô tả, không tính là đạt. Hai task HTTP dùng model thật với
input tổng hợp đều completed; probe thứ hai đối chiếu cả execution và usage ledger.
[Receipt tổng hợp](artifacts/product-review/2026-10-15/review-results.json) giữ
command, kết quả, hash bằng chứng và danh sách việc còn mở.

## Model và fallback: bằng chứng đợt 07-10-2026

Những đánh giá Ling bên dưới chỉ áp dụng cho ngày 07-10; hiện trạng mới ở trên.


Chuỗi theo yêu cầu là `thinkingmachines/inkling-small:free` →
`inclusionai/ling-3.0-flash-sante:free` → `dots-studio/dots-3-note-preview:free`,
sau đó các fallback free trước đây. Cấu hình đã áp dụng cho primary, reasoning,
default, fast_general và reasoning_high của tenant nêu trên. Proposer giữ độc lập.

| Model | Benchmark đã hiệu chỉnh, 2 lượt × 3 ca | Quan sát |
| --- | --- | --- |
| Inkling Small | 0/6 | HTTP 403 do endpoint free chỉ cho agentic harness được chấp nhận; không có mẫu chất lượng để chấm |
| Ling Sante | 3/6 | Cả ba ca lượt đầu đạt, khoảng 1,98–2,69 giây; lượt hai rate limited |
| DOTS | 3/6 | Tool round trip đạt cả hai lượt; có sai artifact và inconsistent usage; các ca đạt khoảng 5,68–11,52 giây |

Kết luận vận hành hiện tại: giữ primary như yêu cầu nhưng chuyển ngay sang Ling
khi Inkling bị chặn. Ling là candidate phục vụ hữu ích đầu tiên trong phép đo.
DOTS đứng sau. Đây chưa phải xếp hạng chất lượng ổn định hay SLA free tier.
Không dùng tốc độ phản hồi 403/429 để kết luận model nhanh.

[Benchmark hiệu chỉnh](artifacts/model-readiness/2026-10-07/benchmark-calibrated.json)
giữ capability catalogue, từng output và từng lỗi. [Benchmark đầu](artifacts/model-readiness/2026-10-07/benchmark.json)
được giữ nguyên, gồm một lỗi so mã `decision` quá chặt trong khi prompt chưa nói
rõ phải trả mã. Lượt hiệu chỉnh nói rõ contract và bổ sung tool round trip, vì vậy
không lấy chênh lệch hai lượt làm bằng chứng model tự cải thiện.

Các sửa đổi runtime:

- Bounded candidate deadline bao gồm cả retry/backoff. Chuỗi execution mặc định
  dùng 30 giây/candidate, không retry transport, rồi thử model tiếp theo.
- HTTP 403 về quyền dùng model và 404 không retry cùng candidate. 401 dừng vì
  credential sai. Content-filter refusal vẫn dừng, không route quanh policy.
- Generic gateway loại empty completion, finish error, thiếu required tool,
  tool không tồn tại, arguments không phải JSON object và JSON output hỏng.
  Nonsense về nghiệp vụ cần validator theo task; không có bộ chấm prose chung.
- WorkflowEvidenceRuntime tiếp tục nhận raw response để lưu usage, sửa artifact,
  loại candidate và kiểm chứng nghiệp vụ. Không làm mất response lỗi đã được
  runtime này ghi sổ chỉ vì gateway nhận thêm validator.
- Generic agent ledger ghi cả completion bị từ chối; token usage trong bridge
  tính cả các completion này và reasoning. Khi hết candidate, lỗi vẫn giữ response
  đã trả để recorder lưu lại. Transport lỗi không trả usage thì không bịa usage.
- Capability đúng catalogue: Inkling/Ling dùng tools nhưng không quảng bá strict
  JSON Schema. Request có `output_schema` bỏ qua candidate không hỗ trợ.
- Privacy lấy giới hạn thấp nhất của profile/provider. Invalid classification
  được xử lý như PUBLIC. Test mới xác nhận provider không được gọi khi vượt quyền,
  thay vì để network lỗi làm test privacy xanh tình cờ.

Giới hạn privacy bộc lộ một lỗi cấu hình thật: task executor truyền INTERNAL nhưng
profile mới chỉ cho PUBLIC. Lượt procurement đầu dừng trước khi gọi model. Sau
đó cấu hình rõ INTERNAL cho chính tenant thử nghiệm và retry thành công; không
hạ nhãn source hay mở quyền cho mọi tenant.

## Workflow và vòng học có chứng cứ

| Kiểm chứng | Kết quả và bằng chứng |
| --- | --- |
| Procurement đầu | Dừng đúng ở privacy boundary, giữ [report thất bại](artifacts/model-readiness/2026-10-07/procurement.json) |
| Retry procurement | **13/13 bước**, 6 real model calls, 0 fake, [report](artifacts/model-readiness/2026-10-07/procurement-retry.json) |
| Audit độc lập | **75 checks PASS**, đọc task/execution/usage/event/audit và hash artifact; [receipt](artifacts/model-readiness/2026-10-07/procurement-audit.json) |
| Feedback | Model sửa human-gate owner theo validator, sau đó nhận câu trả lời bổ sung; **3 real model calls**, [report](artifacts/model-readiness/2026-10-07/feedback-configured.json). Campaign vẫn paused |
| Train | **2/2**, HR và procurement; tạo hoặc reuse 2 skill nháp có nguồn và corpus hash |
| Holdout lượt 1 | Baseline **4/4**, candidate **4/4** |
| Holdout lượt 2 | Candidate **4/4**, baseline **4/4**, đảo thứ tự arm để giảm thiên lệch availability |
| Skill read-back | **2 version chưa publish**, [receipt](artifacts/model-readiness/2026-10-07/skills-readback.json) |

[Paired report](artifacts/model-readiness/2026-10-07/learning-paired.json) giữ mọi
task ID, raw output, usage và grading. Gold không đi vào task input. Dataset
con HR/procurement giữ hash của [corpus gốc](examples/organization_eval/cases.json).
Candidate lessons chỉ được truyền trong sandbox, không bind vào production.
Hai arm đều đạt, nên chưa có bằng chứng candidate tốt hơn baseline. Chưa có
review chuyên gia mới cho corpus này, và không có huấn luyện trọng số model.

Mọi người, supplier, interview, delivery và gate trong simulation đều là tổng hợp.
Không gửi mail, PO, payment hoặc thực hiện onboarding thật trong các probe này.
Một resume feedback cũ thất bại vì report ở state failed không có câu hỏi đang
chờ; tạo probe mới sau sửa cấu hình, không sửa lịch sử thành công giả.

## Kiểm tra theo đường tích hợp

| Đường | Đã kiểm tra hoặc đối chiếu source | Khoảng trống cần xử lý |
| --- | --- | --- |
| UI → API → task → runtime → output | UI giai đoạn 7 đã đóng cổng theo receipt trước; task executor thật chạy procurement và evaluation mới | Chưa có source picker, upload và output download thống nhất cho mọi task |
| Tenant profiles → gateway | DB preview/apply, fresh-session read-back; sửa evaluation harness từng dựng lại profile và làm mất capability/deadline của tenant | Cần UI quản trị profile, capability/entitlement probe có cache theo tenant |
| Controller → stage → gate → revision | Procurement hoàn thành và audit độc lập; feedback sửa lỗi và pause | Chưa chạy MEP full với mail thật trong đợt này; không coi controller mọi workflow đều đã nghiệm thu |
| Model → tool → model | Benchmark calculator round trip; regression cho empty/error/required tool/403/401/refusal/deadline | Task prose tùy ý cần contract riêng để chấm semantic quality |
| Skill proposal → evaluation → publication | 2 draft, 2 lượt holdout, read-back unpublished; publication gate hiện yêu cầu evidence và human review | Chưa có căn cứ publish hay nâng autonomy |
| PostgreSQL → outbox → NATS → Temporal | Socket và `make preflight-e2e` trực tiếp; full suite/E2E là cổng kỹ thuật riêng | Socket lắng nghe không chứng nhận relay healthy; đối chiếu E2E receipt cuối |
| Tools/MCP/A2A | Giữ subprocess và socket tests có sẵn, không thay bằng mock | Chưa canary một connector doanh nghiệp mới trong đợt này |
| Gmail → attachment → extraction | Source có SMTP/IMAP, TXT/DOCX/PDF, hash và provenance; PDF text cần native pdftotext | Chưa upload UI, ZIP chung, OCR ảnh hoặc kho đa nguồn. Không gọi Gmail trong audit read-only |
| Documents → versions/distribution | GET register/summary và source router có version/distribution/acknowledgement | Đây là document control, không phải endpoint upload/parser |
| Retrieval | Tenant/unit predicates và hash embedding trong source | Hash embedding không chứng minh semantic search. Đo recall trên corpus nguồn trước khi thay provider |
| Tenant isolation | RLS/role readiness từ API; isolation tests dùng ao_app; privacy assertions mới không gọi provider khi bị cấm | Demo header tenant chưa có authenticated membership; cần đóng trước khi mở nhiều công ty |
| Tenant customization | Model profiles, agent definitions, skills, policies đã có version/binding | Business workflows còn fixed O-Nexus SOP/owners; chưa có template/provisioning/import/export hoàn chỉnh |

[GET/socket snapshot](artifacts/model-readiness/2026-10-07/runtime-before-restart.json)
có 11 checks PASS, gồm health, ready, models, agents, tools, skills, documents,
stream status và ba socket. Đây là kiểm tra đọc, không phải completion test.
Runtime được khởi động lại từ checkout này sau khi các probe đã kết thúc.
Receipt cuối và regression gate được ghi ở thư mục model-readiness.

## Chạy lại phép đo

Các command dùng tổ chức được chỉ rõ; không tự chọn tenant đầu tiên để ghi.

```bash
uv run python scripts/configure_model_chain.py --org ORG --output preview.json
uv run python scripts/configure_model_chain.py --org ORG --max-classification internal --apply --output applied.json
AO_MODEL_PROVIDER_DEFAULT=openrouter uv run python scripts/benchmark_free_models.py --output benchmark.json
AO_MODEL_PROVIDER_DEFAULT=openrouter uv run python -m scripts.run_readiness_learning --org ORG --dataset artifacts/model-readiness/2026-10-07/workflow-corpus.json --rounds 2 --output learning.json
uv run python scripts/audit_runtime_readiness.py --org ORG --output runtime.json
make lint
make typecheck
make test
make test-e2e
```

Chỉ dùng `--max-classification internal` khi tenant được phép gửi INTERNAL tới
provider đó. Cấu hình explicit này không cấp phép gửi tài liệu confidential.
Benchmark có thể exit 1 do model không đạt; report thất bại vẫn là kết quả cần giữ.

Nguồn kỹ thuật đã kiểm tra:
[OpenRouter models API](https://openrouter.ai/docs/api/api-reference/models/get-models),
[tool calling](https://openrouter.ai/docs/guides/features/tool-calling),
[structured outputs](https://openrouter.ai/docs/guides/features/structured-outputs).
Các quyết định upload/OCR và nguồn tham khảo nằm trong DATA_INTAKE_PLAN.md.
