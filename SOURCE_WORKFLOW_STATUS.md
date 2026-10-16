# Luồng nguồn → workflow → file kết quả

> Cập nhật 16/10/2026: OpenRouter còn PARTIAL: runtime thật đạt 3/7; 4 ca lỗi upstream 503.
> Baseline đã kiểm tra: 3.870 test, 35 E2E, lint/typecheck PASS; UI trên cổng 8100
> đã qua 91 kiểm tra điều hướng và 30 kiểm tra nút/phông chữ. Đây là bằng chứng
> của đợt trước, không thay cho campaign model thật: chưa đạt nghiệm thu.
> [Báo cáo công bố đã lọc dữ liệu](artifacts/product-completion/2026-10-16-openrouter/summary.json).
> Sửa 200-envelope: HTTP 200 mang error.code 503 được phân loại đúng (fallback được, giữ terminal 401/refusal, không bịa usage); 5 regression MockTransport; harness campaign giữ local, chưa nghiệm thu.
> Các đường dẫn bằng chứng cục bộ trong nhật ký bên dưới không được phát hành lên Git.


> Nhật ký đợt trước, 16-10-2026 (giờ Việt Nam): OpenCode đã chuyển sang
> session mới dùng `opencode/longcat-2.5-preview-free`; ngừng dispatch Nemotron.
> Giữ nguyên model chạy tác vụ sản phẩm. Codex kiểm chứng thêm identity/token/operator
> CLI và quota source+artifact: **51 PASS** (17,69 giây); đây là focused suite,
> chưa thay thế full gate sau các sửa UI. Hai worker Longcat thực sự chạy song song:
> recovery preview (C) và audit pilot60 (D); nhóm UI/browser đang được điều phối.
> Pilot60 đã có đủ60 attempt, artifact đạt32/48 model case (66,7%), không đạt
> ngưỡng90%; 44/60 expectation có gồm12 negative guard. Chưa có expert ký duyệt.
> Evidence đợt này đang giữ riêng tại `.orchestra/longcat-2026-10-16/`; chưa công bố
> receipt cuối, chưa coi thay đổi đang viết hoặc báo cáo agent là đã nghiệm thu.


Cập nhật 15-10-2026, checkout hiện hành `/home/vutun/ai_orchestrator`, nhánh `main`.
Các receipt source-intake bên dưới được nhập từ đợt worktree trước và đã có trên main.
Phạm vi: tài liệu do người
vận hành chọn rõ ràng hoặc file mock có nhãn. Không tự lấy dữ liệu tenant làm input.

## Trạng thái hiện hành 15-10-2026

Main tại `/home/vutun/ai_orchestrator`, baseline `a50f912`; diff chưa commit.
Đây là trạng thái sau triển khai và kiểm chứng cuối; các snapshot phía dưới là lịch sử.
OpenCode thực hiện UI, corpus, CLI, harness và tài liệu; riêng batch lớn có chín subagent.
Codex giữ tenant/idempotency, ledger, event correctness, review và gate.

| ID | Owner, tiêu chí và dependency | Kết quả thực tế | Evidence | Việc tiếp theo / giới hạn |
| --- | --- | --- | --- | --- |
| WTR-01 | Codex; kiểm kê 23e2 | SKIP: đã mất, không đăng ký Git | inventory (bằng chứng cục bộ: `artifacts/product-completion/2026-10-15-main/worktrees.txt`) | Không can thiệp process còn cwd đã xóa. |
| WTR-02 | Codex; bảo toàn rồi gỡ6737 | PASS: process đã kết thúc;64 file/hash giữ riêng; bundle verify; Git remove không force | Retirement receipt (bằng chứng cục bộ: `artifacts/product-completion/2026-10-15-main/worktree-retirement-final.json`) | Snapshot detached chỉ để archive3 config/instruction files; không tạo branch triển khai/commit main. |
| R0-CAT-01 | Codex; fresh DB + seed/register idempotent | PASS: hai DB scratch, migration0036, catalogue13 roles/28 SOP/6 policies | receipt (bằng chứng cục bộ: `artifacts/product-completion/2026-10-15-main/clean-install-final/receipt.json`) | Giữ native PostgreSQL55432; không dùng DB5433. |
| R0-FIX-01 | Codex; fixture/event/process spine | PASS:62 tests | log (bằng chứng cục bộ: `artifacts/product-completion/2026-10-15-main/r0-fixture-verification.txt`) | Không mở lại gap đã sửa trên main. |
| R0-IDEM-01 | Codex; source create concurrent/tenant/legacy replay | PASS:19 source integration; hai repro FAIL trước sửa | [source suite](artifacts/product-completion/2026-10-15-main/source-idempotency-fixed-full-source.txt) | Phạm vi source API; generic idempotency ở API khác chưa được công bố đã sửa. |
| R0-EVT-01 | Codex + OpenCode J; consumer timeout/cancellation + scoped relay | PASS:17 focused; final full gate PASS | [focused](artifacts/product-completion/2026-10-15-main/consumer-relay-fixed-rerun.txt) | Giữ default global relay, RLS tenant sessions và idle timeout semantics. |
| R0-HLD-02 | Codex; free-policy/preflight/ledger accounting | PASS:58 unit tests | [contract](artifacts/product-completion/2026-10-15-main/accounting-contract.md) | Chỉ ledger authoritative; execution là aggregate của cùng calls, không cộng hai lần. |
| SRC-REV-01 | Codex; terminal root revision, human/idempotency/rollback | PASS:19 source integration | [suite](artifacts/product-completion/2026-10-15-main/source-idempotency-fixed-full-source.txt) | Task/manifest mới, không chạy hoặc copy output/spend/approval; checkpoint revision vẫn MISSING. |
| SRC-REV-02 | OpenCode + Codex; sau SRC-REV-01 | PASS kỹ thuật:42 checks MỖI desktop/mobile, More thật, upload/reload/keyboard/retry/history | latest browser (bằng chứng cục bộ: `artifacts/product-completion/2026-10-15-main/final-closeout/review-browser-root-final/acceptance.json`); H độc lập trước đó (bằng chứng cục bộ: `artifacts/product-completion/2026-10-15-main/ui-independent-confirmed/receipt.md`) | Frontend giữ cùng source-intake; H completed-DOCX PASS khác G fresh-Run provider FAIL. |
| UX-R2-DELTA-01 | OpenCode; banner waiting-input | PASS:bug đã tái hiện/sửa;40 loads triển khai +20 loads độc lập | independent (bằng chứng cục bộ: `artifacts/product-completion/2026-10-15-main/final-closeout/broad-r2-independent/receipt.md`) | Banner hướng dẫn bổ sung input, không đổ cho thiếu owner. |
| HARNESS-R2-DELTA-01 | OpenCode triển khai; Codex sửa integrity sau review | PARTIAL nghiệm thu độc lập; Codex PASS28 unit +42/viewport; injected mid-fault FAIL đúng exit1 | fault proof (bằng chứng cục bộ: `artifacts/product-completion/2026-10-15-main/final-closeout/midprobe-failure-verification.json`) | Giữ receipt false-positive trước sửa. OpenCode kiểm độc lập bản cuối BLOCKED do quota. |
| R2-REVIEW-01 | OpenCode CLI; Codex critical ledger/round/path corrections | PARTIAL nghiệm thu độc lập; Codex PASS57 unit; packet8/8 + checklist0/60 có thật | review packet8 (bằng chứng cục bộ: `artifacts/product-completion/2026-10-15-main/final-closeout/review-packet-root-actual-8-relocated/packet.md`); checklist60 (bằng chứng cục bộ: `artifacts/product-completion/2026-10-15-main/final-closeout/review-packet-root-not-run-60/packet.csv`) | Expert UNREVIEWED; không collapse round hoặc zero-fill unknown. OpenCode independent sau sửa BLOCKED quota. |
| R2-CORP-01 | OpenCode L/M + Codex; tối thiểu30ca mỗi pilot | OFFLINE PASS60ca; live NOT_RUN; expert PENDING | verifier chạy lại được (bằng chứng cục bộ: `artifacts/product-completion/2026-10-15-main/pilot-corpus-independent/verify.py`) | 30 document_review +30 quote_comparison; không biến8ca live thành60ca đã chạy. |
| R0-HLD-01 | OpenCode I + Codex; corpus v2 real-model synthetic | FAIL:7/8 expectations gồm2 guards;5/6 model attempts có artifact=83.3% | actual run (bằng chứng cục bộ: `artifacts/product-completion/2026-10-15-main/model-corrected-full/run-receipt.json`); independent (bằng chứng cục bộ: `artifacts/product-completion/2026-10-15-main/model-independent-confirmed/receipt.md`) | qc-injection thiếu required submit_evidence sau5 submissions; dưới ngưỡng đề xuất90%; giữ cả failed first run/timeout240s. |
| API-VERIFY-01 | Codex; owned API8112 + actual retry boundary | PASS23 runtime/9pages; concurrent201 cùng ID, replay1, changed409,0exec/usage | latest runtime (bằng chứng cục bộ: `artifacts/product-completion/2026-10-15-main/final-closeout/runtime-final-root/runtime-check.json`); actual API (bằng chứng cục bộ: `artifacts/product-completion/2026-10-15-main/restarted-api-idempotency.json`) | Auth-disabled synthetic local; không suy ra production RBAC/deployment của API8100. |
| GATE-MAIN-01 | Codex; lint→typecheck→test→E2E | lint PASS; typecheck PASS; make test **3723 PASS**; E2E **35 PASS** | [final gate6](artifacts/product-completion/2026-10-15-main/gates-final-6/receipt.json) | Gate4 và mọi gate FAIL/interrupted là lịch sử riêng; không xóa. |
| DEV-OPENCODE-01 | Codex điều phối OpenCode Muse free xhigh | BLOCKED:FreeUsageLimitError429;4 subagent cuối +1 bounded retry không chạy được | quota events (bằng chứng cục bộ: `artifacts/product-completion/2026-10-15-main/final-closeout/opencode-quota-events.json`) | Không đổi model trả phí/fake hoặc retry vô hạn. Metadata36 sessions được export kiểm chứng; completion không suy ra từ metadata. |

Bằng chứng đều là dữ liệu tổng hợp có nhãn hoặc metadata kỹ thuật. Runtime giữ
NVIDIA Nemotron Ultra free → DOTS free; Muse chỉ dùng cho phát triển OpenCode.
Ledger quan sát báo $0; không coi đó là bảng giá chung hay SLA. Model runtime đã
yêu cầu tool call bắt buộc; lỗi thiếu tool vẫn bị loại, không dựng tool call từ text.

Gói review không chấm chuyên môn hoặc ký thay người. Năm bộ artifact đọc lại gồm
30 file; SHA khi receipt không có claimed hash được ghi **observed_no_claim**,
không phải xác minh hash lịch sử. Full rerun có `rerun_of` vẫn là8/8, mọi round
được giữ; subset giữ denominator và nguồn gốc riêng. Mọi expert field còn UNREVIEWED.

R0 reliability và expert acceptance vẫn mở. Identity/membership, checkpoint revision,
quota/retention/storage và connector là **MISSING/NOT_IMPLEMENTED** theo roadmap,
không được gộp thành blocker provider. R3 dữ liệu tenant thật/connector canary
phụ thuộc cổng R0/R2; chưa tuyên bố sản phẩm hoặc production hoàn thành.

Lịch sử lỗi gồm first holdout sai interpreter, timeout240s dù late-complete, G fresh-Run
provider-truncated, gate1/2/3/5, sáu CLI integrity repro và probe mid-fault false-PASS.
Chúng được giữ trong gói bằng chứng (bằng chứng cục bộ: `artifacts/product-completion/2026-10-15-main/README.md`); không dùng rerun để xóa verdict cũ.

## HISTORY — Tiếp tục trên main, 15-10-2026 (bản nháp superseded, giữ để đối chiếu)

HISTORY: bảng dưới là bản nháp cũ, đã được hợp nhất vào `## Gói hiện hành` ở trên.
Không dùng số cũ thay receipt mới. Sai sót đã sửa ở bản hiện hành: path `clean-install-verified`
là sai (đúng là `clean-install-final`); backend là 19 PASS (16 là subset lịch sử);
UI H 42 checks/viewport + DOCX completed PASS đã nghiệm thu (không còn "chưa nghiệm thu");
harness accounting/preflight 58 PASS + event focused 17 PASS đã có (không còn "chưa viết");
GATE-MAIN-01 hiện hành là gates-final-3 FAIL 1/3630 (không còn RUNNING/“chưa chạy đủ”).
Timestamps: 05:30 UTC (interrupt/lint FAIL) khác 07:32 UTC (gate3 FAIL 618.91s), giữ riêng.

| ID | Owner | Mục tiêu và dependency | Trạng thái HISTORY (superseded) | Bằng chứng HISTORY |
| --- | --- | --- | --- | --- |
| WTR-01 | Codex | Kiểm kê `23e2` | SKIP: đường dẫn đã mất, không đăng ký với Git | Hai process còn cwd đã xóa; không can thiệp phiên khác. |
| WTR-02 | Codex | Bảo toàn rồi gỡ `6737` | BLOCKED: process khác PID 199181 còn cwd trong worktree | Đã sao lưu 64 file, manifest SHA-256 tại `.orchestra/worktree-retirement/6737/manifest.json`. Không có commit riêng ngoài main; thay đổi chưa commit là cấu hình/hướng dẫn, đã giữ riêng. Chỉ gỡ khi phiên kia kết thúc. |
| R0-CAT-01 | Codex | Cài từ DB rỗng, catalogue/register đầy đủ và chạy lại không nhân đôi | PASS trong database tạm riêng | `scripts/verify_clean_install.py`; receipt `.orchestra/main-continuation/clean-install-verified/receipt.json`. Migrate cả hai DB mới đến 0036, copy reference từ scratch sang scratch, register hai lượt cùng số bản ghi. Không dùng catalogue dev. |
| R0-FIX-01 | Codex | Đối chiếu gap fixture với code main | PASS: 62 test task UI/event/process spine | Log `.orchestra/main-continuation/r0-fixture-verification.txt`. Bỏ nhánh skip giả định Event chưa được ghi; assertion đòi đúng frame của task HTTP. Các gap delegation/forbidden-zone cũ đã được sửa trong commit baseline. |
| R0-IDEM-01 | Codex | Tạo source workflow an toàn khi retry đồng thời và hai tenant dùng cùng key | PASS: hai repro FAIL trước sửa, toàn source suite 19 PASS sau sửa | `.orchestra/main-continuation/source-idempotency-reproduction.txt`, `source-idempotency-fixed-full-source.txt`. Namespace key theo tenant + advisory lock; replay key cũ vẫn giữ. Không thay schema hoặc công bố đã sửa mọi API/consumer. |
| SRC-REV-01 | Codex | Tạo revision nguồn từ task gốc đã kết thúc | Backend PASS: 19 integration tests, UI đang kiểm độc lập | API tạo task/manifest mới, giữ constraints/schema/budget/owner, không chạy, không copy output/spend/approval. Key theo tenant, serialize request đồng thời; nguồn lỗi rollback. Lịch sử duyệt vẫn thuộc task gốc. |
| SRC-REV-02 | OpenCode/subagent | UI chọn nguồn thay thế và lý do, dependency SRC-REV-01 | Triển khai một phần, chưa nghiệm thu browser | Bản đầu có code và probe. Còn sửa isolation trạng thái picker, upload/phân trang, copy trạng thái và lỗi harness trước khi nghiệm thu. |
| R0-HLD-01 | OpenCode/subagent | Corpus cố định và đo model/artifact qua API thật | Triển khai một phần, đang sửa sau review Codex | Harness phải lưu từng attempt, ledger/model/cost, hash/citation/file đọc lại, lỗi và denominator. Chưa có full holdout receipt. |
| R0-LIVE-01 | Codex | Đường nguồn/mock qua model thật trên tenant tổng hợp riêng | PASS 2/2 trong một lượt, R0 reliability vẫn mở | `.orchestra/main-continuation/live-baseline/acceptance.json` và `provenance.json`. Quote hoàn thành bằng DOTS fallback, hồ sơ bằng NVIDIA primary, ledger báo $0; sáu định dạng tải/mở/hash đều đạt. |
| GATE-MAIN-01 | Codex | lint → typecheck → toàn suite → E2E | Chưa chạy đủ trên diff cuối | PostgreSQL dev/test cùng 0036. Preflight mới PASS sau khi mở NATS/Temporal. Chờ worker ổn định file rồi chạy gate tuần tự. |

Session OpenCode mới `ses_ec2169b38ffeMRk4L621eQ86Ad` xác minh `pwd` và `main`
bằng shell. Hai subagent khảo sát đã xác minh qua export cùng model
`opencode/muse-spark-1.3-contributor-free`, variant `xhigh`; không tái dùng session
cũ ở 6737. Prompt, raw JSON, exit/time và exports giữ trong `.orchestra/main-continuation/`.
Codex đọc source/diff và chạy test trực tiếp; báo cáo worker không thay acceptance.

Revision hiện chỉ nhận root source workflow terminal. Task đang chạy/chờ input/chờ
approval bị chặn; revision checkpoint của workflow nhiều bước vẫn là backlog.
Artifact và quyết định cũ chỉ có giá trị với hồ sơ gốc, không áp dụng cho revision.
Nguồn thiếu, sai định dạng, unit không hợp lệ và privacy policy vẫn giữ các guard cũ.

Các số test/UI/live bên dưới là lịch sử trước batch main này, trừ khi có receipt mới
được nêu rõ. Chưa đóng R0 quality, R2 pilot có người xét, R3 identity/membership,
connector canary hoặc SaaS. Không tự gửi email, tạo PO, thanh toán hay publish skill.

## Kiểm chứng lại sau báo cáo agent ngoài, 15-10-2026

Sidebar cũ chỉ kiểm tra tọa độ dọc nên bỏ lọt chuyển động ngang. Trình duyệt
thật đo nút `x=175 → 15,5` và icon đầu `y=121,19 → 74` khi thu gọn.
Đã neo nút ở `left:16px`, giữ vị trí icon/chiều cao nhãn và thay ẩn/hiện
`display` bằng opacity/visibility. Vùng bấm workspace và hàng điều hướng theo
độ rộng thực tế; mobile dùng drawer, giữ nhãn và vùng bấm riêng.

- Gate chuyển động mới: **111/111**, lấy tọa độ nút và mọi icon từng frame trên
  desktop 1440×900, 1920×1080, 1024×640; hover, bàn phím, scroll, reduced motion;
  thêm drawer 390/360px, vùng bấm workspace và trả focus. **31 PNG**, không lỗi JS.
  [Receipt](artifacts/source-intake/2026-10-15/sidebar-followup/verified/receipt.json).
- Backend hiện hành: **115 passed**, gồm repair budget, sai tool/args/phép tính,
  fallback, source workflow và recovery. Lint, typecheck đã chạy lại.
  [Log](artifacts/source-intake/2026-10-15/sidebar-followup/backend-regression.txt).
- Shell gate **195/195**, gồm lưu trạng thái qua reload, accessibility và mobile.
  [Shell gate](artifacts/source-intake/2026-10-15/sidebar-followup/shell-acceptance/stage-3-gate.json).
  Harness xác lập trạng thái expanded bằng nút thật trước khi kiểm collapse/reload,
  thêm assertion precondition, giữ assertion persistence.
- Runtime/API và chín trang: **23/23**; work gate **131/131**.
  [Runtime](artifacts/source-intake/2026-10-15/sidebar-followup/runtime-final/runtime-check.json),
  [work gate](artifacts/source-intake/2026-10-15/sidebar-followup/work/stage-4-gate.json).
- Đã tìm và đọc log agent ngoài: **3560 passed**, **34 E2E passed**. Đây là
  lần chạy trước sửa sidebar, không phải full suite chạy mới trong vòng này.
  Bản log nằm trong [external-evidence](artifacts/source-intake/2026-10-15/sidebar-followup/external-evidence/).
- Probe live mới trên cùng chuỗi NVIDIA → DOTS: **1/2**, mock báo giá completed,
  tải và kiểm sáu định dạng; document sources bị DOTS content filter từ chối.
  Giữ nguyên failed task và receipt, không đổi sang fake hoặc vượt filter.
  Lần trước 2/2 là bằng chứng có thật, chưa phải cam kết ổn định.
  [Live mới](artifacts/source-intake/2026-10-15/sidebar-followup/live/acceptance.json),
  [executions](artifacts/source-intake/2026-10-15/sidebar-followup/live/executions.json).
- Backlog mới: **209 task phi-terminal**, 0 running: 207 assigned,
  1 waiting_for_input, 1 waiting_for_approval. Số 211 trong báo cáo trước là
  snapshot cũ. Không tự approve/cancel để làm sạch số liệu.
  [Snapshot](artifacts/source-intake/2026-10-15/sidebar-followup/backlog.json).

Script kiểm tra vị trí cũ đã thêm trục X; script mới chạy lại được bằng:

```sh
uv run python scripts/sidebar-motion-receipt.py \
  --base http://127.0.0.1:8110 --org <tenant-id> \
  --chromium <chrome-path> --out <receipt-directory>
```

Còn mở: độ sẵn sàng/quality của model free, corpus holdout và pilot có người
nghiệm thu, revision nguồn, identity/membership, quota artifact và connector thật.
Kết quả UI xanh không thay thế nghiệm thu các phần này.

## Hành vi đã triển khai

- Form giao việc yêu cầu chọn nguồn hoặc mock, không chọn sẵn. Kho nguồn phân
  trang, upload nhiều file, xem trích xuất, tải bản gốc, chọn file cho task.
- File mock CSV/TXT có thật đi qua cùng parser. Manifest chụp phiên bản nguồn,
  SHA-256 của byte gốc và trích xuất, phân loại, nguồn mock/upload và xác nhận đọc.
- Workflow `document_review` tạo báo cáo có trích dẫn; `quote_comparison` kiểm
  báo giá CSV cùng mặt hàng/số lượng, loại thiếu chứng chỉ và tính tổng VND bằng
  quy tắc xác định. Model phải khớp phép tính, không tự quyết định giao dịch.
- Task thiếu nguồn, file không đọc được hoặc vượt quyền owner chờ input mà không
  gọi model. Bổ sung nguồn hợp lệ đưa task về hàng chờ có thể chạy.
- Executor thật dùng model gateway, usage ledger và kiểm tra artifact. Mỗi nguồn
  phải có source ID, locator và đoạn trích nguyên văn. Không xuất file nếu thất bại.
- Kết quả được dựng thành DOCX, XLSX, PDF Unicode, JSON, Markdown và ZIP chứa
  các định dạng, manifest, bản gốc. XLSX vô hiệu công thức từ chuỗi văn bản đầu vào.
- Bảng nguồn, manifest, liên kết và artifact là append-only ở quyền `ao_app`;
  RLS bắt buộc tenant và phạm vi đơn vị. File OCR cần duyệt được lưu thành phiên
  bản mới, không sửa trích xuất cũ. Manifest chỉ gắn một lần vào task chưa chạy.
- Retry tạo task mới, sao chép yêu cầu/schema/budget và gắn lại phiên bản nguồn.
  Không sao chép kết quả, chi phí hay approval của lần trước.
- API tạo workflow hỗ trợ `Idempotency-Key`; upload gốc vẫn tạo phiên bản mới.

## Model hiện hành

Primary `nvidia/nemotron-3-ultra-550b-a55b:free`, fallback
`dots-studio/dots-3-note-preview:free`. Đã bỏ Ling/Inkling khỏi execution defaults.
Deadline mỗi candidate 60 giây; tối đa 5 vòng submit/sửa, một lần thử lại lỗi
provider và một lần thử lại lỗi gateway trong cùng budget. Không có retry vô hạn.
Profile deterministic chỉ dành cho cấu hình fake/test; không dùng để giả kết quả
live. Proposer phản biện giữ Gemma 4 31B free → Gemma 4 26B A4B free, tách khỏi chuỗi
execution. Catalogue [OpenRouter Gemma 26B](https://openrouter.ai/google/gemma-4-26b-a4b-it:free)
xác nhận hỗ trợ function calling; chưa benchmark proposer trong đợt nguồn này.

Benchmark ghim từng model nằm trong
[model-benchmark.json](artifacts/source-intake/2026-10-15/model-benchmark.json).
NVIDIA trả tool call dưới dạng văn bản lỗi trong hai ca, không được coi là gọi
công cụ thành công. DOTS vượt hai ca tool nhưng một ca lỗi usage; đây là rủi ro
provider thực tế, chưa có SLA. Chuỗi ưu tiên tuân theo yêu cầu, kiểm định chất
lượng độc lập và ghi model thực sự hoàn thành.

## Định dạng và giới hạn

| Nguồn | Cách xử lý | Giới hạn/chưa hỗ trợ |
|---|---|---|
| TXT, MD, CSV, JSON | UTF-8; locator dòng; CSV giữ bảng | 5.000 dòng CSV; JSON phải parse được |
| DOCX | XML an toàn, đoạn văn kể cả ô bảng | Chưa giữ bố cục phức tạp, header/footer |
| XLSX | Sheet/ô/dòng; không thực thi công thức | 5.000 dòng; công thức cần người kiểm tra |
| PPTX | Văn bản theo slide | Chưa hiểu chart, ảnh hoặc bố cục |
| PDF | `pdftotext`; OCR scan bằng Poppler/Tesseract | 50 trang text; 10 trang scan; scan cần duyệt |
| JPEG, PNG, TIFF | Kiểm ảnh; OCR eng+vie | 20 triệu pixel; một frame; độ tin cậy thấp cần duyệt |
| ZIP | Đọc member trong bộ nhớ, giữ locator member | 30 member, sâu 2, chống traversal/link/bomb |
| Định dạng khác | Lưu và báo unsupported | Không tuyên bố đọc được mọi file |

10 MiB/file, 20 MiB/gói nguồn, 1 GiB nguồn/tenant, 50.000 ký tự context chọn,
60.000 ký tự trích xuất/file. Parser chạy process riêng: timeout, CPU, RAM,
expanded size. Đây chưa phải sandbox mạng/antivirus hoàn chỉnh. PDF/OCR cần
`pdftotext`, `pdftoppm`, `tesseract` cùng language pack `eng`, `vie` trên host.
Dữ liệu confidential/restricted/secret bị model privacy policy chặn nếu không có
candidate phù hợp; không hạ classification để chạy free model.

## API chính

`POST /api/v1/source-files` multipart; `GET /source-files` kho phân trang;
`POST /source-files/mock`; `GET /source-files/{id}` xem trích xuất;
`GET /source-files/{id}/download`; `POST /source-files/{id}/confirm` ghi phiên bản duyệt.

`POST /api/v1/source-workflows` yêu cầu `title`, `goal`, `workflow`, `selection`
(`mode: sources|mock`, `file_ids`), có thể chọn `owner_agent_id`.
`POST /tasks/{id}/sources` bổ sung nguồn trước khi chạy;
`GET /tasks/{id}/sources` đọc manifest;
`POST /tasks/{id}/run` trả handle;
`GET /tasks/{id}/artifacts` và `GET /source-artifacts/{id}/download` tải kết quả.
Tất cả đường dẫn viết tắt trong đoạn này cùng prefix `/api/v1`.

## Kiểm chứng có thể chạy lại

```sh
make migrate
make migrate-test
make model-sync
make lint
make typecheck
make test
make test-e2e
make serve
uv run python scripts/probe_source_workflow.py \
  --org <tenant-id> --output artifacts/source-intake/local-live
```

Probe live tạo file tổng hợp, gọi model thật, kiểm hash file tải về, mở DOCX/XLSX
và ZIP, kiểm tổng tiền. Không gửi email, tạo PO hoặc thay đổi nghiệp vụ ngoài hệ
thống. Receipt giữ cả lần thất bại; không coi failed-only rerun là full acceptance.
Các bằng chứng của đợt này nằm tại
[artifacts/source-intake/2026-10-15](artifacts/source-intake/2026-10-15).

## Việc tiếp theo để mở dữ liệu và tenant thật

1. Identity/membership tenant, storage quota toàn bộ artifact, retention/GC và
   object storage; malware scanning và worker sandbox trước khi nhận file rộng rãi.
2. Luồng revision có UI đổi nguồn rõ ràng, hủy approval/artifact phụ thuộc;
   resumable upload, job extraction dài và trạng thái theo từng member ZIP.
3. Connector Gmail/Drive/plugin: OAuth scope, chọn account, retrieval theo quyền,
   attachment/version/hash/provenance; không thay connector bằng dữ liệu mock.
4. Workflow hợp đồng có version, stage input/output, công cụ được phép, checkpoint
   người duyệt, lịch chạy, retry/compensation và tiêu chí file theo từng nghiệp vụ.
5. Hai tenant pilot cấu hình policy/model/workflow/field độc lập; kiểm cấm đọc chéo
   đơn vị/tenant qua API, worker, export, retry và connector; chi phí/quota theo tenant.
6. Mở rộng workflow theo [PRODUCT_COMPLETION_PLAN.md](PRODUCT_COMPLETION_PLAN.md),
   mỗi pack chỉ công bố sẵn sàng sau tập dữ liệu đánh giá, file đầu ra được người
   dùng chấp nhận, lỗi/timeout/phân quyền và replay đều vượt cổng nghiệm thu.

## Bằng chứng hiện có

- API kiểm 12 fixture định dạng: tất cả có text, PDF scan cần review.
  [Receipt](artifacts/source-intake/2026-10-15/formats/acceptance.json).
- Trình duyệt kiểm upload, chọn mock và tải DOCX, không lỗi JS.
  [Receipt](artifacts/source-intake/2026-10-15/browser/acceptance.json).
- Cặp task model thật nguồn/mock cùng completed, đọc lại sáu file mỗi task.
  [Receipt](artifacts/source-intake/2026-10-15/live-schema-tuned/acceptance.json).
  Vòng xác nhận sau đó có lỗi provider, không được tính là pass toàn bộ.
- Ledger xác nhận model hoàn thành và cost $0 do provider báo cáo.
  [Ledger](artifacts/source-intake/2026-10-15/live-ledger.json).
- Hai test phục hồi pass; xử lý đúng 14 review lỗi thời, lần hai 0 thay đổi.
  [Apply](artifacts/source-intake/2026-10-15/stale-reviews-applied.json),
  [idempotence](artifacts/source-intake/2026-10-15/stale-reviews-idempotent.json).
- Runtime/readiness và chín trang Chromium: 23 checks pass.
  [Receipt](artifacts/source-intake/2026-10-15/runtime-ui/runtime-check.json).

Các file `test-final.txt`, `e2e-final.txt`, `lint.txt`, `typecheck.txt` là kết quả
cổng mới nhất; chỉ tính pass sau khi command đã kết thúc. Các log có `FAIL` được
giữ để giải thích quá trình sửa, không phải bằng chứng nghiệm thu đã xanh.

Luồng thực sự bắt đầu bằng nút Chạy và kết thúc bằng nút tải DOCX trên Chromium
đã pass, không lỗi JavaScript:
[browser-live](artifacts/source-intake/2026-10-15/browser-live/acceptance.json).
Có thể chạy lại bằng `uv run --with playwright python scripts/probe_source_browser.py
--org <tenant-id> --chromium <chrome-path> --output <artifact-directory>`.
Parser các định dạng có probe riêng `scripts/probe_source_formats.py`.

Retry qua HTTP sau sửa owner đã được chạy với model thật và completed, giữ nguyên
manifest nguồn và tạo đủ sáu artifact:
[live-retry](artifacts/source-intake/2026-10-15/live-retry.json).
Owner nguồn được giữ khi retry; owner vẫn phải qua kiểm tra active, đơn vị và policy
của executor trước khi gọi model. Quy tắc retry của task thông thường giữ nguyên.

## Cập nhật backend chạy mượt ngày 14-10-2026

- Prompt nguồn ép gọi tool đúng: `source_document.py` thêm "Invoke submit_evidence
  as a tool call; never write the artifact or the tool call itself as prose or
  fenced JSON" (đúng ca NVIDIA viết tool call dạng text), tool description thêm
  "Call exactly once per turn".
- Vòng sửa hỏng turn có gì để sửa: `workflow_evidence.py` đưa đoạn text thô
  (cắt 2.000 ký tự) vào feedback khi turn không gọi tool, thay vì
  `PREVIOUS SUBMISSION:` trống; hỏng turn bị loại candidate, sai ngữ nghĩa
  (winner/total) được thử lại 1 lần cùng candidate rồi mới loại; tối đa 5 vòng,
  `max_provider_retries=0`, vượt budget thì dừng và ghi ledger.
- Regression fixture (không mạng, không tốn tiền):
  `tests/unit/test_source_smooth_run.py`, 7 tests cho 5 ca (text-form, sai tool,
  args hỏng, winner sai, bound 5 vòng, hết budget, fallback gateway NVIDIA→DOTS).
- Cổng: `make lint`, `make typecheck` xanh; `test_source_workflow.py` (11) +
  `test_source_recovery.py` (2) + `test_pydanticai_bridge.py` +
  `test_model_completion_fallback.py` xanh; `test_business_workflow.py` +
  `test_source_intake.py` không regression.
  Receipt: [backend-smooth-run](artifacts/source-intake/2026-10-15/backend-smooth-run.json).
  Không probe live (fixture đã phủ hết ca, giữ benchmark live 14-10 làm bằng chứng
  read-only). Không đổi contract API/DB, không sửa migration, không đụng
  `web/`/`verify_page.mjs`, không thêm model trả phí.

## Cập nhật kiểm tra lại ngày 14-10-2026

- Sidebar: thu gọn còn rail 64px bằng nút, persist `localStorage` qua reload; hover
  (chỉ thiết bị `hover:hover` + `pointer:fine`) hiện tạm rộng 216px, out chuột thu
  lại, không đổi trạng thái đã lưu; drawer mobile không ảnh hưởng. Kiểm chứng
  Chromium thật, không lỗi JS:
  [sidebar-hover](artifacts/source-intake/2026-10-15/runtime-ui/sidebar-hover.json).
  Quy tắc rộng của peek nằm ở layer utilities vì layer thắng specificity.
- Retry task nguồn giữ owner nên hàng mới ở `assigned` (có chủ, chưa chạy) thay vì
  `created`; cổng trang đã sửa để chấp nhận cả hai trạng thái chưa chạy, kèm test
  `test_retry_keeps_source_owner_and_can_run_from_http` khóa hành vi (giữ owner,
  không execution nào trước Run, chạy đến completed). Cổng `make page` đạt exit 0.
- Generator ORM `rebuild_model_table_args.py` từng phát literal nháy đơn khiến
  `ruff format` viết lại sau mỗi `sync_model.sh`; đã chuyển sang literal nháy kép
  ổn định và cho pipeline tự format cuối, `--verify` vẫn chứng minh idempotent,
  `make lint` đạt 451 files.
- Trang console hết 404 `favicon.ico` nhờ `<link rel="icon" href="data:,">`; đây là
  lỗi console duy nhất còn lại trên Chromium và đã được xóa.

## Cập nhật nút sidebar ngày 14-10-2026

- Nút thu gọn/mở rộng (`#sidebarToggle`) chuyển từ đầu sidebar xuống đáy
  (neo `position:absolute; bottom` trong `#sidebar` vốn đã `position:sticky`;
  không đổi DOM trong `index.html`). `.foot` chừa 48px đáy để nút không đè chữ
  kết nối; khi thu gọn `.foot` ẩn và nút căn giữa ngang nhưng cùng tọa độ dọc.
- Tọa độ dọc không đổi ở cả ba trạng thái (đo Chromium thật 1440px: y=860 mở,
  thu và hover-peek); persist `localStorage`, hover-peek, aria-expanded/nhãn
  VI-EN, tooltip khi thu, drawer mobile (`display:none`), reduced-motion giữ
  nguyên. `paintSidebar`/`indicator` không cần sửa (logic không đổi, chỉ CSS).
- `verify_page.mjs` thêm 4 check CSS (neo đáy, giữ neo khi thu/peek, drawer vẫn
  ẩn nút); mọi check sidebar cũ vẫn xanh, `make page` exit 0.
  Receipt: [sidebar-toggle-bottom](artifacts/source-intake/2026-10-15/runtime-ui/sidebar-toggle-bottom.json)
  + 3 ảnh mở/thu/peek; chạy lại bằng
  `uv run python scripts/sidebar-toggle-receipt.py` (API ở cổng 8107).

## Tinh chỉnh console (sang trọng + đặc hơn + gom cụm) ngày 15-10-2026

- Chỉ chạm frontend: `tokens.css` (thêm `--shadow-lg/pop/toast`, `--ring-focus`,
  `--accent-gradient`, `--glass-blur`; comfortable gọn lại
  `--row-min-height 48px`, `--row-padding 12px 16px`, `--panel-padding 16px 20px`;
  mọi giá trị màu giữ nguyên), `components.css` (hover/focus/active, glass toast
  + `.ui-callout.ui-glass`, skeleton shimmer, drawer/menu/popover motion, peek
  nổi bóng), `console.css`/`management.css`/`work-ui.css` (siết padding/gap list,
  register, detail), `components.js` (callout info/neutral không action gắn
  `ui-glass`), `index.html` (bỏ 1 inline `margin-top` để overview xếp lưới CSS).
  Tuyệt đối không sửa rule `.sidebar-toggle`/đáy sidebar (`shell.css`,
  `shell.js` của agent khác giữ nguyên).
- Task detail: heading (tiêu đề + Hủy/Chạy lại/Chạy ngay) `position:sticky` dưới
  thanh crumbs kèm blur, overview desktop ≥1100px xếp 2 cột (đáp án + yêu cầu
  gốc cạnh nhau), panel có `scroll-margin-top`. Register giữ action inline
  (owner + chờ duyệt) nay gọn hơn, đọc được nhiều hàng hơn trong 1 màn 1440px.
- Xanh: `make lint` (454 files), `node --check components.js`, `make page`
  exit 0, `ui-foundation-gate` 170/170 (contrast 4.5:1 đủ 6 palette × sáng/tối ×
  2 density, coarse 44px, reduced-motion), `ui-components-gate` 148/148
  (toast-axe, button-density 40/32), issues-gate (bản copy đổi base 8099, repo
  không sửa) 90 checks 0 failed, interactions-gate (bản copy) passed 71 checks
  (dark-toast-action axe với toast kính, intro ≤400ms, reduced-motion).
  Receipt riêng 21/21: `uv run python scripts/ui-refined-receipt.py --base
  http://127.0.0.1:8099 --org org_01m3ycsehgwz7v1fk2ahswkwhm --chromium
  /home/vutun/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome --out
  artifacts/source-intake/2026-10-15/runtime-ui` (0 lỗi JS, ảnh desktop 1440 +
  mobile 390 các màn give/issues/approvals/task detail/settings-appearance +
  giữa trang; đối chiếu runtime-*.png 23:09 trước đổi).
  Receipt: [refined-console-receipt](artifacts/source-intake/2026-10-15/runtime-ui/refined-console-receipt.json).
- Hai gate chặn sẵn, đã chứng minh KHÔNG do đợt này (không sửa gate repo):
  `ui-shell-gate` kẹt ở `collapsed-keyboard-tooltip` vì `focusin → sidebar-peek`
  (core.js của agent sidebar-toggle) gỡ class `ui-tooltip-host` ngay khi focus;
  tái hiện y hệt khi stash toàn bộ CSS của đợt này. `ui-work-gate` crash ở
  `posts[0]` (rồi strict-mode 31 `.ui-panel-body` trong `#newTaskForm`) vì
  `SourceUI.create` đòi chọn nguồn trước và POST `/source-workflows` thay vì
  POST `/tasks` như fixture gate kỳ vọng — drift của tính năng source-intake.
  Đề xuất: chủ sở hữu peek quyết định tooltip khi focus (giữ peek nhưng vẫn hiện
  tooltip, hoặc sửa gate), và chủ source-intake cập nhật `ui-work-gate` theo
  luồng tạo task mới (mock `/source-workflows`, selector form body cụ thể hơn).

## Cập nhật vòng repair dùng hết budget 5 turns ngày 15-10-2026

- Nguyên nhân ca mock quote_comparison FAILED trong `/tmp/opencode/serve-8104.log`:
  turn 1 NVIDIA `stop` không gọi tool bị loại ngay (nhánh `not output`), turn 2
  DOTS `length`, turn 3 DOTS đủ 5 fields nhưng summary yếu bị validator từ chối
  (failures=2 → loại), turn 4-5 cả 2 đã excluded → gateway ném ModelUnavailable
  (không phải GatewayUnavailable-transient nên không được catch) → task failed.
  Vòng `range(5)` thực tế chỉ dùng được 3 turns. Ca sources cùng model PASSED
  nên đây là flakiness + luật loại quá gắt.
- Luật loại mới (`workflow_evidence.py::_execute_single`, ghi trong docstring):
  lần lỗi đầu (không tool call / JSON hỏng / shape sai / truncated / validator
  từ chối) KHÔNG loại, repair cùng candidate với rejection + previous submission;
  chỉ loại ở lỗi thứ 2 của cùng candidate, hoặc ngay khi
  `finish_reason=error` (sau allowance transient). Truncated lần đầu vẫn tăng
  gấp đôi output limit thay vì loại.
- Hết candidate mà còn turn: mở lại candidate ít lỗi nhất (hòa → lỗi trước,
  thường là primary) MỖI candidate 1 lần, retry trong cùng turn (không tốn
  submission); hết lượt mở lại thì fail fast bằng ValueError nêu tên candidate,
  số lỗi và rejection cuối thay vì ModelUnavailable khó hiểu. Lý do chọn mở lại
  thay vì fail fast ngay: probe cho thấy model flaky theo turn, feedback repair
  đã có sẵn, bỏ 2 turns là phí budget.
- Prompt `source_document.py`: summary phải ≥2 câu (60+ ký tự) nêu kết luận; với
  quote_comparison nêu winner + tổng VND + lý do loại. Ngưỡng validator (20 ký
  tự) và schema giữ nguyên; không spam token (chỉ thêm ~1 câu).
- Test `tests/unit/test_source_smooth_run.py` 11 ca: sửa 1 test cũ theo luật mới
  (lần hỏng đầu repair cùng candidate, không exclude), thêm live-chain
  no-tool→length→summary-yếu completed turn 4 (winner A / 1520000), test mở lại
  least-failed completed trong budget, test mở lại đã cạn fail fast nêu tên lỗi.
- Xanh: `make lint` (454 files), `make typecheck` (186 files), 102 unit
  (smooth_run + business_workflow + source_intake + pydanticai_bridge +
  completion_fallback + entitlement_fallback) + 13 integration
  (source_workflow + source_recovery). Không probe live (fixture tái hiện đúng
  chuỗi với cost $0). Không đổi contract API/DB, migration, `web/`, model trả phí.
  Receipt: [backend-repair-budget](artifacts/source-intake/2026-10-15/backend-repair-budget.json).

## 15-10-2026 — Gỡ 2 UI gate kẹt (chỉ sửa harness, không đổi product)

- `ui-shell-gate`: bỏ check `collapsed-keyboard-tooltip` (sai ý đồ — focus mở
  peek hiện nhãn đầy đủ nên tooltip là thừa), thay bằng 4 check
  `collapsed-keyboard-peek-opens` / `no-redundant-tooltip` /
  `peek-retracts-on-blur` / `rail-tooltip-restored` (tooltip chuột/rail giữ
  nguyên, kiểm chứng host khôi phục sau blur). Không chạm `core.js`/`shell.js`/CSS.
- `ui-work-gate`: mock `POST /source-workflows` (503, bắt payload) thay vì
  `/tasks`; gate chọn `#sourceChooseFiles` trước Run; selector form
  `#newTaskForm .ui-panel-body` → `#newTaskForm > .ui-panel-body`;
  `create-payload-preserved` assert goal + `selection.mode == sources` +
  `workflow == document_review`. Không hạ chuẩn, không đổi luồng sản phẩm.
- Cả 2 gate bọc `try/except`: crash vẫn ghi report JSON
  (`no-unexpected-crash` + `errors`), đã kiểm chứng bằng base chết.
- Xanh: shell-gate 194/194, work-gate 131/131 (base 8101, server tạm đã dừng),
  `make page` (PAGE_PORT=8101) exit 0, `make lint` exit 0.
  Receipt: [ui-gate-fix-2026-10-15](artifacts/source-intake/2026-10-15/runtime-ui/ui-gate-fix-2026-10-15.json),
  report + PNG: `runtime-ui/shell-gate/` (35), `runtime-ui/work-gate/` (44).
