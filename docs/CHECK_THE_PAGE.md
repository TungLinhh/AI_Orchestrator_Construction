# Hướng dẫn kiểm tra lại page

Viết lại sau khi dựng lại UI. Mọi thứ dưới đây đã chạy thật trên máy này.

---

## 0. Trang này mở ra ở đâu

Không phải Dashboard. Trang mở ở **Departments**, vì câu hỏi hằng ngày là *"sáu phòng ban này
đang làm gì"*, không phải *"công trình kia tiến độ thế nào"*.

```
Executive Agent
├── Procurement        (Mua sắm)
├── HR                 (Nhân sự)
├── Sales              (Kinh doanh)
├── Finance            (Tài chính)
├── QA/QC-HSE
├── Design & M&E       (Thiết kế & M&E)
└── Knowledge · Project Mgmt   (tier hai)
```

Bấm hộp nào → xem **lần chạy** (bên trái) và **việc** (bên phải).

---

## 1. Chạy một lệnh

```bash
cd /home/vutun/ai_orchestrator
make page
```

Nó bật Postgres nếu chưa chạy → **cấp phát dòng user cho người vận hành cục bộ** (không có
 nó thì nút Approve trả 500) → bật API ở cổng 8099 với chế độ demo → tìm tenant →
**tự chạy kiểm tra page** → in ra URL để mở.

Kiểm tra gồm **97 câu**, trong đó 16 câu dành riêng cho sáu phòng ban. Lần gần nhất:

```
the departments:
  ok    the department tree rendered  — 7 box(es)
  ok    all six departments are present  — 6
  ok    the chief is the root of the tree
  ok    every box carries its open and done counts
  ok    every box shows its grant against its ceiling  — 7 box(es)
  ok    a lit box means a run in flight, nothing else  — lit=0 api=0
  ok    a stranded run is drawn differently from a live one  — dashed border, not a moving light
  ok    the tiles rendered  — 480 chars
  ok    it counts stranded runs separately
  ok    the department panel opened  — Mua sắm — Procurement Agent
  ok    it shows what the agent did  — 3 run(s)
  ok    it shows the work beside it  — open, attention and done
  ok    open and finished are separated  — 0 open · 0 need attention · 2 done
  ok    there is a way back to all six
  ok    the breadcrumb offers the way out

  all checks passed
```

Dừng: `make page-stop`

> **Đèn sáng chạy quanh viền** chỉ hiện khi có execution đang chạy **thật**. Muốn thấy nó,
> mở `#/give` → bấm **Run** trên một task đang `created`, rồi quay lại `#/departments` sau
> vài giây. Trang tự polling mỗi 10 giây, không cần F5.

---

## 2. Vấn đề bạn báo: không thoát ra được — đã sửa, kiểm được

Đây là phần quan trọng nhất, nên nó có test riêng.

Bản cũ lưu project đang chọn trong **một biến JavaScript**. Đó là ngõ cụt, theo ba
cách khác nhau:

| Hậu quả | Nay |
|---|---|
| Không có nút Back | Có `‹ Back` ở thanh breadcrumb, **luôn hiện** |
| Nút Back của trình duyệt về nơi bạn đứng *trước cả website* | Điều hướng bằng hash, nên Back của trình duyệt đúng nghĩa |
| Tải lại là mất | URL giữ trạng thái — dán cho đồng nghiệp cũng được |

Thêm hai điều nữa:

* **`Esc` luôn quay lại.** Gắn ở `document`, nên có một cách thoát duy nhất và biết
  trước, ở bất cứ đâu trong sản phẩm.
* **Danh sách không bao giờ bị thay bằng chi tiết.** Trên màn rộng chúng nằm cạnh
  nhau; dưới 1080px thì chi tiết mở ra nhưng danh sách vẫn đó, và breadcrumb có Back.
  Đây là nửa còn lại của "không thoát ra được" — không phải mất danh sách, chỉ mất một
  cú bấm để quay lại.

Nút Back **không bao giờ tắt lại** sau khi bạn đã đi vào bất cứ đâu. Không tồn tại
trạng thái nào mà bạn "đang ở đâu đó" mà không có lối ra nhìn thấy được.

**Tự kiểm:** bấm một project → nhìn thanh breadcrumb có `‹ Back` → bấm `Esc` → bạn về
dashboard. Bấm nút Back của trình duyệt → về project. Cả hai đều phải đúng.

---

## 3. Cấu trúc mới

| Mục | Dùng để làm gì |
|---|---|
| **Dashboard** | "Có gì cần tôi xử lý" — số liệu, hàng đợi, trễ hạn, portfolio |
| **Projects** | Danh sách + chi tiết cạnh nhau: thanh phân rã, cây WBS, bảng tiến độ |
| **Approvals** | Hàng đợi **có nút bấm** — Approve / Ask / Reject |
| **Agents** | Danh sách tác nhân + **nút dừng / chạy lại** |
| **Decisions** | Nhật ký quyết định — có thể lọc theo kill / revive / refusal |
| **Console** | Cây phân công + event stream + tạo task (giữ nguyên phần cũ) |

**Role switcher** (CEO / PM / Approver) **đổi thứ tự** các panel, không ẩn panel nào.
Bản cũ nó bật/tắt `hidden` — đó là lý do cảm giác như nó đang giấu thứ khỏi bạn. Nay
sidebar không bao giờ đổi, mọi panel đều tới được, chỉ cái nào lên trước thay đổi.

### Xử lý công việc — phần trước đó thiếu hoàn toàn

Bản cũ có hộp thư duyệt **và không có nút duyệt**. Bạn thấy việc chờ mà không chạm
được vào. Nay:

* **Approve** / **Reject** — có hộp xác nhận.
* **Ask** — gửi câu hỏi của bạn về cho agent thay vì kết thúc task. Khi bạn chưa chắc,
  đây gần như luôn là đáp án đúng.
* Nút **Stop** trên từng agent, và nó **bắt buộc phải có lý do** — database từ chối một
  lần kill không có lý do, và trang nói rõ điều đó trước khi gửi.
* Thất bại thì **báo lỗi ngay trên trang**, không im lặng.

---

## 4. Quy tắc duy nhất: chưa đo ≠ đúng hạn

**Cần đếm bằng mắt:** vào một project → thanh **Work breakdown**.

Phải có **36 thanh gạch sọc trong 40**, và **1 thanh đỏ**.

| Trạng thái | Hình |
|---|---|
| Đo rồi, trễ hạn | thanh đỏ đặc |
| Đo rồi, đúng hạn | thanh xanh đặc |
| **Chưa đo** | **viền đứt, không có thanh** |

Lý do: `TĐ BOH.xlsx` có 110 hoạt động, **100%** để trống cột thực tế — và khi có, chúng
giống hệt ngày dự kiến, không gì đánh dấu là đã ghi nhận. Một cái thanh là một *chiều
rộng*, và chiều rộng là một *phép đo*. Zone chưa đo thì không đứng được thanh.

**Nếu bạn thấy 40 thanh xanh, trang đang nói dối.** Đó là bug.

Ở bảng **Progress**: **288 dòng** in nghiêng mờ = chưa đo. Chúng không hiện số ngày
trễ — vì không có số nào để hiện.

---

## 5. Mobile

Thu nhỏ cửa sổ xuống **375px** và bấm **Approvals**. Đó là bề mặt mobile (Tập 1 §Tầng 1:
*"phê duyệt HITL trên mobile"*).

Kiểm: sidebar xuống thành thanh ngang cuộn được, nút Approve/Reject vừa tay, không
tràn ngang.

Đây là thứ **không test nào bắt được** và là lý do bước này tồn tại.

---

## 5b. Agent thực sự làm việc — `make demo`

Trước khi sửa, console **vẽ 189 event thật thành những dòng trống**: nó đọc
`ev.detail` và `ev.at`, mà payload chỉ có `data`, `occurred_at`, `view`. Nên bạn không
thấy agent nào làm gì — không phải vì chưa agent nào chạy, mà vì trang không đọc được
payload đó.

Giờ chạy:

```bash
make demo
```

Sẽ in ra:

```
  nvidia/nemotron-3-ultra-550b-a55b:free answered 'ONLINE'
  Executive Agent: bound to profile 'primary'

  runtime.tools_exposed  names=['delegate_to_agent', 'safe_web_search', 'write_report']
  delegation.applied     target='Program Agent'  status=accepted

  status     completed

  --- delegation tree (1 hop(s)) ---
      └ Program Agent  [accepted]
        Produce a weekly progress report for BÃI TRÀM ESTATES…

  --- model calls ---
    2 x openrouter/dots-studio/dots-3-note-preview:free  $0.0000
```

Một agent thật, trên một model thật miễn phí, **giao việc cho agent khác**, tốn 0 đô.

Sau đó mở trang → **Console**: cây phân công sẽ có nhánh, và feed sẽ chạy.

Lưu ý: task chạy là `coordination`, và một task coordination tự làm hết thì **bị hệ
thống fail cố ý** ("the agent had 8 agents it could have handed work to and did the work
itself"). 24/47 task trong dữ liệu cũ fail đúng như vậy. Đó là quy tắc phân tách
trách nhiệm, không phải bug.

## 6. Muốn tự kiểm sâu hơn

```bash
ORG=org_01m3h7j45b6cj3jnphq2ggjteq     # make page in ra dòng này
curl -s -H "x-organization-id: $ORG" http://127.0.0.1:8099/api/v1/portfolio | python3 -m json.tool | head -20
curl -s -H "x-organization-id: $ORG" "http://127.0.0.1:8099/api/v1/ai-decisions?limit=5" | python3 -m json.tool
```

Sau khi bấm **Stop** trên một agent, chạy lại `ai-decisions` — bạn sẽ thấy một dòng
`decision: "killed"` với `actor_type: "system"`, `rationale` là lý do bạn nhập, và
`agent_name` là tên agent. Đó là bằng chứng kill switch hoạt động và có ghi vào nhật ký.

Nếu bấm **Stop** mà không nhập lý do, trang **không gửi request** và nói rõ vì sao.
Nếu bạn gọi API trực tiếp với `{"reason": "x"}` thì nhận `422`.

---

## 7. Phần còn thiếu, nói thẳng

**Trang vẫn chưa được ai nhìn thấy.** Không có trình duyệt nào kết nối vào phiên làm
việc này, và máy cũng không có bản headless.

`make page` **thực thi JavaScript của trang** với DOM giả và API thật rồi kiểm tra đúng
những gì nó vẽ ra — 40 thanh, 36 gạch sọc, 288 dòng chưa đo, và nó **điều hướng vào một
project rồi quay lại** để chứng minh lối ra hoạt động.

Nó không nói được:

* bố cục có đẹp và dễ đọc không — đây là điều **tôi không kiểm được**, và tôi đã viết
  lại toàn bộ stylesheet mà không có cách nào xem kết quả;
* màu có qua được đo tương phản không;
* khoảng cách có đúng không.

**Phần đó cần bạn mở lên nhìn.** Nếu thấy chỗ nào lộn xộn, chỉ tôi chỗ đó — tôi sửa
được, nhưng tôi không tự thấy được. Riêng phần **Console** thì đã kiểm được bằng máy:
`make verify-page` đọc stream thật và xác nhận có delegation `Executive Agent →
Program Agent`.

Còn lại trong `docs/PLAN_FINISH.md`: Phase 2c (composite FK, 135 khoá), Phase 3
(document control), rồi 8 agent.
