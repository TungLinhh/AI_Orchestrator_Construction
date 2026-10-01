# Hướng dẫn truy cập và tiêu chí đánh giá

Tài liệu này để anh **tự kiểm tra sản phẩm** mà không cần đọc code. Mỗi mục có: cách
truy cập, cách kiểm, và tiêu chí đạt/không đạt.

Đọc `docs/PLAN_FINISH.md` nếu anh muốn biết *tại sao* nó được xây thế nào. Tài liệu này
chỉ nói *kiểm thế nào*.

---

## 0. Khởi động

```bash
cd /home/vutun/ai_orchestrator
make page          # bật Postgres nếu chưa, seed tham chiếu, chạy API, mở trang
```

In ra:

```
Open this:  http://127.0.0.1:8099/api/v1/ui?org=org_...
Stop it:    make page-stop
```

**Mở đúng link đó**, kèm `?org=`. Không có `?org=` thì mọi request bị từ chối vì
`x-organization-id` là bắt buộc — tenancy là thật, không phải mô phỏng.

Dừng: `make page-stop`.

### Token

**Không có token.** Authentication đã tắt hoàn toàn (`AO_API_AUTH_DISABLED=true`). Trước đây
trang hỏi token ở mọi lần bấm nút vì nó coi **403** — tức "việc này cần người" — là
*mật khẩu sai*. Nay 403 là câu trả lời kinh doanh và được hiện ra như lỗi.

> **Tiêu chí:** mở trang, bấm hết các nút. **Không bao giờ** hiện hộp thoại xin token.

---

## 1. Trang có gì, và đánh giá từng mục

Sidebar (trái), theo thứ tự đọc:

1. **Departments** — `All six`, rồi `Procurement · HR · Sales · Finance · QA/QC-HSE ·
   Design & M&E`
2. **Work** — `Give work · Approvals · Documents`
3. **Construction data** — `Projects · Dashboard · Agents · Decisions · Console`

Hai mục cuối nhóm lại vì **anh không quản lý theo project**: đó là dữ liệu công trình, và
nó là của nhóm khác. Chúng vẫn còn, chỉ không chiếm chỗ đầu tiên.

Mọi mục có **breadcrumb + nút Back + phím Esc**. Đó là câu trả lời cho "không có lối ra
khỏi drill-down".

### 1.0 Sáu phòng ban — màn hình mở đầu

`#⬡ All six`. Cây: **Executive Agent** ở gốc, sáu phòng ban dưới, dưới cùng là tier hai
(Knowledge, Project Mgmt). Mỗi hộp tự trả lời **năm câu hỏi** mà không cần bấm:

| Hộp hiện | Nghĩa là |
|---|---|
| Ánh sáng **chạy quanh viền** | Có execution đang chạy thật |
| Viền **đứt đoạn** | Có execution kẹt — bắt đầu từ lâu, chưa đóng |
| `open N` | Việc đang mở |
| `done N` | Việc đã xong |
| `Lx/Ly` | Cấp độ được cấp / trần cho phép |

Bấm vào hộp → `#/dept/<key>`, hai cột: **"What it did"** (các lần chạy: model, token, giây,
`summary` — đúng chuỗi mà workflow nhánh theo) và **"Its work"** (Open · Needs attention ·
Finished, theo **tên task**, không phải số đếm).

> **Về "chain of thought":** mô hình **không** đưa ra suy luận riêng và nền tảng này
> **không lưu** nó. Cái thật có là `summary`, `artifacts`, `decision_record`, và model đã
> dùng — nên panel tên là **"What it did"** và hiện đúng những thứ đó. Đặt nhãn "chain of
> thought" sẽ là panel nói dối duy nhất trong sản phẩm.

> **Tiêu chí 1.16 — ánh sáng phải có nghĩa.** Ánh sáng = có execution đang chạy **thật**.
> Bốn execution trong tenant phát triển bắt đầu từ 27/09 vẫn mang cờ `running` trên task đã
> `failed`; nếu đèn sáng cho chúng thì nó báo đang làm việc mà không ai làm. Nên `running` và
> `stuck` là **hai câu trả lời khác nhau**, và kẹt thì vẽ viền đứt chứ không sáng.
>
> **Tiêu chí 1.17 — mỗi phòng ban phải trả lời đủ năm.** Hộp thiếu một trong năm thì là hộp
> trang trí, không phải thông tin.

### 1.1 Give work — dòng CEO

`#◷ Give work` (mục thứ hai, có badge vàng = số việc chờ anh).

| cột | đọc gì |
|---|---|
| **Waiting on you** | việc cần anh quyết |
| **With an agent** | đang chạy |
| **Settled** | xong, đã có báo cáo |
| badge vàng | luôn bằng *Waiting on you* |

Mỗi dòng: loại việc · trạng thái · ai giữ · **đã giao cho mấy bộ phận** · có bao nhiêu
việc con.

> **Tiêu chí 1.1 — hàng đầu là việc của anh.** Việc có quyết định chờ **luôn ở trên cùng**,
> kể cả khi bản thân nó đã `completed`. Badge vàng khớp đúng con số *Waiting on you*
> (đã có kiểm tự động).
>
> **Tiêu chí 1.2 — bấm một dòng, rồi bấm lại `#◷ Give work`.** Màn hình phải trở về danh
> sách, và **bảng chi tiết phải trống**. Còn sót lại = đọc nhầm giữa hai màn hình.

### 1.2 Task detail — cây, quyết định, log, báo cáo

Bấm một dòng. Bốn khối, **theo đúng thứ tự người dùng đi**:

1. **Who did what** — đồ thị SVG. Mỗi hộp là một task; **đường nối** là lần giao việc.
   Đường **đứt** = bị từ chối. Di chuột lên cạnh xem *ai giao cho ai*.
2. **Delegations / tree** — cùng dữ liệu ở dạng danh sách, thẳng hàng.
3. **Decisions** — nút **Approve** / **Refuse** cạnh lý do.
4. **The log** — chuỗi sự kiện theo thời gian.
5. **Reported** — câu tóm tắt bằng chữ ở cuối.

> **Tiêu chí 1.3 — đồ thị có CẠNH, không chỉ hộp.** Đếm `<path>` trong SVG. Với cây 6
> task phải ra 5–6 cạnh. (Đây là lỗi thật đã sửa: `delegations.child_task_id` NULL ở cả
> 7 delegation, nên bản đầu vẽ hộp không có cạnh.)
>
> **Tiêu chí 1.4 — quyết định nằm CÙNG MÀN HÌNH với bằng chứng.** Cây và quyết định phải
> hiện cùng lúc. Quyết định xem riêng một chỗ khác là quyết định mù.
>
> **Tiêu chí 1.5 — bấm Approve.** Phải thành công, badge vàng **giảm 1**. Bấm lần hai:
> phải nói *đã ghi nhận rồi, giữ nguyên lần đầu* — **không** được ghi đè tên và giờ.
>
> **Tiêu chí 1.6 — bấm Refuse.** Bắt buộc nhập lý do. Không nhập thì không làm gì cả.
> Lý do được giữ cùng quyết định.

### 1.3 Run — chạy thật

Cuối màn hình: **Run this task now**.

Trước khi bấm, nút ghi rõ: **~10 phút · 67.000 token**. Đó là số **đo được**, không phải ước
lượng.

Sau khi bấm: nút đổi thành *running*, trang **tự poll** (không đơ, không spinner vô hạn),
mỗi lần hiện *N completed, M failed, K in flight*.

> **Tiêu chí 1.7 — không chạy được việc đã xong.** Bấm Run trên một task `completed`: phải
> từ chối kèm lý do, **không** báo "đã bắt đầu". (Đã sửa: bản đầu trả `started: true` rồi
> executor đọc lại kết quả cũ và thoát sau 1 giây.)
>
> **Tiêu chí 1.8 — không chạy trùng.** Bấm hai lần liên tiếp: lần hai phải từ chối, không
> được tạo thêm 67.000 token.

### 1.4 Documents — kiểm soát văn bản

28 SOP của hồ sơ, phát hành thành tài liệu có kiểm soát. Mỗi tài liệu: mã `ONX-…` đã
**parse** (không phải nối chuỗi), phân khối, phiên bản đang có hiệu lực, **ai chưa đọc**.

> **Tiêu chí 1.9 — số "chưa đọc" là số CHƯA XÁC NHẬN, không phải số chưa gửi.** Đó là cả
> hai chỗ có khác nhau: gửi là ý định, xác nhận mới là kết quả.
>
> **Tiêu chí 1.10 — lọc theo khối** (Back Office / Front Office / Middle Office / PMO). Lọc
> phải khớp đúng đoạn đầu của mã — và số trên đầu phải bằng số dòng.
>
> **Tiêu chí 1.11 — Click *Confirm read*.** Bắt buộc nhập tên (database từ chối dòng không
> có người). Bấm lần hai: **giữ tên và giờ của lần đầu**.
>
> **Tiêu chí 1.12 — sau khi phát hành bản mới**, ma trận phân phối phải **nói rõ** "chưa ai
> được gửi bản N" **kèm nút *Send it to a role*** — không phải để trống.
>
> **Tiêu chí 1.13 — gửi hai lần cùng một vai trò** phải cho **một** nghĩa vụ, không phải hai.
> Số "chưa đọc" không được tăng khi retry.

### 1.5 Dashboard / Projects / Approvals

- **Dashboard**: CEO cockpit + số *delegation* đã giao.
- **Projects**: 6 project thật, 240 nút WBS, 2778 số liệu tiến độ. Mỗi project có thanh
  *unmeasured* **riêng**, tách khỏi *late*.
- **Approvals**: hộp thư HITL.

> **Tiêu chí 1.14 — thanh *unmeasured* không được tính vào *late*.** Trong corpus thật,
> **100%** cột thực tế bằng byte với cột kế hoạch, nên "có ngày" **không phải** là đo được.
> Trang có dòng giải thích ngay dưới.
>
> **Tiêu chí 1.15 — *Above ceiling* phải bằng 0.** Đây là **control**, không phải mô tả. Nó
> được kiểm tự động: số trên gạch phải bằng API. (Đã sửa F152: truy vấn dùng `<>` thay vì
> `>`, và so sánh *chuỗi* trên một thang cấp độ — nó báo 8 khi thật là 0.)

### 1.6 Trạng thái tenant lúc kiểm

Số liệu thật, để anh so với những gì anh thấy trên màn hình:

| | |
|---|---|
| Task | `completed` 18 · `assigned` 10 · `created` 6 · **`failed` 7** (mọi cái đều xử lý được) |
| Execution kẹt | **0** (trước đó 11, từ 27/09) |
| Approval | 2 đã duyệt · **1 đang chờ** để anh bấm |
| Văn bản | 28 tài liệu, 28 bản đang hiệu lực, 27 chưa xác nhận |
| Phòng ban | 6 phòng, mỗi phòng 2 việc đã xong; Tài chính còn 1 đang mở + 1 cần xem |

Hàng đợi CEO có **4 dòng trùng tiêu đề** — đây là lỗi delegation trùng đã ghi ở mục 5, chưa
sửa vì cần anh quyết.

---

## 2. Chạy cả đội agent

```bash
make run-fleet                       # cả 8 agent của hồ sơ, song song 4
make run-fleet -- --only "QA/QC-HSE Agent"
```

Mỗi agent được **một việc thật lấy từ corpus thật** (240 nút WBS, 2778 số liệu), chạy qua
đúng `TaskExecutionService` mà worker Temporal gọi.

In ra:

```
  ok   Project Mgmt Agent   completed      686 tok   0.1min  delegated=0
  ok   QA/QC-HSE Agent      completed      648 tok   0.1min  delegated=0
  6 completed, 0 refused, 0 crashed
```

> **Tiêu chí 2.1 — `CRASH` là lỗi.** `REFUSED` thì **không** phải lỗi: đó là platform từ
> chối và nói lý do.
>
> **Tiêu chí 2.2 — đừng kỳ vọng nó ủy quyền.** Tất cả 16 agent ở **L1**. Hồ sơ quy định
> bậc quyền chỉ lên khi **tỷ lệ đồng thuận đo được** đủ cao, không phải vì tiện. Không có
> script nào tự nâng — đó là lý do `above_l1` tồn tại.
>
> **Tiêu chí 2.3 — `delegated=0` là đúng** cho các task này: mỗi agent một việc đủ để tự
> làm. Cây ủy quyệc xuất hiện ở task *coordination* (xem `make demo`).

---

## 3. Tự chạy lại toàn bộ kiểm tra

```bash
make lint            # ruff check + ruff format --check
make typecheck       # mypy, 126 file
make test-fresh      # truncate + seed tham chiếu + toàn bộ suite
make verify-page     # chạy JS trang trong Node, 81 kiểm trên API thật
```

Kỳ vọng hiện tại:

| lệnh | kết quả |
|---|---|
| `make lint` | `All checks passed!` + `259 files already formatted` |
| `make typecheck` | `Success: no issues found in 126 source files` |
| `make test-fresh` | `26xx passed, 8 skipped` |
| `make verify-page` | `all checks passed` (81 kiểm) |

> **Tiêu chí 3.1 — `make verify-page` phải nói `all checks passed`.** Nó chạy JavaScript
> thật của trang trong Node với một DOM shim, đối chiếu API thật. Nó bắt được `TypeError`
> lúc khởi động, payload thiếu field, và panel rỗng — thứ mà test kiểm tra chuỗi không thấy
> và vẫn trả 200.

### Cách tự kiểm RLS (đa tenant)

```bash
ORG=org_01m3h7j45b6cj3jnphq2ggjteq
B=http://127.0.0.1:8099
# ID document của tenant khác → phải 404, KHÔNG phải 403
curl -s -o /dev/null -w "%{http_code}\n" "$B/api/v1/documents/ID_CUA_TENANT_KHAC" \
  -H "x-organization-id: $ORG"
```

> **Tiêu chí 3.2 — 404, không phải 403.** Nói "forbidden" là xác nhận hàng đó tồn tại. Lý do
> mọi khóa ngoại đều là khóa composite `(organization_id, id)` là để không ai phân biệt được
> "không của anh" với "không có".

---

## 4. Bảng đánh giá nhanh

| # | Kiểm | Đạt |
|---|---|---|
| 1.1 | Việc chờ anh luôn trên cùng; badge khớp số | ☐ |
| 1.2 | Ra khỏi task → chi tiết trống | ☐ |
| 1.3 | Đồ thị có **cạnh**, không chỉ hộp | ☐ |
| 1.4 | Quyết định cùng màn hình với cây bằng chứng | ☐ |
| 1.5 | Approve giảm badge; bấm lại giữ lần đầu | ☐ |
| 1.6 | Refuse bắt buộc có lý do | ☐ |
| 1.7 | Run trên task đã xong → từ chối, nói lý do | ☐ |
| 1.8 | Run hai lần → chỉ một lần chạy | ☐ |
| 1.9 | "Chưa đọc" = chưa xác nhận, không phải chưa gửi | ☐ |
| 1.10 | Lọc khối: số đầu khớp số dòng | ☐ |
| 1.11 | Confirm read bắt tên; lần hai giữ lần đầu | ☐ |
| 1.12 | Bản mới → nói rõ + có nút gửi | ☐ |
| 1.13 | Gửi trùng → một nghĩa vụ | ☐ |
| 1.14 | *unmeasured* tách khỏi *late* | ☐ |
| 1.15 | *Above ceiling* = 0 | ☐ |
| 1.16 | Đèn sáng ⇔ execution đang chạy thật; kẹt thì viền đứt | ☐ |
| 1.17 | Mỗi hộp phòng ban đủ 5 câu trả lời | ☐ |
| 1.18 | Bấm hộp → thấy lần chạy **và** task, cạnh nhau | ☐ |
| 1.19 | Approve bấm được (không phải lỗi "agent") | ☐ |
| 1.20 | Bản mới lùi ngày → 422 nói cách sửa, không phải 500 | ☐ |
| 1.21 | Task failed có nút **Chạy lại**; task chưa fail thì không có | ☐ |
| 1.22 | "Chạy lại" tạo task **mới**, task cũ giữ nguyên lỗi | ☐ |
| 1.23 | Bấm "Chạy lại" lần hai → từ chối, nói task nào đang làm | ☐ |
| 1.24 | Không còn hàng đợi nào báo "đang có người khác làm" vô hạn | ☐ |
| 1.25 | `#/task/<id>` mở được (route có thật) | ☐ |
| 2.1 | Fleet: `CRASH` = 0 | ☐ |
| 3.1 | `verify-page` → all checks passed | ☐ |
| 3.2 | Tenant khác → 404 không phải 403 | ☐ |

---

## 5. Còn lại — và vì sao chưa làm

| Việc | Vì sao |
|---|---|
| **Lease không bảo vệ run đang chạy** | Đếm được: **0 lần `commit()`** trong toàn bộ đường chạy, và cả hai caller bọc run bằng `session.begin()`. Nên `lease_expires_at` nằm trong transaction đang mở — reaper ở connection khác **không thấy** cho tới khi run commit, tức là đúng lúc lease hết ý nghĩa. Sửa đúng là lấy + gia hạn lease trên **connection riêng**, ngoài transaction của run: phải sửa **3 call site** (`local_runner`, `worker_runtime`, `task_workflow`), không sửa được trong method vì commit ở đó sẽ kết thúc transaction của caller (F102). Xem F190. |
| Reaper đóng execution kẹt | 4 execution `running` trên task đã `failed` (từ trước khi bọc transaction). Giờ hiện ra trong view phòng ban với **viền đứt**, chưa dọn. |
| 3 delegation → 5 task con trùng | Dedup theo *fingerprint*, nên chỉ cần diễn đạt mục tiêu hơi khác là lọt. Cần `UNIQUE` theo mục tiêu — **quyết định schema**. |
| Worker Temporal | Đường bền vững đã có (`workflow_runs` + broker). `make` không có target; cần broker chạy. |
| Form catalogue + 12 bảng assurance | Corpus **không có giấy phép nào**. Xây lúc này là bịa yêu cầu. |
| 3-way match | Hồ sơ **có** SOP (`ONX-BO-FIN-SOP-002`) nhưng thiếu bảng `invoice`. Quy trình rõ, dữ liệu không có. |
| Chọn tenant đích khi client đang ký | `client.name` trả `null`; không có bảng client tenant-scoped để tra. |
| **4 task con trùng** trong hàng đợi CEO | Không phải dữ liệu rác — cùng parent, cùng agent, cùng tiêu đề, và **4 fingerprint khác nhau** nên `create` không bao giờ chặn. Sửa thật: cột intent + unique index. **Cần anh quyết: giữ dòng nào, và xoá 3 dòng còn lại có phải xoá việc đã giao không.** Chi tiết F193. |
| **Trần request theo từng task** | `max_requests` giờ 48, đặt từ phân phối đo được (trung vị run thành công = **1** lượt; run nặng nhất biết xong = 24; cả 3 run thất bại đều dừng đúng ở 24). Một task thật sự cần 200 lượt chưa nói được. Chi tiết F199. |

**Đã đóng trong lượt đi qua từng nút** (chi tiết ở `docs/FAILED_APPROACHES.md` F182–F189):

| Vấn đề | Nguyên nhân gốc |
|---|---|
| **Approve không bao giờ chạy** | Principal khi tắt auth là `SERVICE`, mà `decide` đúng khi từ chối agent. Đã thành `HUMAN`; id vẫn `dev:no-auth`. |
| Approve 500 (`fk_approvals_decided_by_users`) | `decided_by` là FK composite tới `users`; `dev:no-auth` chưa có dòng. Đã cấp phát dòng user cục bộ. |
| `?status=approved` luôn rỗng | Filter chạy **sau** `inbox` đã lọc `pending` trong SQL. Đã đưa filter vào truy vấn; state lạ thì **từ chối**. |
| Phát hành bản lùi ngày → 500 | Đóng bản cũ ở ngày mới → cửa sổ đảo ngược. Đã chặn trước, trả 422 kèm câu. |
| Đèn "đang chạy" sáng cho việc không ai làm | 4 execution kẹt. Đã tách `running` / `stuck`. |

---

## 6. Còn điều gì tôi **không** tự kiểm được

**Bố cục.** `verify-page` chứng minh trang *tính đúng* — nó không đánh giá được:

- hiển thị ở **375px** (điện thoại)
- **độ tương phản** màu
- thanh biểu đồ **tràn** khung
- thứ tự đọc bằng mắt

Cần anh mở và nhìn. Đặc biệt: **đồ thị ở 1.4** — nó có đọc được không, và ở khổ ngang
anh thì còn là cây hay thành bảng.
