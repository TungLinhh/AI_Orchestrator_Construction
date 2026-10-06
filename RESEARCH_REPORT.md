# Vận hành O-Nexus và lựa chọn framework

Ngày nghiên cứu: 2026-10-06. Kết luận áp dụng cho code hiện tại, không phải dự
đoán rằng một framework sẽ bảo đảm chất lượng mọi công việc.

## Kết luận

Giữ Pydantic, PydanticAI, Temporal, PostgreSQL và NATS. Chưa có bằng chứng cần
chuyển hệ thống sang LangGraph. Ưu tiên quy trình đúng nghiệp vụ, đo chất lượng,
phục hồi khi lỗi và kiểm soát tích hợp. Chuyển framework trước khi đo các vấn đề
này sẽ tạo thêm chi phí di chuyển mà chưa giải quyết được nguyên nhân.

Pydantic và LangGraph đảm nhiệm các việc khác nhau. Pydantic kiểm tra hợp đồng
dữ liệu. PydanticAI chạy agent và công cụ. LangGraph tổ chức graph và trạng thái
agent. Ngay cả khi dùng LangGraph, dự án vẫn cần schema và kiểm tra dữ liệu.

## Căn cứ từ code

| Thành phần | Trách nhiệm đang có | Điều cần củng cố |
| --- | --- | --- |
| Pydantic | API và hợp đồng domain | Version hợp đồng giữa các lần deploy |
| PydanticAI và ModelGateway | Chạy agent, routing, privacy, budget, usage | Độ tương thích tool-call và kết quả model |
| Temporal | Retry, timer, signal, recovery workflow | Kiểm tra recovery cho controller nghiệp vụ mới |
| PostgreSQL, SQLAlchemy, RLS | Task, artifact, approval, audit và cách ly tenant | Đo truy vấn lớn và số vòng truy cập database |
| NATS, outbox, consumer | Sự kiện có receipt và xử lý trùng | Theo dõi backlog, tuổi sự kiện, retry và dead letter |
| Console được ghép từ module | Điều hướng, hồ sơ, workflow, inbox, log | Đo tải trang, reconnect và thao tác trên dữ liệu lớn |

`application/task_execution.py` là đường thực thi chung. Các controller
`business_workflow.py` và `agent_blueprints.py` còn tự giữ trạng thái nghiệp vụ
trong task. Việc có Temporal cho task không chứng minh mọi controller sẽ tự
phục hồi đầy đủ sau khi process bị kill. Cần nghiệm thu từng controller.

`skill_learning.py`, `skill_learner.py`, `learning.py`, `learning_revert.py` và
`procedure_operations.py` đã có đề xuất, kiểm duyệt, shadow và thu hồi bài học.
Nên nối đánh giá chất lượng vào các cơ chế này trước khi bổ sung hệ thống nhớ mới.

## Chất lượng đầu ra

Mỗi workflow cần brief có phiên bản, nguồn bắt buộc, sản phẩm từng bước, chủ
kiểm tra và điều kiện từ chối. Bộ chấm bằng mã kiểm tra số liệu và bằng chứng.
Người chuyên môn kiểm tra tính đầy đủ, tính đúng trong ngành và chất lượng diễn
giải. Có thể dùng model làm người chấm phụ sau khi đối chiếu với chuyên gia.

Tài liệu [Anthropic về đánh giá agent](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)
phân biệt lời agent báo hoàn thành với trạng thái thực tế sau chạy và đề xuất
kết hợp bộ chấm bằng mã, model và con người. Với O-Nexus, phải đọc lại task,
artifact và receipt, sau đó kiểm tra chất lượng sản phẩm theo SOP. JSON đủ trường
chỉ là một phần của kiểm tra.

Lưu một corpus có phiên bản cho mỗi phòng ban. Tách dữ liệu luyện tập và dữ
liệu đánh giá. Đo tỷ lệ hoàn thành, giá trị tính sai, trích dẫn không có thật,
thiếu nguồn, vượt cổng duyệt, p50 và p95 thời gian chạy, token, chi phí và số retry.
Các lần đổi prompt, model hoặc skill phải chạy lại corpus cố định. Không lấy
model tạo sản phẩm làm người duy nhất quyết định sản phẩm đạt chất lượng.

Đợt này bổ sung corpus v1 gồm 21 tình huống. Đây là tập kiểm tra cơ bản, chưa
bao phủ toàn bộ SOP của mọi phòng ban. Workflow MEP và procurement có kiểm tra
toàn bộ bước bằng controller gốc. Các phòng ban khác có đánh giá nhiệm vụ mẫu,
chưa có bằng chứng hoàn thành quy trình dài đầy đủ.

## Học và thích nghi

Vòng đề xuất là chạy việc, kiểm tra độc lập, phân loại lỗi, tạo đề xuất có task
và phiên bản nguồn, đánh giá trên holdout, người phụ trách duyệt, rồi shadow thực
tế. Khi tăng lỗi hoặc gặp bằng chứng phủ định, thu hồi phiên bản và giữ lịch sử.
Không dùng số lượt mock làm số ngày quan sát thật. Không tự xuất bản bài học.

Các bài học trong bộ mock là quy tắc luyện tập kèm kết quả quan sát và nguồn gốc,
không phải huấn luyện lại trọng số model. Candidate được thử trong đầu vào của
thí nghiệm tổng hợp. Cần đo trên nhiều lượt và corpus của chuyên gia để chứng
minh cải thiện. Độ tin cậy của bài học phải giới hạn theo phòng ban và tình huống.

[Pydantic Evals](https://pydantic.dev/docs/ai/evals/evals/) đã hỗ trợ tổ chức
dataset, case, experiment và evaluator. Có thể dùng khi cần mở rộng báo cáo.
Bộ chấm nhỏ hiện tại không cần thêm dependency để kiểm tra các số liệu xác định.

## PydanticAI hay LangGraph

| Câu hỏi của O-Nexus | Stack hiện tại | LangGraph |
| --- | --- | --- |
| Hợp đồng dữ liệu và đầu ra | Pydantic, schema và validator đang dùng | Vẫn cần schema và kiểm tra nghiệp vụ |
| Chờ Boss trong nhiều ngày, retry và restart | Temporal và trạng thái database đang có | Persistence, checkpoint và interrupt cần nối lại |
| RLS, approval theo hash, DOA, audit | Cơ chế riêng của O-Nexus | Không thay thế các cơ chế này |
| Workflow suy luận có nhánh động phức tạp | Có thể triển khai trong runtime hiện tại | Graph có thể làm luồng nhánh dễ quan sát hơn |
| Rủi ro chuyển đổi | Củng cố từng phần, giữ lịch sử | Di chuyển checkpoint, replay, công cụ và UI |

[PydanticAI hỗ trợ Temporal](https://pydantic.dev/docs/ai/capabilities/durable_execution/temporal/).
Tài liệu cũng yêu cầu chú ý version hợp đồng của workflow chạy lâu. Dự án hiện
dùng activity riêng của Temporal; chưa sử dụng mọi cơ chế trong tích hợp này.
[LangGraph có persistence](https://docs.langchain.com/oss/python/langgraph/persistence)
và [interrupt cho người duyệt](https://docs.langchain.com/oss/python/langgraph/interrupts).
Các tính năng này hữu ích, nhưng thêm chúng cần xác định rõ hệ thống nào sở hữu
trạng thái, retry và lịch sử. Hai bộ điều phối cùng sở hữu một bước sẽ khó phục hồi.

Chỉ đề xuất thử LangGraph khi có một workflow nhánh động cụ thể mà code hiện
tại khó biểu đạt. Thử riêng trong sandbox và đo cùng corpus: chất lượng, thời
gian, chi phí, resume khi crash và tải vận hành. Không thay toàn bộ stack chỉ để
có graph. Quyết định di chuyển cần dựa vào kết quả thử và được chủ dự án duyệt.

## Chuẩn bị tích hợp ứng dụng thật

Mỗi connector cần phân quyền theo scope, nguồn và cursor có phiên bản, idempotency
key, receipt, read-back, timeout, retry có giới hạn, rate limit và xử lý lỗi rõ ràng.
Test bằng server hoặc transport sandbox có ca timeout, duplicate, mất kết nối,
payload lớn và dữ liệu thiếu. Sau đó chạy một canary read-only với tài khoản thật.
Chỉ mở write sau khi chủ sở hữu xác nhận quyền và phạm vi hành động.

Tách ngân sách theo task và tenant. Giới hạn concurrency theo provider và
connector. Đo pool database, queue depth và backlog. Không giữ transaction và
row lock xuyên suốt một cuộc gọi model nếu có thể thiết kế bước checkpoint phù
hợp. Các thay đổi kiến trúc để đạt mục tiêu này cần được xem xét riêng.

OpenRouter free có thể thiếu quota hoặc trả lỗi từ upstream. Có fallback free
và log không đồng nghĩa có SLA. Cần số liệu ổn định qua nhiều thời điểm trước khi
hứa lịch hoàn thành cho công việc thật.

## Refactor đề xuất, chưa thực hiện

1. Tách kế hoạch HR thành template tuyển dụng, payroll, performance và offboarding.
   Mỗi campaign giữ snapshot đã duyệt và nguồn riêng. Đây là thay đổi domain cần
   chủ dự án quyết định trước khi triển khai.
2. Chuẩn hóa lifecycle của controller quanh checkpoint, lease, resume và việc
   đối chiếu receipt khi hành động bị gián đoạn. Giữ một chủ sở hữu trạng thái cho
   mỗi bước. Cần thử recovery trước khi đổi đường thực thi.
3. Tối ưu báo cáo và relay theo số tenant. `OutboxRelay.health()` hiện gọi nhiều
   truy vấn cho từng tenant. Database test có 11.613 tenant tích lũy đã khiến
   lần kiểm tra relay kéo dài, nên phải đo và thiết kế lại phép tổng hợp mà vẫn
   giữ RLS. Đợt này dùng quy trình reset database test sẵn có để chạy gate sạch.

Chưa đổi framework, schema database hoặc cấu trúc controller trong đợt này.

## Kết quả đo ngày 2026-10-06

Lượt model thật với contract kiểm tra bằng chứng đạt **15/21**. Có 28 phản hồi
model được ghi ModelUsage, sáu phản hồi lỗi, chín phản hồi qua fallback và
110.620 token; chi phí ghi nhận 0 USD. Không có fake model trong lượt này.
Thời gian một task: p50 19,014 giây, p95 114,875 giây. Model gồm Dots primary,
Ling và Nemotron free fallback. HTTP rejection không có phản hồi usage nên số
28 không phải tổng tất cả HTTP request đã thử.

Bốn ca injection bị từ chối HTTP 400, một ca hết budget khi sửa sản phẩm,
và Sales trả trạng thái `draft_pending_approval` thay vì giá trị contract
`draft`. Ca Sales này cho thấy cần chuẩn hóa vocabulary trạng thái và đánh giá
lại contract với chuyên gia, không suy ra đã gửi hay cam kết với khách. Một
probe qua cùng đường thực thi tái hiện 400 từ AtlasCloud với thông báo
`bad request`; metadata chưa đủ chỉ ra chính xác lý do. Không đổi lỗi này thành
thành công hoặc tự retry vô hạn. Sửa lựa chọn tool từ named choice sang
`required` đã khắc phục lỗi tương thích riêng được tái hiện trên fallback;
không chứng minh mọi lỗi 400 đã được giải quyết.

Lượt train tiếp theo đạt 6/7, lượt holdout với candidate đạt 13/14; tất cả
14 execution của holdout hoàn thành. Đã sửa contract Sales thành enum
`draft`, `sent`, `blocked`, giữ trạng thái chờ duyệt ở `decision`. Hai lượt
kiểm tra riêng Sales normal/injection sau sửa đều đạt bằng Dots primary. Không
thay gold answer. Protocol 2 ghi hash schema và chỉ lấy một đề xuất đạt mới
nhất cho mỗi ca train, tránh lặp hướng dẫn từ các bản lịch sử. Các lượt cũ vẫn
được giữ; không cộng các lần retry này để báo một lượt 21/21 model thật.

**Lượt holdout cuối theo protocol 2 đạt 14/14**, mỗi ca dùng đúng một đề xuất
train. Có 20 phản hồi model thật, không fake, bốn phản hồi lỗi và bốn fallback;
65.512 token, 0 USD ghi nhận. Không execution thất bại hoặc HTTP rejection
trong lượt này. p50 task 19,874 giây, p95 59,26 giây. Mock release theo cùng
protocol đạt 21/21 và vẫn ghi `model_quality_measured=false`. Cả hai giữ
`production_ready=false`. Kết quả tốt chưa đủ để quy nguyên nhân cho bài học:
contract/prompt đã thay đổi và chưa kiểm soát biến động provider qua nhiều lượt.

Bảy ca train và 14 biến thể holdout đủ để kiểm tra cơ chế ban đầu. Biến thể
injection dùng cùng dữ kiện nghiệp vụ với ca train, nên tập này chưa chứng minh
khả năng khái quát trên hồ sơ và số liệu mới. Cần corpus chuyên gia độc lập,
nhiều seed và nhiều lượt trước khi tăng quyền. Không thay đáp án để nâng điểm.

### Hiệu năng console

Phát hiện `/ui` chạy subprocess Alembic đồng bộ trên mỗi request, gây nghẽn cả
health và API. Đã đưa phép đọc stamp sang thread và cache kết quả trong process.
Sau migration phải restart API để stamp cập nhật. Có regression test kiểm tra
cache và event loop vẫn tiến triển khi subprocess chờ.

| Endpoint | p50 trước sửa | p50 sau sửa, cache ấm | p95 sau sửa, cache ấm |
| --- | --- | --- | --- |
| Health | 8,71 ms | 7,05 ms | 70,19 ms |
| UI | 1.999,31 ms | 23,47 ms | 39,13 ms |
| Departments | 4.022,33 ms | 134,99 ms | 168,27 ms |

Mỗi endpoint đo mười request, concurrency ba, trên máy cục bộ. Không có lỗi
HTTP trong các lượt trước/sau có server chạy. Lượt cold sau restart có UI p95
5.170,49 ms: cache chưa ấm, nhiều request đầu có thể cùng đọc stamp. Đây chưa
phải SLA hoặc benchmark nhiều tenant. Công cụ đo lại:

```sh
uv run python scripts/measure_console.py --org <org_id> \
  --output .devdata/reports/console-latency.json
```

Đã đọc UI trong browser, mở từ hồ sơ task sang đúng nhật ký của task đó và
kiểm tra màn hình tổ chức. Màu nền/nav/header và dấu chọn được chỉnh nhẹ;
không thay mô hình điều hướng trong lần củng cố này.

### Tích hợp Gmail tiếp theo

Với connector Gmail API, đề xuất tách quyền đọc CV và quyền gửi thư. Google
liệt kê `gmail.readonly` cho việc đọc và `gmail.send` cho việc gửi, đồng thời
yêu cầu chọn scope hẹp phù hợp. Scope đọc vẫn cho phép đọc hộp thư rộng hơn một
campaign; bộ lọc campaign ở ứng dụng phải tiếp tục được kiểm tra. Không coi
scope OAuth là bộ lọc hồ sơ tuyển dụng. Quyết định thêm connector chưa được
thực hiện trong đợt này. Tham khảo
[scope chính thức của Gmail](https://developers.google.com/workspace/gmail/api/auth/scopes?hl=en).

Các báo cáo gốc nằm trong `.devdata/reports/organization-live-final-20261006.json`
và `console-latency-{20261006,fixed-20261006,warm-20261006}.json`. Lịch sử lỗi
trước đó được giữ. JSON và log cục bộ không được đưa vào Git vì các lượt dùng
nguồn thật sau này có thể chứa dữ liệu riêng.

Lượt cuối: `.devdata/reports/organization-candidate-final-20261006.json` và
`organization-mock-release-20261006.json`; hai ca Sales riêng nằm trong
`organization-sales-{contract,holdout}-20261006.json`. Corpus không đổi hash
giữa các lượt; protocol 2 ghi thêm hash output schema cho từng case.

## Kiểm tra kỹ thuật

`make lint` qua cả Ruff và format: 367 file. `make typecheck` qua 162 source
file. Suite sạch qua `make test-fresh`: 3.374 passed, ba skipped, một deselected.
E2E với PostgreSQL, NATS và Temporal qua 34 test. Sau khi siết contract và loại
bài học trùng, 38 test liên quan evaluator/cache/proposal qua. `make page` qua
kiểm tra Node với API thật, gồm điều hướng, log, workflow và hồ sơ CV.

Logs: `.devdata/reports/test-fresh-final-20261006.log`,
`e2e-final-20261006.log`, `evaluation-final-20261006.log`,
`page-final-20261006.log`. Không đổi schema, thêm dependency hoặc refactor lớn.
