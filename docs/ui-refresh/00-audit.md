# Khảo sát giao diện — Giai đoạn 0

Ngày khảo sát: 2026-10-07, theo múi giờ người dùng Asia/Saigon.
Điểm gốc: `68e4718ab9deacd220cdd354fed21f85b07035eb`; nhánh `ui/refresh`.
**Trạng thái: Giai đoạn 0 hoàn tất; cổng khảo sát đã đạt. Giai đoạn 1 chưa triển khai.**

## 1. Bản đồ file và cách phục vụ

Thư mục `src/ai_orchestrator/web/` có 22 file, tổng 394.674 byte bao gồm tài liệu
kiến trúc và Python. Các số dưới đây được lấy từ filesystem, không từ chú thích
cũ trong AGENTS (chú thích ~140KB/single-file đã không còn phản ánh source hiện tại).

| File | Byte | Dòng |
|---|---:|---:|
| `__init__.py` | 53 | 1 |
| `agent-blueprints.js` | 22,798 | 511 |
| `architecture.md` | 4,576 | 73 |
| `boot.js` | 1,143 | 35 |
| `business.js` | 13,045 | 358 |
| `console.css` | 26,158 | 1537 |
| `core.js` | 50,537 | 1170 |
| `index.html` | 20,784 | 252 |
| `library.js` | 15,975 | 458 |
| `management.css` | 5,970 | 254 |
| `management.js` | 13,183 | 325 |
| `navigation.js` | 10,799 | 330 |
| `operations.js` | 12,700 | 377 |
| `organization.js` | 17,726 | 525 |
| `page.py` | 849 | 31 |
| `palettes.js` | 2,637 | 103 |
| `preferences.js` | 1,847 | 68 |
| `procurement-intake.js` | 11,702 | 294 |
| `settings.js` | 13,889 | 413 |
| `stream.js` | 19,278 | 492 |
| `work.js` | 91,268 | 2375 |
| `workflows.js` | 37,757 | 871 |

`GET /api/v1/ui?org=...` trong `api/stream.py::ui()` kiểm định org ID, gọi
`web/page.py::render_console()`, thay placeholder org/auth/schema/provider, trả
HTML. Không có asset server/font/icon riêng ở hiện trạng. `index.html` là
**template source**, không phải artifact build. CSS được ghép console.css rồi
management.css vào một style; 16 script được ghép vào một script theo thứ tự:

`core → palettes → preferences → navigation → work → stream → management →
organization → agent-blueprints → procurement-intake → workflows → library →
operations → business → settings → boot`.

Đây là script cổ điển chia source file, chưa phải ES modules. Nhiều hàm/state
của Work dùng chung lexical scope. Management modules giữ state trong closure và
đăng ký renderer vào `MANAGEMENT_VIEWS`. Không thêm `import` tùy ý vào file được
nối thành classic script. `render_console()` đọc asset mỗi request; đổi CSS/JS
không cần build. Đổi manifest Python cần restart tiến trình API để nạp code mới.

API giữ cùng origin. `core.js` đọc `?org=` đã được server nhúng và gửi
`x-organization-id`; token từ sessionStorage qua Authorization header. Stream
là fetch/ReadableStream để gửi header, không đưa token vào URL.

## 2. Routing, dữ liệu và ownership

`navigation.js::parseHash()` tạo `{name,arg,section,focus,eventOffset}`. `go()`
đổi hash; hashchange gọi route. Route ẩn view cũ, chọn view, render breadcrumb,
ngắt poller không còn liên quan; `renderToken` và guard chống phản hồi cũ ghi đè
màn mới. Esc đóng `#sheet` trước, sau đó quay lại history trong console; không
quay lại khi đang gõ input/textarea/select.

Work detail thực sự dùng `#view-task`, mặc dù route chính là `#/give/:id`.
Không đổi nhầm view vì tên route: đây là lỗi đã từng xảy ra và có regression check.
Các alias `#/task/:id`, `#/console` vẫn được xử lý. Hash lạ fallback về give.

`loadRegister()` gọi `/ceo/work?limit=200`, tăng offset cho tới đủ `total`, rồi
`renderGive()` ghi mọi hàng qua innerHTML. Tìm kiếm tên/owner/ID và bucket được
lọc phía client. `registerSnapshot` cho tìm kiếm không phải tải API lại; nhưng
DOM vẫn dựng lại mỗi lần gõ. Poll list 10 giây; task detail poll theo trạng thái.
Không có virtualization hoặc phân trang hiển thị. `.queue-scroll` giới hạn 64vh
và overflow auto; 1.188 task nằm trong DOM, không chỉ phần đang thấy.

Snapshot GET của lượt khảo sát đầu xác nhận: **1.188 total, 351 needs_you, 0 in_flight,
837 settled**. “Settled” là bucket terminal theo server; không tự diễn giải
837 là 837 task thành công. Không sửa bucket hoặc cách tính trong đợt UI.

## 3. Trang, route và màn phụ

| Nhóm | Route công khai / màn con | Nội dung cần giữ |
|---|---|---|
| Công việc | `#/give`, `#/console` | List, 4 chỉ số, tìm kiếm/bucket, tạo việc, activity và tree |
| Task | `#/give/:task_id`, `#/task/:task_id` | overview, steps, assignment, decisions, log |
| Task log | `#/give/:id/log/page/:offset` | Pagination event, đúng task và offset |
| Sự cố | `#/work/issues`, `#/work` | Filter failed/blocked/no owner, mở reason và đúng task |
| Phê duyệt | `#/work/approvals`, `#/approval/:id` | Queue và sheet proposal, approve/reject/request info |
| Tổ chức | `#/departments`, `#/dept/:key` | Chief, offices, departments, công việc đúng đơn vị |
| Agent | `#/agent/:agent_id` | Controls, state, budget, capability, tool/skill bindings |
| Quy trình | `#/processes`, `/catalogue`, `/catalogue/:sop_id` | SOP và nội dung nguồn |
| Workflow | `#/processes/workflows`, `/workflows/:root_id` | MEP/procurement, stage/evidence/revision/feedback/control |
| Tuyển dụng mẫu | `#/processes/hiring` | Example đã seed, không đại diện mọi đợt tuyển |
| Định nghĩa | `#/processes/definitions`, `/definitions/:id` | Model, instruction, schema và authority |
| Provision | `#/processes/provision`, `/provision/:draft_id`, `/provision/workflow/:root_id` | Soạn/duyệt draft và operating plan |
| Thư viện | `#/library/skills`, `/skills/:id`, `/tools`, `/tools/:id`, `/memory` | Kỹ năng/version, tool/schema, memory search/write có nguồn |
| Dữ liệu | `#/business/projects`, `/projects/:id`, `/documents`, `/documents/:id` | Corpus, WBS/workspace, document versions/distribution |
| Vận hành | `#/operations/models`, `/usage`, `/usage/:agent_id` | Profiles, probe form, usage với đúng filter |
| Sự kiện | `#/operations/events`, `/events/:event_id` | Record thực và liên kết subject |
| Quyết định | `#/operations/decisions`, `/decisions/:agent_id` | Agent/task decision history |
| Kiểm toán | `#/operations/audit` | Audit rows và record details |
| Kiểm soát | `#/operations/governance` | Policies, authority, auditability |
| Hệ thống | `#/operations/system` | Health/model/schema/runtime status |
| Cài đặt | `#/settings`, `/organization`, `/units`, `/roles`, `/appearance` | Permission từ server; prefs browser riêng |

Danh sách có phân trang dùng `/page/:offset` của `Management.offset/pager`; không
xóa hoặc chuyển page thành resource ID. Workflow journey còn có anchor stage
trong trang, phải cuộn đúng bằng chứng và giữ nguyên hash chứa root.

Màn phụ hiện có: `#sheet` native dialog dùng chung cho proposal/phê duyệt, toàn
văn kết quả, step, offer/hiring và xác nhận hủy task; `#notifPanel` bell popover;
`#newTaskForm` disclosure; auth warning details; issue/step/event/record details;
procurement intake và hiring revision disclosure; blueprint editor, document
write forms, table schema editor và memory forms. Sidebar thu gọn bằng class,
mobile hiện biến thành navigation hàng ngang; **chưa có drawer/bottom sheet**.
Command palette và style guide **chưa tồn tại**.

Native `prompt/confirm` vẫn được dùng cho token, lý do approval, retry và một số
controls. Khảo sát không xác nhận/sent các hành động đó. Ảnh sheet dùng hàm trình
bày hiện có và dữ liệu task đọc thật, không submit. Dữ liệu dự án/tài liệu hiện
rỗng; màn chi tiết cần fixture GET riêng hoặc corpus thật ở gate tương ứng,
không dựng dữ liệu mẫu vào tổ chức.

**Lỗi route đã truy nguồn:** `organization.js` tạo “Work in this unit” với agent
ID; `work.js::findByKey()` chỉ so key `chief/front-office/...`. Khi bấm, thông báo
“No such department” rồi quay về sơ đồ. Sửa caller UI thành key thực tế, không
đổi contract backend hoặc xóa route theo key. Baseline giữ nguyên lỗi này.

Ảnh theo route và kết quả từng URL đã có trong report.json và mục lục index.html.
Ảnh viewport cố ý không chụp một PNG cao hàng trăm nghìn pixel cho 1.188 hàng;
hiệu năng được đo riêng trên toàn danh sách.

## 4. Theme và preferences

`palettes.js` định nghĩa navy/teal/indigo/forest/copper/graphite, mỗi màu có ba
accent light/dark: accent, accent-2 (hover), accent-soft. Hai bộ neutral chung
cho mọi palette: bg, surface, surface-2, nav-bg, text/text-2/text-3, line,
line-strong, fill/fill-hover, sunken, late/late-soft, ok/ok-soft, warn/warn-soft,
on-accent. console.css còn có alias panel/panel-2/text-1/bad/bad-soft/font-mono.
Nền chưa pha sắc theo palette; Graphite vẫn ngả xanh. Chưa có status info đầy đủ,
accent-border hoặc token fg/bg/border thống nhất cho mỗi status.

`preferences.js` normalize lựa chọn, bắt matchMedia theme system, ghi CSS custom
properties inline ở html. Dataset: theme, palette, density, motion, home,
color-mode (resolved). Defaults auto/navy/comfortable/auto/campaigns.

| Storage | Khóa | Phạm vi |
|---|---|---|
| localStorage | `onx-ui-preferences-v1` | JSON theme/palette/density/motion/home |
| localStorage | `ao-lang-v1` | vi/en |
| localStorage | `ao-sidebar-collapsed` | true/false |
| localStorage | `ao-notif-seen-v2:<ORG>` | Notification IDs đã đọc, bounded |
| sessionStorage | `ao_token` | Service credential, không được ghi vào artifact |

Preferences chỉ apply khi script ở cuối body chạy. HTML ban đầu lang=en và
palette CSS mặc định teal, rồi JS chuyển sang lựa chọn đã lưu. Có nguy cơ flash
sai palette/theme. Chưa có theme-color meta. Density hiện chỉ đổi padding vài
card/table, chưa điều khiển row/button qua token. Reduced-motion hiện dùng
!important override; sẽ chuyển về layer và token.

## 5. Component lặp và nợ CSS

Có primitive nút `.btn`, `.pill`, `.card`, `.row`, `.seg`, `.note`, `.reason`,
`.empty`, `.field`, `.toolbar`; Management tự thêm form/table/card/detail helper.
Điều này giúp chia feature nhưng chưa tạo API component nhất quán. `.pill`
Management hiển thị raw status trung tính; Work nối status/type/owner bằng dấu
chấm; stream lại có mapping màu riêng. Input Work và Management có style khác
selector, bảng dùng heading uppercase. Nhiều font weight 650/750/800 ngoài thang
400/500/600.

Đếm tĩnh trên hai CSS hiện tại:

- 0 `@layer`; 6 `!important` (hidden và reduced-motion).
- 29 lần xuất hiện màu literal (bao gồm token gốc, **không phải 29 vi phạm**).
- 495 giá trị px literal (bao gồm token; cần chuyển dần theo trách nhiệm).
- z-index 30/100/200 không có semantic layer token.
- Selector lặp gồm nav, brand, content, stats, row, workspace-tabs, campaign
  workspace/controls/main, palette-grid và record-fields. Một phần là media query
  hợp lệ; một phần ghi đè cùng phạm vi giữa console.css và management.css.
- Markup/render có inline layout/font/spacing; CSS audit riêng không đếm hết
  inline styles. Component mới sẽ nhận token/variant thay cho style tự phát.
- Specificity cao thấy ở `.sidebar-collapsed .nav a > span:not(.ic):not(.count)`
  và `:root[data-density="compact"] .card > .body`. ID selector như `#notifCount`
  còn thắng rule component: nền accent với chữ `--surface` đáng kiểm riêng ở
  mode tối. Sidebar chọn dùng box-shadow inset, còn focus dùng outline 3px +
  offset 3px; hai dấu hiệu có thể cùng hiện và tạo cảm giác viền kép.
- `[hidden]` đang cần important vì display rule khác có thể thắng. Không bỏ rule
  đó trước khi migrate selector tương ứng và kiểm keyboard/visibility.
- JS còn dọn các node project đã nghỉ (`projDetail`) và `navWorkCount` ẩn có
  caller cập nhật. Không xóa chỉ vì không thấy trên screenshot; tìm caller và
  verifier trước.

`source-audit.json` ghi file sizes, enum và selector lặp. Browser CSS coverage là
chứng cứ “chưa được dùng trong ma trận này”, không chứng minh dead CSS: disabled,
hover, print, error, quyền khác và dữ liệu chưa có có thể chưa được kích hoạt.

## 6. Trạng thái và loại

Nguồn chuẩn: `domain/enums.py`; giữ string wire contract. Không dịch giá trị gửi
API; chỉ dịch nhãn UI. Mọi trạng thái phải có chữ và hình, không chỉ màu.

| Nhóm | Giá trị |
|---|---|
| `TaskStatus` | `created`, `queued`, `assigned`, `running`, `blocked`, `waiting_for_input`, `waiting_for_approval`, `completed`, `failed`, `canceled`, `expired` |
| `TaskType` | `research`, `analysis`, `report`, `review`, `decision`, `execution`, `coordination` |
| `TaskPriority` | `low`, `normal`, `high`, `urgent` |
| `ApprovalStatus` | `pending`, `approved`, `rejected`, `needs_information`, `expired`, `canceled` |
| `AgentLifecycleStatus` | `draft`, `provisioning`, `active`, `paused`, `degraded`, `suspended`, `retired` |
| `AgentRuntimeStatus` | `idle`, `ready`, `busy`, `waiting`, `degraded`, `blocked`, `unavailable` |
| `AgentHealth` | `healthy`, `degraded`, `unhealthy`, `unknown` |
| `DelegationStatus` | `proposed`, `accepted`, `rejected`, `in_progress`, `completed`, `failed`, `canceled`, `timed_out`, `blocked` |
| `SubagentStatus` | `spawning`, `running`, `completed`, `failed`, `canceled`, `expired` |
| `SkillGovernanceState` | `trusted`, `reviewed`, `experimental`, `deprecated`, `blocked` |
| `PolicyDecisionType` | `allow`, `deny`, `require_approval`, `escalate` |
| `RiskLevel` | `low`, `medium`, `high`, `critical`, `privileged` |
| `RunMode` | `live`, `simulation`, `replay` |
| `MemoryTier` | `run_context`, `working`, `task_episodic`, `agent`, `department`, `organization`, `semantic`, `audit` |
| `DataClassification` | `public`, `internal`, `confidential`, `restricted`, `secret` |

Ngoài bảng: AgentResultStatus trong contracts.py gồm completed/blocked/failed/
needs_approval/needs_input/delegated/canceled; VersionState trong promotion.py
shadow/active/superseded/rejected. Stream tree còn có pending/rejected/delegated;
đây là projection, không phải trạng thái task mới. Workflow root và stage dùng
TaskStatus; mode thực tế là simulation/live/replay. Nhãn mô phỏng phải luôn rõ.

Các trạng thái bổ sung đã truy code, tách khỏi TaskStatus:

| Nguồn | Giá trị |
|---|---|
| WorkflowCommand (commands/drivers/feedback) | not_requested, pending, running, paused, waiting, suspended, dispatch_failed, superseded |
| workflow_lifecycle projection | stopped, failed, reconciliation_required, suspended |
| workflow feedback | requested, thinking, failed, awaiting_confirmation, applied |
| ConnectorAction CHECK trong persistence/models.py | prepared, sending, confirmed, unknown |
| Document control (application/document_control.py) | draft, approved, superseded |
| Procurement artifact (application/business_workflow.py) | draft_not_issued |

WorkflowCommand.state hiện là string không có CHECK giới hạn, feedback nằm trong
JSONB. Bảng ghi các giá trị tìm thấy trong code hiện tại; không tuyên bố có enum
khép kín khi backend chưa có. Unknown value phải hiện neutral với nguyên tên,
không chuyển thành thành công hoặc lỗi giả.

## 7. Cách tái chạy và giới hạn baseline

Native stack, không Docker: PostgreSQL 55432, NATS 4222, Temporal 7233/UI8233.
API đang phục vụ 8100. Không chạy `make page` trong khảo sát vì target còn seed và
sweep dữ liệu. Khi cần một API UI độc lập, với DB đã chạy:

```sh
AO_API_AUTH_DISABLED=true AO_MODEL_PROVIDER_DEFAULT=fake AO_EMBEDDING_PROVIDER_DEFAULT=hash uv run -m uvicorn ai_orchestrator.main:app --host 127.0.0.1 --port 8100
make lint
make typecheck
make test
uv run --with playwright --with fonttools --with brotli python scripts/ui-font-study.py --download
uv run --with playwright python scripts/ui-shots.py --org org_01m3ycsehgwz7v1fk2ahswkwhm --chromium /home/vutun/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome
```

`--download` chỉ tải tài sản phát triển có phiên bản cố định; chụp lại không cần
mạng ngoài. Thay --chromium bằng executable của máy khác. Không có Playwright
trong dependency runtime sản phẩm. `--strict` bật những assertion hiện đã triển
khai; Giai đoạn 0 ghi nhận lỗi cũ, không ép baseline phải đạt chuẩn UI cuối.

Lint baseline qua (406 files formatted), mypy qua (177 source files, note section
tests.* unused). Suite unit/integration: **3.476 passed, 3 skipped, 1 deselected,
621,98 giây**. Có **10 warning openpyxl** khi đọc corpus: không hỗ trợ Conditional
Formatting extension và không parse được một số header/footer. Không phải lỗi
UI mới; giữ nguyên log, không tắt warning để lấy kết quả sạch giả.
Suite dùng provider fake/hash theo conftest, không gọi model/mail thật.
Sau khi thêm công cụ audit, lint được chạy lại cho toàn repo; số file mới có
format tăng nhưng source sản phẩm vẫn nguyên.

Không chạy live-model, không duyệt approval, không gửi email, không seed corpus
Bãi Tràm, không đổi DB vận hành. Các screenshot của record hiện có chỉ là quan
sát trạng thái, không là bằng chứng nghiệm thu workflow thật.

## 8. Font và thiết kế tiếp theo

Xem `01-design-plan.md`, font-study/index.html và screenshots. Sáu họ được tải
cục bộ, giữ license và provenance. Bản sản phẩm chỉ chọn Inter + JetBrains Mono.
Thiết kế mới dùng dải chỉ số, toolbar, hàng có cấu trúc, trạng thái ngữ nghĩa;
không thêm dashboard thứ hai. Ba phương án tĩnh ở `layout-study.html` dùng dữ
liệu GET và được gắn nhãn nghiên cứu, không được phục vụ trong app.

## 9. Kết quả đóng cổng Giai đoạn 0

Cổng `scripts/ui-phase0-gate.py` đã đạt; receipt ở
`artifacts/ui-shots/00-baseline/stage-0-gate.json`. Source sản phẩm, tests,
migrations và dependency manifest giữ nguyên so với commit gốc. Hash từng file
UI cũng trùng `source-audit.json`. Chỉ thêm hồ sơ, tài sản nghiên cứu và công cụ
khảo sát; không đổi backend/API/schema hoặc quyền nghiệp vụ.

- 57 URL đại diện, mỗi URL chụp light/dark ở 1440×900 và 390×844. Route động
  được chuẩn hóa theo loại ID, không đếm mỗi SOP/record là một trang mới.
- 282 ảnh trong report chính, 56 ảnh màn phụ/fixture, tổng **338 ảnh** có metadata.
  Ảnh specimen font và hai prototype bố cục là nghiên cứu riêng, ngoài tổng này.
- Đủ 24 trường hợp trang Công việc: 6 palette × 2 mode × 2 density; thêm
  360×800, 768×1024 và 1920×1080. Style guide chưa tồn tại, sẽ chụp ở Giai đoạn 2.
- Đủ loading/empty/no-results/error/long-title/long-error/all-zero. Fixture chỉ
  thay GET trong browser, có nhãn rõ; không ghi dữ liệu hư cấu vào tổ chức.
- Thêm ảnh danh sách sau cuộn ở cả bốn mode/viewport để nhìn rõ hàng task.
- 0 request tới host ngoài localhost, 0 request ghi nghiệp vụ trong các probe.
  API khảo sát dùng fake/hash để startup không chạy workflow/mail monitor.
- 0 pageerror JavaScript; 1 console error HTTP 503 từ fixture error chủ động.
  Giữ nguyên thông báo đó trong JSON, không che lỗi bằng lọc toàn bộ console.
- 0 ảnh có horizontal overflow; zoom 200% của probe bổ sung không cuộn ngang.
  Đây là phạm vi đã đo, chưa chứng nhận mọi trạng thái/zoom/trình duyệt.
- 72 cặp chữ/nền được đo trong 24 trường hợp đều đạt 4,5:1. Chưa bao gồm mọi
  status, link, focus ring, icon và viền input; không tuyên bố toàn app đạt AA.
- Reduced-motion không có transform animation đang chạy trong hai probe.

### Hiệu năng và snapshot thực tế

Lượt mới có **1.189 task**, 351 needs_you, 0 in_flight, 838 settled; mốc 1.188
trong brief/prototype và khảo sát đầu được giữ làm lịch sử. Không sửa dữ liệu để
ép khớp số liệu brief. `settled` bao gồm các trạng thái terminal, không đồng nghĩa
mọi task đã thành công.

Thời gian từ bắt đầu tải đến register sẵn sàng: **588,2 ms**. Cuộn 60 frame trên
1.189 hàng: **977,8 ms**, frame lớn nhất **17,4 ms**, **0 long task** quan sát được.
List cao 121.034px trong viewport riêng 576px. Kết quả headless cục bộ này là mốc
trước/sau, chưa phải SLA hoặc nghiệm thu cuộn trên thiết bị của người dùng.

### Lỗi baseline phải xử lý ở các giai đoạn sau

Axe ghi bốn nhóm; số sau là số lượt chụp có rule vi phạm, không phải số node duy nhất:

| Rule | Lượt chụp | Hướng xử lý |
|---|---:|---|
| nested-interactive | 4 | Sửa cấu trúc SVG/cây tương tác và thứ tự focus |
| aria-prohibited-attr | 8 | Đặt role/nhãn đúng cho campaign progress |
| link-in-text-block | 4 | Phân biệt link bằng underline và tương phản phù hợp |
| scrollable-region-focusable | 2 | Vùng văn bản cuộn truy cập được bằng bàn phím |

Giai đoạn 0 ghi nhận các lỗi này; `--strict` phải từ chối hiện trạng, không sửa
source để lấy baseline đẹp giả. Cổng hiện tại chỉ kiểm hồ sơ đầy đủ và bất biến
source; `final_ui_acceptance` vẫn false. Clipping chữ Việt trong mọi component,
keyboard/focus, prepaint và browser ngoài Chromium sẽ được bổ sung ở cổng tương ứng.

### Tự phản biện sau khi xem ảnh

Ảnh danh sách mobile xác nhận tên, ID, loại, trạng thái và lỗi chưa có vùng rõ;
“bạn” vẫn lẻ bên phải. Bỏ phương án card cho từng KPI và bảng nén mất lý do lỗi.
Giữ phương án dải chỉ số + hàng có cấu trúc; cần test nội dung lỗi dài và dấu Việt
khi triển khai, không đánh giá bằng prototype tĩnh. 71 PNG không còn được report
tham chiếu từ lượt bị ngắt đã chuyển vào `.devdata/ui-refresh-interrupted-baseline/`;
không xóa bằng chứng cũ hoặc trộn chúng vào mục lục của lượt đã hoàn tất.
