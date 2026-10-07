# FUTURE_WORK — kế hoạch O-Nexus

## UI refresh theo brief — 2026-10-07

**Giai đoạn 0 đã hoàn tất**, trên nhánh `ui/refresh`: 57 URL, 338 ảnh,
24 trường hợp màu/mật độ, sáu font được kiểm tra. Receipt
[stage-0-gate.json](artifacts/ui-shots/00-baseline/stage-0-gate.json) đã PASS. Theo yêu cầu
mới, ưu tiên đi đúng thứ tự brief. Giai đoạn 1 đã triển khai nền tảng, browser
gate PASS và kiểm chứng cuối đã qua; chưa thay nghiệp vụ.
Nguồn đặc tả giữ nguyên tại [BRIEF.md](docs/ui-refresh/BRIEF.md).

- Khảo sát: [00-audit.md](docs/ui-refresh/00-audit.md).
- Quyết định và cổng kiểm chứng: [PROGRESS.md](docs/ui-refresh/PROGRESS.md).
- Thiết kế và thứ tự tiếp theo: [01-design-plan.md](docs/ui-refresh/01-design-plan.md).
- Ảnh hiện trạng: [index.html](artifacts/ui-shots/00-baseline/index.html).

**Mốc đã hoàn tất:** Giai đoạn 1 — layers, tokens cho 12 tổ
hợp, Inter/JetBrains Mono và Lucide cục bộ, density/motion/prepaint. Browser gate
đã đạt 170 checks; [receipt](artifacts/ui-shots/01-foundation/stage-1-gate.json).
Lint/typecheck đạt; full suite 3.477 passed, 8 skipped (5 cần NATS, 3 skip cũ),
1 deselected. Giữ bằng chứng và giới hạn thực thi tại PROGRESS.md.

**Mốc đã hoàn tất:** Giai đoạn 2 — component API và style guide ẩn local-only.
148 checks browser, 24 tổ hợp, 34 ảnh, axe sạch trên style guide/overlays;
full suite 3.477 passed/8 skipped/1 deselected. Receipt:
[stage-2-gate.json](artifacts/ui-shots/02-components/stage-2-gate.json).
Hướng dẫn: [03-components.md](docs/ui-refresh/03-components.md).

**Mốc đã hoàn tất:** Giai đoạn 3 — khung ứng dụng, browser
PASS 191 checks/24 tổ hợp/35 ảnh; lint/typecheck đạt, full suite một lượt
3.477 passed/8 skipped/1 deselected. Sidebar/rail,
indicator/tooltip, topbar VI/EN, mode/sáu màu/density, workspace thật, callout
xác thực và mobile drawer. Handoff: [04-shell.md](docs/ui-refresh/04-shell.md).

**Mốc đã hoàn tất:** Giai đoạn 4 — Công việc/detail/form. Browser PASS
131 checks/24 tổ hợp/44 ảnh, supplement PASS 14 checks. Register keyed,
100 hàng ban đầu/tải thêm, search/filter/sort toàn corpus, copy lỗi/ID,
validation/loading, nhật ký có phạm vi trang và controller cũ. Handoff:
[05-work.md](docs/ui-refresh/05-work.md). Lint/typecheck đạt; full suite một lượt
**3.477 passed/8 skipped/1 deselected** (547,38s). Không nghiệm thu model/mail/workflow thật bằng fixture UI.

**Mốc kế tiếp:** Giai đoạn 5 — chuyển từng trang, một trang một commit.

1. Sự cố: hàng lỗi mở rộng/copy, filter/count, loading/empty/error, owner và
   hướng xử lý; giữ route/action hiện có và dùng lại UI components.
2. Phê duyệt: giải thích yêu cầu, quyết định/authority, confirmation có context,
   trạng thái gửi/lỗi và thông báo đang có. Không giả phê duyệt, đổi quyền hoặc
   tạo cơ chế notification backend trong phạm vi refresh.
3. Tổ chức/đơn vị: hierarchy, hồ sơ agent, form và trạng thái; sửa deep link
   `#/dept/<agent_id>` thành key phòng ban thật sau khi xác minh mapping.
4. Quy trình cùng màn campaign/workflow/blueprint/intake: giữ thứ tự stage,
   human gates và mode simulation/live; input/output/log dùng cùng component.
5. Thư viện, Dữ liệu nghiệp vụ, Vận hành, Cài đặt: từng trang và màn con, giữ
   thao tác thật; tiếng Việt/EN, error/loading/empty, mobile và keyboard.
6. Mỗi commit có ảnh trước/sau và gate đúng scope. GĐ6 làm command palette,
   shortcut help, responsive/a11y/motion toàn app; GĐ7 tự phản biện, CSS cleanup,
   ma trận nghiệm thu và FINAL-REPORT. Chưa coi GĐ0–4 là toàn app đã hoàn tất.

Các mục nghiệp vụ bên dưới là roadmap riêng; không mở rộng phạm vi UI refresh.



## Trạng thái sau đợt 4 — 2026-10-06

Đợt này tiếp nối commit `b5c9e82`. Các phần lịch sử bên dưới giữ nguyên phạm vi
nghiệm thu tại thời điểm viết. Ưu tiên vẫn là tuyển MEP đến onboarding, procurement,
rồi các phòng ban khác. Không mở Bãi Tràm, tối ưu tenant hoặc đổi framework.

| Hạng mục kế tiếp | Đã triển khai | Bằng chứng và điều kiện còn lại |
| --- | --- | --- |
| Editor revision tuyển dụng | Form brief có kiểu dữ liệu, hash nguồn, diff và danh sách stage chịu ảnh hưởng. Người có quyền khác người đề xuất duyệt đúng snapshot. Tạo campaign mới cùng mode, chưa tự chạy; không kế thừa chứng cứ phỏng vấn, acceptance/onboarding. | Integration kiểm tra API, maker-checker, nguồn bất biến, hash cũ, áp dụng lặp và gate mới. Revision hiện hỗ trợ tuyển dụng; sửa BOQ của campaign procurement đã chạy là mốc riêng. |
| Tách chu kỳ HR | Catalogue tuyển dụng/onboarding, performance, payroll và offboarding; blueprint chỉ nhận đúng SOP/step của chu kỳ đã chọn. Giữ snapshot trigger/nguồn và cổng người duyệt. | Kiểm tra cả bốn chu kỳ, không sửa plan 26 bước đã duyệt trước đây. Nội dung bản nháp mới vẫn cần chuyên gia kiểm tra và Boss duyệt. Chưa có chứng cứ vận hành payroll/offboarding thật. |
| Đánh giá bài học feedback | Runner baseline/candidate hai lượt đổi thứ tự, cùng corpus train/holdout, task độc lập, hash bài học/output và receipt audit lưu PostgreSQL. API chỉ bind version đã xuất bản. | Publication của feedback kiểm tra ledger, model usage thật từng task và nguồn xác nhận chuyên gia; checkbox PASS không thay thế bằng chứng. Development-only luôn bị từ chối xuất bản. |
| Procurement live intake | Form BOQ, tối thiểu ba NCC, báo giá từng dòng, nguồn pháp lý/tài chính/HSE/chứng chỉ và người duyệt chất lượng. Server kiểm tra ID, số lượng, tiền VND nguyên và ngày giao. | Tạo campaign live chưa chạy, chưa gửi RFQ/PO/thanh toán. Nguồn là khai báo của người nhập, chưa phải tài liệu đã được connector tải và xác minh. Phải bổ sung nguồn thật và duyệt chất lượng thật trước khi nghiệm thu. |
| Giao diện và màu | Sáu bảng màu Navy, Teal, Indigo, Forest, Copper, Graphite; sáng/tối/theo hệ thống, xem trước và lưu theo trình duyệt. Campaign có vùng sản phẩm/log và vùng điều khiển, mobile đưa điều khiển lên trước. | Chromium kiểm tra 12 tổ hợp màu, tương phản chữ tối thiểu 4,5:1 ở các cặp token được kiểm tra, lưu/reload, trang desktop/mobile và form gửi đúng payload. Đây chưa phải kiểm toán accessibility toàn diện. |
| Bộ hồ sơ nghiệm thu tổng hợp | Theo xác nhận mới của người dùng: tự nghiên cứu mẫu công khai và tạo hồ sơ hư cấu. Packet có manifest/hash, CV mạnh/yếu/injection, phỏng vấn/acceptance, BOQ/ba NCC/chứng chỉ/GRN/invoice mô phỏng; loader kiểm tra nguồn trước tạo task. | Nguồn Acas và World Bank chỉ gợi ý cấu trúc; SOP O-Nexus điều khiển quy trình. Runner tạo lượt simulation mới, auditor đối chiếu brief với cả packet và kiểm tra ledger. Xem [hồ sơ và lệnh chạy lại](examples/workflow_acceptance/README.md); kết quả thực chạy ở WORK_REPORT.md. |
| Đo tải | Công cụ chỉ đọc 24 mẫu trên ba campaign, đồng thời bốn request; API p95 289 ms, pool checkout riêng p95 79 ms, không lỗi trong phép đo. | Pool riêng gồm pre-ping/tạo connection, không phải queue wait nội bộ API. Chưa nghiệm thu throughput model/campaign đồng thời hoặc SLA production. |

**Lượt model thật:** hai lượt × ba ca HR × hai nhánh = 12 task trên corpus tổng
hợp v1. Candidate đạt 6/6, baseline 4/6 với hai HTTP 400 từ provider. Có ba phản
hồi `finish_reason=error`, được giữ lại rồi fallback qua model free; không lấy
phản hồi lỗi làm artifact. Tổng 13 phản hồi có usage, USD 0 được ghi nhận. Báo cáo
cũ cộng reasoning hai lần thành 46.996; tổng input + completion thực tế là 32.943
token. Adapter đã sửa cho lượt mới, ledger lịch sử giữ nguyên; không so tổng
token khác quy ước như cùng một phép đo. Candidate thử nghiệm
`sklv_01m49pe2wk5ap17d2shmq63mmf` mang cờ
development-only, chưa xuất bản/bind. Kết quả này không chứng minh bài học tốt
hơn về nội dung: corpus nhỏ, provider không ổn định và chưa có chuyên gia duyệt.
Hai baseline thất bại còn khiến ledger hiện tại không đủ điều kiện publication.
Giữ nguyên báo cáo lỗi; không đổi gold hay lặp đến khi xanh để thay kết quả cũ.

**Phạm vi nghiệm thu được người dùng xác nhận:** tự tìm form/tài liệu công khai,
tự tạo dữ liệu và nghiệm thu. Đây là nghiệm thu kỹ thuật/nghiệp vụ trên hồ sơ
hư cấu có model/mail thật, các quyết định và hoạt động ngoài đời được ghi rõ mô
phỏng. Nghiệm thu mock đã đạt: MEP phục hồi 21/21 và 112 kiểm tra, procurement 13/13
và 76 kiểm tra. Lượt MEP đầu thất bại do content filter được giữ nguyên; một lượt
phục hồi dùng controller và nguồn không đổi, chấm lại khi nguồn intake thay đổi.
Xem [kết quả đầy đủ](examples/workflow_acceptance/RESULTS.md), gồm biến thiên điểm
và đề xuất điều kiện thử việc chưa có nguồn policy. Nghiệm thu production với
người/NCC thật vẫn là mốc riêng bên dưới.

### Thứ tự tiếp theo và điều kiện chốt

1. **Chốt chuẩn nội dung và xử lý provider refusal.** Phân biệt lỗi upstream,
   content-filter của provider và quyết định policy của nền tảng trong log/metrics.
   Provider refusal hiện dừng task, không tự đổi model để vượt refusal. Chọn quy
   tắc người vận hành xét lại hoặc tiếp nhận nguồn thay thế trước production;
   không biến lỗi provider thành đánh giá sai ứng viên. Bộ mock có kiểm tra nguồn
   công khai/hư cấu đã được tạo theo yêu cầu, nhưng chưa thay corpus chuyên gia.
   **Xác nhận nguồn và chuẩn nội dung HR/procurement:** Người phụ trách ký phiên
   bản corpus có hồ sơ khó, phản ví dụ và căn cứ SOP độc lập. Chạy lại nhiều lượt
   baseline/candidate, ghi cả lỗi provider, token, chi phí và p50/p95. Chỉ xem xét
   xuất bản candidate đã được người vận hành xác nhận, đủ ledger và chuyên gia
   duyệt; thử rollback trước khi canary. Nguồn xác nhận phải được người duyệt kiểm
   tra, API hiện lưu attestation chứ không tự xác minh chữ ký tài liệu ngoài.
2. **Nghiệm thu MEP live đến onboarding.** Cần CV thật, hai vòng phỏng vấn,
   quyết định HR/Boss, offer/acceptance và chứng cứ onboarding thực tế. Mọi bước
   có nguồn, artifact, execution và quyết định đúng hash; thiếu nguồn phải chờ.
   Các mốc 30/60/90 chỉ được hoàn thành khi tới kỳ và có bằng chứng, không ghi
   log giả để chốt. Phân biệt hoàn thành onboarding ban đầu với review tương lai.
3. **Nghiệm thu procurement live.** BOQ, ba báo giá, hồ sơ NCC, chứng chỉ và
   quyết định người vật tư phải có thật. Chạy controller và audit từng vật tư,
   chặn nguồn thiếu/không phù hợp/lệch PO–GRN–Invoice. PO, nhận hàng và thanh toán
   thực tế cần adapter có scope, intent, idempotency, receipt/read-back và xử lý
   timeout chưa rõ kết quả; chưa được mở quyền từ form tạo campaign.
4. **Đo và refactor theo nghẽn thực tế.** Instrument queue wait trong pool API,
   chạy nhiều campaign model đồng thời có giới hạn và đo độ trễ thông báo trên
   browser. Tách context builder/provider evidence/settlement khỏi executor khi
   có contract test; chuyển state console theo từng campaign, giữ một chủ sở
   hữu polling và draft form. Đo token/context trước khi cắt bớt nguồn; review
   contract cache accounting và migration usage lịch sử trước khi mở cached
   provider rộng. Luồng live hiện có 20 bước; simulation có thêm self-mail test
   thành 21, không yêu cầu chạy self-mail giả trong nghiệp vụ thật.
5. **Mở Design → QA/QC-HSE → Finance → Sales.** Mỗi phòng có controller/nguồn,
   corpus chuyên gia, negative cases, gates và audit. Shadow thực tế đủ 28 ngày
   theo tiêu chí đã ký trước khi tăng quyền. Mock không được tính vào shadow.

**Điều kiện chốt bản kỹ thuật:** lint/format, mypy, unit/integration, E2E và console
trên API thật đều đạt; diff không có secret, các cổng người duyệt giữ hiệu lực.
Kết quả cuối và lệnh chạy lại ở [WORK_REPORT.md](WORK_REPORT.md). Chốt bản kỹ thuật
không thay thế nghiệm thu nghiệp vụ thật tại các mục 1–3.

## Điều khiển, feedback và campaign — 2026-10-07

Đợt mới triển khai receipt SMTP self-test và dispatch native bền vững. Lệnh
Run/duyệt/bổ sung chứng cứ có generation lưu trong PostgreSQL trước khi trả lời.
Startup nhận lại lệnh chưa xử lý; khóa database chọn một process owner. SMTP lưu
intent và Message-ID trước write. Timeout/kill không được kết luận là chưa gửi;
read-only read-back phải tìm đúng Message-ID và hash CV để đối chiếu thành công.

Boss có thể tạm dừng campaign, nhập feedback, nhận câu hỏi/plan từ model, gửi câu
trả lời để model xem lại rồi xác nhận. Tiếp tục áp dụng vào phần chưa xong; sửa
sản phẩm đã xong dùng revision mới và cổng duyệt mới. Bài học được lưu thành
SkillVersion candidate chưa xuất bản, có task nguồn để đánh giá sau.

Console dẫn brief → campaign → việc cần duyệt → onboarding. Hồ sơ MEP dùng field
nguồn, kết quả phỏng vấn và nội dung bàn giao thay cho JSON tự viết. Task mail và
onboarding mới thuộc HR. Cài đặt thiết bị có ngôn ngữ, theme, độ gọn, giảm chuyển
động và màn hình bắt đầu; không thay quyền nghiệp vụ. Chi tiết vận hành nằm trong
[WORKFLOW_CONTROL.md](WORKFLOW_CONTROL.md); kết quả cuối ghi tại WORK_REPORT.md.

Bổ sung khi nhận bàn giao: shortlist rỗng hoặc không ai đạt cả hai vòng phỏng
vấn đưa campaign sang chờ, không gọi model chọn người lặp lại. UI hiển thị ngưỡng,
điểm và hướng xử lý bằng feedback/revision với nguồn mới. Điểm và quyết định cũ
không đổi. Form feedback/phỏng vấn và hash offer đã được kiểm tra theo nhánh UI.

Sau bản này, thứ tự còn lại là:

1. Bổ sung editor thay đổi brief có cấu trúc và diff trước khi duyệt revision;
   mapping sang đúng stage, nguồn cần nhận và mode phải do người vận hành xác nhận.
   Feedback hiện là context cho phần chưa xong, không tự thay schema hay quyền.
   Đo câu hỏi lặp, thứ tự plan, chất lượng văn bản và số retry trên tập tình huống
   có đáp án chuẩn; model free vẫn có thể chậm hoặc trả sai dù format hợp lệ.
2. Tách blueprint HR thành tuyển dụng, performance, payroll và offboarding; không
   chuyển ngầm plan cũ đã được duyệt. MEP hiện có workflow tuyển dụng riêng.
3. Nối skill candidate từ feedback vào corpus baseline/holdout thật và publication
   gate. Chỉ bind version đã qua đánh giá vào agent.
4. Nghiệm thu campaign MEP thật với CV, phỏng vấn, quyết định HR/Boss, acceptance
   và onboarding thật. Cần nguồn người vận hành; 30/60/90 chưa được ghi là đã xong.
5. Procurement live intake có BOQ, báo giá và đánh giá của người vật tư. Write
   PO/delivery/payment cần adapter và receipt riêng trước khi triển khai thật.
6. Đo p95/pool wait với nhiều campaign, giảm context theo bằng chứng và tiếp tục
   chia executor/state console khi ranh giới vận hành đã được chứng minh.

Model thiếu nguồn phải hỏi/chờ; output sai phải được validator báo và yêu cầu sửa.
Không cam kết mọi model luôn đúng hoặc không retry. Không làm IT riêng/Bãi Tràm.

## Tiếp tục triển khai — 2026-10-07

Đã đọc và rà bàn giao `82a6c84`, giữ nguyên lifecycle đã nghiệm thu. Đợt tiếp
triển khai owner transaction/attempt cho pipeline native, worker Temporal,
fleet và demo: commit claim trước runtime, heartbeat độc lập, checkpoint usage,
chặn kết quả về muộn sau Cancel và chặn coordinator settle khi execution vẫn
đang chạy. Các phép thử và giới hạn có trong REFACTOR_PLAN.md/WORK_REPORT.md.
Các script chẩn đoán lịch sử không mặc nhiên có heartbeat; external receipt và
dispatch bền vững vẫn là mốc tiếp theo, trước khi mở write connector thật.

**Hướng demo do Boss chọn:** Boss giao brief → theo dõi đợt tuyển → nhận việc
cần duyệt → xem hồ sơ onboarding. Màn hình chính của mốc HR sẽ dẫn theo campaign,
không bắt người vận hành mở từng agent rồi chạy task rời. Mỗi campaign có brief,
JD/rubric phiên bản được duyệt, inbox CV riêng, timeline 21 bước, mục việc cần
quyết định và hồ sơ onboarding có nguồn/receipt. Tạo campaign chưa đồng nghĩa
với gửi offer hay cấp production; từng quyền write cần phạm vi và quyết định.

**Thứ tự tiếp theo:** receipt/đối chiếu connector → dispatch bền vững → tách
các template HR, nhận nguồn theo bước và rework/gửi lại ở gate → nghiệm thu một
campaign MEP thật đến onboarding → procurement → các phòng ban đã ưu tiên.
Corpus chuyên gia và đo baseline/candidate vẫn cần người chuyên môn xác nhận.
Không làm tối ưu tenant, phát triển riêng IT hoặc mở Bãi Tràm trong đợt này.

## Bàn giao controller trước đợt transaction — 2026-10-07

Đã triển khai refactor vòng đời controller theo yêu cầu mới của chủ dự án.
Thiết kế, phạm vi đã triển khai và thứ tự các đợt tiếp theo nằm trong
[REFACTOR_PLAN.md](REFACTOR_PLAN.md). Tối ưu tenant nằm ngoài đợt này.

Controller business và blueprint dùng chung khóa, phục hồi và ghi gián đoạn.
API chỉ dispatch lệnh. Tắt bình thường giữ checkpoint để tiếp tục; gửi mail có
kết quả chưa rõ phải đối chiếu trước khi chạy lại. Phép thử kill tiến trình đã
chứng minh phục hồi procurement đủ 13 bước mà không thực thi lại bước đã xong.
Blueprint phục hồi vẫn chờ người duyệt thật. Không có mail thật hoặc model thật
được gọi trong các phép thử refactor.

Tại bàn giao này, bước tiếp theo là transaction và lease của task executor
(đã triển khai ở phần đầu file), receipt của connector,
dispatch bền vững rồi template HR theo campaign. Việc chia nhỏ executor và state
console đi sau các ranh giới vận hành đó. Phục hồi hiện cần lệnh Run hoặc tín hiệu
tiếp tục mới; chưa tự chạy lại mọi workflow khi API khởi động.

## Hướng phát triển sau nghiên cứu ngày 2026-10-06

Đây là file kế hoạch trong repository `/home/vutun/ai_orchestrator`. Báo cáo phân
tích stack, chất lượng và học từ phản hồi nằm trong [RESEARCH_REPORT.md](RESEARCH_REPORT.md).
Cách chạy corpus có trong [examples/organization_eval/README.md](examples/organization_eval/README.md).

Giữ Pydantic, PydanticAI và Temporal. Chưa thực hiện chuyển sang LangGraph.
Phần lifecycle controller đã được chọn để triển khai ngày 2026-10-07. Các phương
án khác trong báo cáo nghiên cứu giữ vai trò tham khảo cho đợt kế tiếp. Các snapshot cũ ở phần dưới là lịch sử kiểm tra,
không phải cam kết rằng hệ thống đã vận hành nghiệp vụ thật.

### Việc đã bổ sung trong đợt củng cố

- Corpus v1 gồm 21 tình huống cho bảy phòng ban, tách bảy ca train và 14 ca
  holdout. Có đủ nguồn, thiếu nguồn và tài liệu chứa chỉ dẫn độc hại. Không dùng
  dữ liệu Bãi Tràm hoặc người thật.
- Công cụ chạy corpus qua TaskExecutionService, lưu execution, audit và báo cáo
  chấm độc lập. Mock playback và model thật được ghi thành hai loại bằng chứng.
- Đề xuất SkillVersion có nguồn task và hash corpus, chưa xuất bản. Có thể thử
  candidate trên holdout trong sandbox, không đổi hướng dẫn production hoặc
  tăng quyền tự chủ bằng kết quả mock.
- Kiểm tra native workflow tuyển MEP đủ 21 bước với CV file-drop được ghi rõ và
  năm hồ sơ onboarding được ghi/đọc lại trong sandbox. Procurement có kiểm tra
  controller đủ 13 bước. Đây chưa phải nghiệm thu mail hoặc mua hàng thật.
- Sửa công cụ kiểm tra delegation để chỉ đọc task thuộc lượt chạy hiện tại.
  Kết quả cũ của tenant không thể làm lượt chạy mới được tính thành công.
- Chỉnh nhẹ màu nền, thanh điều hướng, trạng thái mục được chọn và header thẻ.
- Sửa nghẽn API do đọc migration stamp đồng bộ mỗi lần mở UI. Trong phép đo
  cục bộ khi cache ấm, UI p50 giảm từ 1.999 ms xuống 23 ms; Departments từ
  4.022 ms xuống 135 ms. Lần đầu sau restart vẫn có chi phí đọc stamp.

**Kết quả và điều kiện còn thiếu:** baseline model thật đạt 15/21. Sau chuẩn
hóa vocabulary và thử bài học, holdout cuối đạt 14/14 bằng model thật; mock
release đạt 21/21. Giữ cả kết quả thất bại cũ. Chưa đủ điều kiện tự vận hành
nghiệp vụ thật: cần corpus chuyên gia độc lập, nhiều lượt provider, recovery
và canary tích hợp. Chi tiết/phạm vi phép đo có trong báo cáo nghiên cứu.
Không lấy test kỹ thuật hoặc mock làm chứng nhận chất lượng model.

### Các mốc cần hoàn thành tiếp

| Ưu tiên | Việc | Điều kiện nghiệm thu |
| --- | --- | --- |
| 1 | Chuyên gia HR và Procurement duyệt corpus, bổ sung hồ sơ khó và ca lỗi | Đáp án có căn cứ SOP, phản ví dụ và phiên bản do người phụ trách xác nhận |
| 2 | Đo model trên nhiều lượt và so sánh candidate với baseline | Báo cáo theo phòng ban về chất lượng, lỗi, p50/p95, token, chi phí và retry. Không nâng điểm bằng việc thay dữ liệu test |
| 3 | Triển khai và nghiệm thu refactor lifecycle controller | Thiết kế và thứ tự refactor có trong REFACTOR_PLAN.md. Không làm tối ưu tenant hoặc relay trong đợt này |
| 4 | Nghiệm thu crash/restart, retry trùng, nguồn cập nhật và cổng hỏi thêm | Không mất bước, không chạy hành động hai lần, không dùng phê duyệt sai phiên bản |
| 5 | Chuẩn hóa contract cho từng connector và kiểm tra sandbox | Có cursor, idempotency key, receipt, read-back, timeout, rate limit, quyền theo scope và log lỗi |
| 6 | Nối nguồn thật theo từng canary read-only, bắt đầu từ tuyển MEP | Đối chiếu nguồn thực tế, đúng campaign và quyền. Sau đó mới duyệt mở từng write action |
| 7 | Hoàn tất tuyển MEP thật rồi procurement thật | Đủ từng bước, sản phẩm và quyết định người thật. Onboarding và nhận hàng có chứng cứ độc lập |
| 8 | Mở workflow dài cho Design, QA/QC-HSE, Finance rồi Sales | Có corpus chuyên gia, controller, negative cases và audit toàn quy trình. IT tiếp tục hỗ trợ mail/onboarding |

### Quy tắc tăng tự chủ

Chỉ coi là cải thiện khi candidate tốt hơn hoặc giữ chất lượng trên holdout,
không tăng lỗi nghiêm trọng, và được người chuyên môn xác nhận. Giữ bài học có
version, phạm vi áp dụng và điều kiện thu hồi. Phân biệt bài học quy trình với
huấn luyện trọng số model. Dữ liệu tổng hợp không được tính vào 28 ngày shadow
thực tế hoặc tự động xuất bản thành skill production.

Trước khi mở tích hợp ghi dữ liệu, phải đo bằng canary thực tế, kiểm tra quyền,
receipt và phục hồi. Thông qua lint, mypy và test là điều kiện kỹ thuật. Chất
lượng sản phẩm, SLA và khả năng chịu tải cần các phép đo riêng.

## Ưu tiên hiện tại — cập nhật 2026-10-06

**Thứ tự:** thông báo phê duyệt và UI → soạn agent bằng model free, Boss chỉnh sửa/duyệt, thiết lập workflow → tuyển MEP với hồ sơ thật → procurement → Design, QA/QC-HSE, Finance, Sales. Phát triển riêng phòng IT tạm hoãn; phần nhận CV và chứng cứ onboarding vẫn phục vụ HR. Không mở dự án Bãi Tràm.

- **Đã triển khai và kiểm tra:** chuông khôi phục toàn bộ yêu cầu pending khi mở lại trang; sự kiện yêu cầu/đã duyệt ghi cùng transaction; badge không bị ẩn; kiểm tra dự phòng 5 giây khi mất stream. Mỗi thông báo mở đúng bản duyệt, không chỉ màn hình tổng. Cursor stream tách theo tenant.
- **Đã triển khai và kiểm tra:** sidebar gọn, khoảng icon/chữ ngắn hơn, nút thu gọn có nhãn truy cập và lưu lựa chọn.
- **Đã triển khai và kiểm tra:** bản nháp agent từ model real `:free`, không fallback sang model trả phí/scripted; lưu execution và model usage. Đầy đủ trách nhiệm, đầu vào, task, đầu ra, tiêu chí, SOP nguồn và cổng người duyệt. Boss sửa từng mục, thêm/xóa/sắp xếp bước; sửa tạo phiên bản và vô hiệu bản duyệt cũ. Cung cấp chuỗi bước và điểm kiểm soát từ playbook; mỗi bước nguồn có tham chiếu cần bao phủ. Đầu vào phải là nguồn hoặc đầu ra đã có của bước trước, không là sản phẩm tương lai. Tham chiếu đủ bước chưa chứng minh nội dung đạt chất lượng.
- **Đã triển khai và kiểm tra:** chấp thuận đúng hash tự tạo definition, agent, skill đã phát hành, công cụ chỉ đọc, workflow và toàn bộ task/dependency/gate. Chờ đầu vào thực tế; người vận hành bắt đầu workflow bằng nút Run. Controller giữ thứ tự, nối đầu ra, chặn chạy/retry rời và dừng cả cây khi hủy. Các cổng duyệt sản phẩm tự tiếp tục sau quyết định thật. Quyền tuyển dụng, cấp production, an toàn, phát hành PO/thanh toán không được cấp từ bản nháp model.

**Trạng thái thực tế bản HR:** draft `tsk_01m48f9egjw00p1qrzrkwhf7zn`, phiên bản 2, 26 bước/14 tham chiếu nguồn từ hai SOP. Console đã ghi quyết định chấp thuận của operator cục bộ lúc 18:50 ngày 2026-10-06; đã tạo agent và workflow `tsk_01m48gt2v1y0kyn9q1m6p3mf0k` với 26 task và 26 cổng duyệt, đang chờ đầu vào. Chưa chạy tuyển người thật. Model free ghi ba phản hồi thành công, 55.971 token, 0 USD; phủ tham chiếu chưa chứng minh chất lượng nội dung. Kế hoạch hiện tại nối nhiều quy trình HR thành một chuỗi; mốc tiếp theo phải tách tuyển dụng, payroll, hiệu suất, offboarding thành các workflow độc lập. Việc nhận nguồn theo từng bước đã được sửa trong bản rà soát này: bước đầu chỉ cần nguồn của chính bước đó, bước sau chờ nguồn bổ sung; nguồn đã cung cấp được khóa sau khi có bước hoàn thành.

**Bổ sung trong lần rà soát:** trình soạn cho thêm/xóa/đổi mã nguồn đầu vào, kiểm tra mã trùng; workflow hiển thị dữ liệu còn thiếu của bước hiện tại và có nút cập nhật trạng thái. Nguồn bổ sung không ghi đè bằng chứng đã dùng. Yêu cầu thêm thông tin không gửi tín hiệu từ chối tới Temporal. Script demo và runner thông thường không bắt đầu các task do controller sở hữu; bước kiểm tra sau dọn hàng đợi dùng chung điều kiện chọn với bộ dọn. Script `scripts/audit_agent_blueprint.py` kiểm tra độc lập việc thiết lập từ bản được duyệt, không chứng nhận chất lượng hồ sơ hay hoàn thành nghiệp vụ.

**Rà soát tính mạch lạc:** ánh xạ SOP Sales từ BD/CR và QA/QC-HSE từ HSE/RSK theo playbook, giữ thông tin nguồn/ánh xạ cho người duyệt; job dọn hàng đợi không được hủy/requeue các task do controller workflow sở hữu. Test kiểm tra người duyệt, hash, cách ly tenant, nối đầu ra, hủy khi model đang chạy và bảo toàn task chờ.

**Sau bản này:** (1) đánh giá bản nháp theo từng bước SOP gốc, đủ trường chưa được tính là chất lượng đạt; (2) tách các nhánh vận hành độc lập của một phòng ban thành workflow tái dùng, có trigger và đầu vào riêng; (3) luồng hỏi thêm/sửa sản phẩm sau gate, retry có lịch sử và phục hồi khi process bị dừng; (4) đo độ trễ commit → bell qua browser, thêm nhắc/escalation theo SLA; (5) tuyển MEP live và procurement live theo các mốc dưới đây; (6) shadow thật 28 ngày trước khi nâng quyền. Mỗi mốc có bộ input cố định, phản ví dụ, log mọi bước và audit độc lập.

Model free có quota và độ sẵn sàng riêng; khi hết quota giữ lỗi thực tế và không báo đã soạn/triển khai. Tham chiếu: [OpenRouter free variants và giới hạn](https://openrouter.ai/docs/api-reference/limits), [tool calling](https://openrouter.ai/docs/guides/features/tool-calling).

## Kế hoạch triển khai tiếp theo có điều kiện nghiệm thu

| Task | Phần phải xây | Điều kiện nghiệm thu |
|---|---|---|
| ORG-01 · tách chu kỳ HR | Một kế hoạch phòng ban chứa các template tuyển dụng, payroll, hiệu suất, offboarding độc lập; mỗi template có trigger, nguồn theo bước và cổng duyệt cuối. Mỗi lần chạy giữ snapshot phiên bản được duyệt. | Tuyển MEP không đòi dữ liệu payroll/offboarding; chạy hai đợt không trộn CV, hồ sơ hoặc quyết định. Bản HR v2 đã duyệt được giữ làm lịch sử, bản thay thế phải duyệt lại. |
| ORG-02 · nối kế hoạch với hành động thật | Dùng lại controller MEP/procurement hiện có cho mail, CV, hồ sơ vật tư và chứng cứ; phân biệt bước soạn tài liệu, nhận nguồn, kiểm chứng và duyệt bằng loại bước có executor được cho phép. | Model không tự khai rằng đã thực hiện hành động. Mỗi hành động có receipt/artefact đọc lại được; mọi quyền gửi ngoài, cấp tài khoản thật hoặc chi tiền cần quyết định đúng phạm vi. |
| ORG-03 · sửa và tiếp tục sau gate | Hỏi thêm → người phụ trách nộp nguồn/trả lời → phiên bản sản phẩm mới → approval mới gắn hash mới. Retry tạo lượt mới giữ lịch sử; controller phục hồi từ trạng thái bền vững sau process restart. | Output cũ, người hỏi và lời giải thích vẫn đọc được; approval cũ không duyệt được bản mới. Thử lỗi giữa từng bước, retry trùng và restart trong lúc chờ người. |
| HR-LIVE · tuyển MEP thật | Chốt brief/định biên, policy lương, người phụ trách HR; nhận CV thật theo subject của đợt; ghi hai vòng phỏng vấn, quyết định Boss, offer/acceptance và chứng cứ onboarding. | Tất cả 21 bước có nguồn, sản phẩm, log và quyết định thật ở chế độ live; thiếu nguồn dừng đúng bước. Kiểm toán độc lập, không dùng fixture để báo đã tuyển. |
| PRC-LIVE · mua sắm thật | Chốt BOQ, ba báo giá, hồ sơ NCC, chứng chỉ từng dòng vật tư và người duyệt chất lượng; chạy RFQ/so sánh/DOA rồi kiểm tra hồ sơ giao nhận. | Mỗi vật tư có NCC được xét chất lượng; thiếu dòng, NCC đỏ hay lệch PO–GRN–Invoice chặn hoàn thành. Phát hành PO/thanh toán theo quyền được duyệt riêng. |
| ORG-04 · thông báo có SLA | Đo commit → popup/bell trên browser nhiều lần; reconnect, nhiều trang pending, nhiều tenant; nhắc và escalation theo cấu hình. | Không mất yêu cầu pending hoặc mở nhầm hồ sơ; có số đo độ trễ và kiểm tra timeout. Một quan sát 5,798 giây chưa được tính là SLA. |
| ORG-05 · mở phòng ban và tăng tự chủ | Sau HR/procurement, triển khai Design → QA/QC-HSE → Finance → Sales, bộ đánh giá theo hồ sơ O-Nexus của từng phòng. IT tiếp tục hoãn. | Hồ sơ đạt tiêu chí nội dung qua đánh giá độc lập và thử ngoại lệ; shadow thật 28 ngày trước khi nâng quyền. |

Chọn mở rộng kế hoạch phòng ban thành các template có phiên bản thay vì thêm nhánh điều kiện vào chuỗi 26 bước: template là đơn vị duyệt và tái dùng, instance là đơn vị thực thi và ghi log. Dùng lại executor nghiệp vụ hiện có để giữ một đường xử lý mail/CV và vật tư. Trình bày trong UI theo ba lớp: kế hoạch phòng ban → các chu kỳ công việc → đợt đang chạy; click đợt mở đúng bước, nguồn, sản phẩm, gate và log của đợt đó.

## Kế hoạch workflow O-Nexus

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
| IT — tạm hoãn phát triển riêng | Yêu cầu onboarding từ HR → quyền theo vai trò → duyệt truy cập → cấp trong staging → kiểm thử đăng nhập/quyền → bàn giao → rà soát | Không cấp production từ quyết định mô phỏng |

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

### IT — tạm hoãn theo yêu cầu mới

Không lên lịch phát triển riêng. Chỉ mở lại khi Boss yêu cầu; giữ hỗ trợ mail và onboarding thuộc luồng HR.

**IT-01** nhận yêu cầu onboarding từ HR đã duyệt → **IT-02** lập quyền tối thiểu theo vai trò và danh mục hệ thống → **IT-03** người quản lý truy cập duyệt → **IT-04** cấp trong staging qua adapter, lưu receipt → **IT-05** kiểm đăng nhập và quyền cho phép/cấm → **IT-06** bàn giao, hướng dẫn và nhật ký → **IT-07** rà soát/thu hồi theo thay đổi nhân sự. Nghiệm thu: có bằng chứng cấp và kiểm thử thực tế, thử quyền từ chối, không cấp production bằng quyết định mô phỏng.

Ở mỗi mốc, bổ sung bộ input có phiên bản, kết quả chuẩn do người xét và ít nhất một lỗi buộc dừng ở từng cổng; đánh giá theo việc hoàn thành bước và căn cứ, không chỉ theo số trường JSON. Các adapter production và giao tiếp với ứng viên/NCC/khách hàng cần phạm vi phát hành được xác nhận cho đợt thực tế.

Tham chiếu cơ chế nộp artifact: [OpenRouter tool choice](https://openrouter.ai/docs/guides/features/tool-calling). Runtime yêu cầu đúng hàm submit_evidence và vẫn kiểm tra nội dung độc lập; ép gọi hàm không phải bảo đảm chất lượng kết luận.
