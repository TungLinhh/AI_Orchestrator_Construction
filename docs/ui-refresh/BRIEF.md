# BRIEF: Nâng cấp toàn bộ giao diện O-Nexus Orchestrator

> Bạn là một design engineer cấp cao, vừa có gu thiết kế tinh tế vừa viết CSS/JS rất kỷ luật. Nhiệm vụ: nâng cấp toàn bộ giao diện của sản phẩm theo brief này. Đọc hết brief trước khi làm gì. Làm theo từng giai đoạn, có cổng kiểm tra, và tự kiểm chứng bằng screenshot thật.

---

## 0. Mục tiêu một câu

Biến giao diện hiện tại thành một sản phẩm **sang trọng, chuyên nghiệp, công phu mà tinh tế**, theo hướng **chính xác và tiết chế kiểu Linear / Vercel**: người dùng nhìn vào thấy ngay sự kỷ luật, cảm nhận được sự chăm chút ở từng chi tiết nhỏ, nhưng không có thứ gì trang trí thừa.

"Sang" ở đây đến từ **kỷ luật** (lưới, tỷ lệ, độ tương phản, nhịp điệu, trạng thái đầy đủ), không đến từ hiệu ứng phô trương. Đừng sao chép tài sản thương hiệu của Linear hay Vercel; chỉ học triết lý của họ.

---

## 1. Bối cảnh sản phẩm

- Tên: **O-Nexus Orchestrator**, một bảng điều khiển để theo dõi, giao việc và phê duyệt công việc cho các agent AI.
- Frontend: **HTML/CSS/JS thuần**, do backend phục vụ trực tiếp tại `/api/v1/ui` (ví dụ `http://127.0.0.1:8100/api/v1/ui?org=...#/give/<task_id>`), điều hướng bằng **hash routing**.
- Style hiện tại: **CSS thuần + CSS variables tự viết**. Chân trang sidebar có chỉ báo trạng thái `trực tiếp` và phiên bản schema (`schema 0034`).
- Ngôn ngữ UI: **tiếng Việt là chính**, có nút chuyển sang **EN**. Phím `Esc` quay lại.
- Sidebar có các nhóm: *Không gian làm việc* (Công việc, Sự cố, Phê duyệt), *Tổ chức* (Tổ chức, Quy trình), *Tài nguyên* (Thư viện, Dữ liệu nghiệp vụ), *Quản trị* (Vận hành, Cài đặt).
- Sản phẩm **đã có hệ thống chọn màu nhấn với 6 tùy chọn**: *Midnight blue, Ocean teal, Quiet indigo, Forest green, Warm copper, Graphite*, cùng chế độ sáng/tối. Nghĩa là thực tế có **12 tổ hợp** (6 màu nhấn × 2 chế độ) và **thiết kế mới phải đẹp, đủ tương phản ở cả 12 tổ hợp**.
- Trang Công việc là trang phức tạp nhất và là dữ liệu dày: **1.188 task**. Mỗi task có: tiêu đề, mã task (kiểu `tsk_01m49qy02cte3jhgz2kez3eje1`), loại (`analysis`, `coordination`...), trạng thái (`Thất bại`...), agent đang giữ (`HR Agent`, `Executive Agent`), số việc con (`21 việc con`), lý do bị từ chối (đôi khi là thông báo lỗi rất dài của nhà cung cấp model), và người phụ trách (`bạn`).
- Bốn chỉ số đầu trang: **Chờ bạn** (351), **Agent đang làm** (0), **Đã xong** (837), **Tổng** (1.188).

### Hồ sơ người dùng và cách dùng

Đây là công cụ vận hành dùng hằng ngày, thường trên desktop, dữ liệu dày, cần quét nhanh và ra quyết định (phê duyệt, giao lại, xem lỗi). **Tốc độ quét mắt và độ rõ của trạng thái quan trọng hơn vẻ đẹp tĩnh.** Vẻ đẹp phải phục vụ khả năng đọc.

---

## 2. Quyết định đã chốt (không hỏi lại)

| Hạng mục | Quyết định |
|---|---|
| Phạm vi | **Toàn bộ app**, mọi trang dùng chung một design system |
| Mức can thiệp | Được **refactor tự do** CSS, markup và JS render |
| Hướng thẩm mỹ | Tối giản chính xác kiểu Linear / Vercel |
| Chế độ sáng/tối | **Ngang hàng.** Mỗi chế độ được thiết kế riêng, **không đảo màu** từ chế độ kia |
| Màu nhấn | Giữ nguyên hệ **6 chủ đề màu hiện có**; thiết kế nền trung tính tiết chế, màu nhấn chỉ dùng cho hành động chính và trạng thái chọn |
| Mật độ | Người dùng chuyển được **Compact / Comfortable** (mặc định lần đầu: Comfortable; nhớ lựa chọn) |
| Chuyển động | **Vừa phải đến phong phú**, nhưng có kỷ luật (xem mục 7) |
| Tính năng UX mới | **Được phép thêm tất cả** (command palette, phím tắt, toast, skeleton, tooltip, bộ lọc nâng cao...) |
| Thiết bị | **Desktop, tablet và đầy đủ mobile** |
| Asset | Mọi font/icon/thư viện đều **self-host, tải về lưu trong repo**. **Tuyệt đối không gọi CDN hay dịch vụ ngoài khi chạy** |
| Công cụ kiểm tra | Có sẵn **Playwright/Chromium** để chụp screenshot |
| Ngôn ngữ nội dung UI | Tiếng Việt (giữ nguyên i18n hiện có, bổ sung bản EN cho chuỗi mới) |

---

## 3. Ràng buộc bất biến (không được phá)

1. **Không đổi hành vi nghiệp vụ.** Không sửa logic backend, API contract, DB, schema. Chỉ được sửa backend nếu cần để phục vụ file tĩnh mới (font, icon) và phải ghi rõ trong báo cáo.
2. **Giữ nguyên hash routes** (`#/give/<task_id>` và mọi route hiện có), các tham số query (`?org=`), và liên kết sâu.
3. **Giữ nguyên hệ i18n và nút VI/EN.** Không xóa key hiện có. Chuỗi mới phải có cả VI và EN.
4. **Giữ phím tắt hiện có** (`Esc` quay lại) và các hành vi đã có.
5. **Trước khi đổi tên hoặc xóa bất kỳ class/id/data-attribute nào**, `grep` toàn repo (JS, test, script, tài liệu) xem có chỗ nào phụ thuộc không. Nếu có, cập nhật đồng bộ hoặc giữ alias.
6. **Giữ khóa lưu tùy chọn người dùng hiện có** (chủ đề, màu nhấn, ngôn ngữ trong localStorage hoặc cookie) để không làm mất cài đặt của người đang dùng. Nếu cần đổi cấu trúc, viết migration một lần, không phá dữ liệu cũ.
7. **Không có yêu cầu mạng ra ngoài khi chạy.** Không `@import` từ Google Fonts, không script CDN, không ảnh remote, không analytics. Kiểm tra bằng Playwright (mục 10).
8. **Không bịa dữ liệu.** Không hiển thị sparkline, phần trăm thay đổi, "xu hướng" hay avatar giả nếu API không trả dữ liệu thật. Nếu muốn có, ghi vào báo cáo như một đề xuất cho backend.
9. **Không dùng lệnh phá hủy** (`git reset --hard`, `git clean -fd`, xóa file ngoài phạm vi UI). Chạy `git status` đầu tiên; nếu working tree không sạch, dừng và ghi chú, không ghi đè thay đổi của người khác.
10. Tuân thủ **giấy phép** của font và icon. Chỉ dùng loại cho phép phân phối lại (OFL, MIT, ISC, Apache-2.0). Ghi vào `THIRD_PARTY_NOTICES.md`.

---

## 4. Cách làm việc (quan trọng)

- Làm trên nhánh riêng: `git switch -c ui/refresh`. **Commit nhỏ, theo từng giai đoạn**, thông điệp rõ ràng. Ứng dụng phải **chạy được sau mỗi commit**, không làm kiểu viết lại một lần.
- Giữ **sổ tiến độ** tại `docs/ui-refresh/PROGRESS.md`: giai đoạn đã xong, đang làm, bước tiếp theo, quyết định đã đưa ra và lý do, vấn đề còn tồn. Cập nhật sau mỗi giai đoạn để một phiên mới có thể tiếp tục đúng chỗ.
- Bạn **không thể hỏi lại người dùng giữa chừng**. Khi gặp điểm mơ hồ, chọn phương án an toàn nhất, **ghi giả định vào PROGRESS.md**, và đi tiếp. Chỉ dừng khi gặp rủi ro mất dữ liệu hoặc phá hợp đồng ở mục 3.
- **Không thêm bước build nếu không cần.** Mặc định giữ **zero-build**: ES modules gốc, CSS tách file. Chỉ thêm công cụ build khi có lý do mạnh và ghi rõ vì sao. Công cụ phát triển (Playwright, axe-core) được cài làm devDependency, nhưng sản phẩm khi chạy không phụ thuộc chúng.
- Chỉ nhắm trình duyệt hiện đại (Chrome/Edge/Firefox/Safari bản mới). Xác minh yêu cầu trình duyệt trong tài liệu repo; nếu không có, giả định "2 phiên bản ổn định gần nhất". Được dùng CSS hiện đại (`@layer`, nesting, `:has()`, container queries, `oklch()`, `color-mix()`, `@property`, `linear()` easing), có fallback hợp lý khi cần.

---

## 5. Giai đoạn 0: Khảo sát (CHỈ ĐỌC, chưa sửa code)

Mục tiêu: hiểu rõ hiện trạng trước khi thiết kế. Tạo `docs/ui-refresh/00-audit.md` gồm:

1. **Bản đồ file UI**: file HTML/CSS/JS nào, kích thước, cách chúng được nạp, cách backend phục vụ. Cách routing hoạt động. Cách render danh sách.
2. **Cơ chế chủ đề hiện có**: biến CSS nào, thuộc tính/class nào bật chế độ sáng/tối và màu nhấn, lưu ở đâu, 6 màu nhấn được định nghĩa thế nào.
3. **Kiểm kê trang và route**: liệt kê *mọi* trang/màn hình (kể cả màn chi tiết như `give/<task_id>`, hộp thoại, drawer) và chụp ảnh từng cái.
4. **Kiểm kê thành phần lặp lại**: nút, ô nhập, tab, thẻ, bảng/hàng danh sách, badge, modal, menu, form, v.v. Đánh dấu chỗ nào đang bị lặp CSS hoặc không nhất quán.
5. **Nợ kỹ thuật CSS**: màu/khoảng cách hard-code, `!important`, selector quá cụ thể, CSS chết, z-index tùy tiện.
6. **Danh sách đầy đủ các trạng thái** (status) và loại (type) mà dữ liệu có thể có, lấy từ code và API, để thiết kế hệ badge không bỏ sót.
7. **Cách xử lý 1.188 hàng**: có ảo hóa/phân trang không, hiệu năng cuộn hiện tại.
8. **Lệnh chạy server, lệnh chạy test, lệnh lint**. Chạy test hiện có để lấy mốc "xanh" ban đầu.
9. **Ảnh baseline** (xem ma trận ở mục 10) lưu vào `artifacts/ui-shots/00-baseline/`.

### Các vấn đề đã quan sát được ở trang Công việc (phải giải quyết, kiểm tra thêm ở các trang khác)

- **Thiếu phân cấp thị giác**: tiêu đề, mã task, loại, trạng thái, người giữ và lý do lỗi gần như cùng trọng lượng; mắt không biết nhìn đâu trước.
- **Trạng thái chỉ là chữ thường** nằm trong một chuỗi nối bằng dấu `·` (`analysis · Thất bại · HR Agent giữ`). Chưa có badge, icon hay màu ngữ nghĩa.
- Chữ **"bạn" nằm lẻ loi bên phải**, không có ngữ cảnh.
- **Lý do lỗi** là đoạn chữ dài, lẫn tiền tố tên model; không rút gọn, không mở rộng, không sao chép được.
- **Icon sidebar** chỉ là ký tự (`○ ✓ →`), không đồng bộ; **viền focus** của mục sidebar đang chọn bị kép/dày/lệch.
- **Nhãn in hoa tracking rộng** ở nhiều nơi (`KHÔNG GIAN LÀM VIỆC`, `CHỜ BẠN`, `O-NEXUS`) và **chuỗi meta nối bằng dấu chấm giữa**: đúng kiểu "dấu hiệu template"; cần thay bằng cấu trúc có ý nghĩa.
- **Bốn thẻ KPI giống hệt nhau**: con số quan trọng nhất ("Chờ bạn") không nổi hơn; giá trị `0` trông như lỗi.
- **Banner "Môi trường cục bộ · chưa bật xác thực"** nặng nề, có mũi tên `▶` nhưng không rõ có mở rộng được không.
- Danh sách nằm trong **thanh cuộn lồng** (cuộn trong thẻ), gây khó chịu, đặc biệt trên mobile.
- Chưa có **độ sâu/elevation** rõ ràng, trạng thái hover/active/selected, empty/loading/error state.
- Chế độ sáng trông như **đảo màu của chế độ tối** hơn là được thiết kế riêng.

> Sau khi xong khảo sát, viết **kế hoạch thiết kế ngắn** (`docs/ui-refresh/01-design-plan.md`): bảng token đề xuất, chọn font (kèm bằng chứng đã thử chữ Việt), kiến trúc CSS, danh sách component, thứ tự triển khai. Rồi **tự phản biện kế hoạch**: có chỗ nào chỉ là "mặc định cho mọi dashboard"? Sửa lại và ghi những gì đã đổi, rồi mới bắt đầu giai đoạn 1.

---

## 6. Hệ thống thiết kế (đặc tả)

### 6.1 Kiến trúc CSS

- Chia file theo `@layer`: `reset, tokens, base, layout, components, pages, utilities`. **Không dùng `!important`.** Thứ tự layer quyết định ưu tiên, tránh "cuộc chiến specificity".
- Token 3 tầng: **primitive** (thang màu thô) → **semantic** (`--color-bg`, `--color-surface-1`, `--color-text-muted`, `--color-border-subtle`, `--color-accent`, `--color-danger-soft`...) → **component** (`--btn-height`, `--row-height`...). **Component chỉ được dùng token semantic.**
- Đặt tên component nhất quán (ví dụ tiền tố `ui-`, dùng thuộc tính `data-*` cho biến thể và trạng thái). Một component = một file CSS ngắn + (nếu cần) một module JS.
- Thuộc tính trên `<html>` (giữ tên hiện có nếu đã có, chỉ dưới đây là ví dụ):
  ```html
  <html lang="vi" data-mode="dark|light" data-accent="midnight|ocean|indigo|forest|copper|graphite" data-density="comfortable|compact">
  ```
- Chống **nhấp nháy sai chủ đề**: script nhỏ inline trong `<head>` đặt thuộc tính *trước khi vẽ*. Đặt `color-scheme`, `<meta name="theme-color">`. Tắt transition lúc tải trang đầu và khi đổi chủ đề để tránh "chớp".

### 6.2 Màu sắc

- **Mỗi chế độ có bộ nền trung tính riêng**, thiết kế bằng mắt chứ không đảo số:
  - *Tối*: không dùng đen thuần. Nền nhiều lớp (`bg`, `surface-1`, `surface-2`, `surface-3`, `overlay`) khác nhau vài bậc sáng; độ sâu thể hiện bằng **độ sáng bề mặt + viền alpha thấp**, không dựa vào bóng đổ.
  - *Sáng*: nền hơi ngả xám/ấm nhẹ, bề mặt trắng, **viền mảnh 1px và bóng nhiều lớp rất nhẹ**. Không dùng xám phẳng nhạt nhòa.
- **Nền trung tính pha sắc rất nhẹ theo chủ đề** (chroma 1 đến 3%): Midnight blue ngả xanh lạnh, Warm copper ngả ấm, Graphite hoàn toàn trung tính. Màu nhấn của mỗi chủ đề khai báo đủ: `accent`, `accent-hover`, `accent-soft` (nền nhạt), `accent-border`, `on-accent` (màu chữ trên nền nhấn) **cho cả sáng và tối**.
- **Graphite** là chủ đề đơn sắc: nút chính dùng tương phản trung tính cao (trắng trên nền tối, gần đen trên nền sáng); các trạng thái chọn dùng nhấn mạnh trung tính, không "đỏ lên" vì thiếu màu.
- **Màu trạng thái độc lập với màu nhấn**: `success, warning, danger, info, neutral/pending`. Lý do: ở chủ đề *Forest green*, "Thất bại" không được trông giống "Đã xong". Mỗi trạng thái có `fg`, `bg-soft`, `border`. **Không dùng màu làm kênh duy nhất**: luôn đi kèm icon hoặc chữ.
- Màu nhấn là **gia vị**: dùng cho hành động chính, mục đang chọn, focus ring, liên kết. Không tô màu nhấn lên mọi viền, tiêu đề, icon.
- **Tương phản (bắt buộc kiểm bằng script cho cả 12 tổ hợp):** chữ thường ≥ 4.5:1; chữ lớn và thành phần UI (viền input, icon có nghĩa, focus ring) ≥ 3:1; chữ trên nút nhấn ≥ 4.5:1.

### 6.3 Typography

- **Chọn font bằng bằng chứng, không bằng thói quen**. Yêu cầu bắt buộc: **hỗ trợ đầy đủ tiếng Việt** (dấu chồng như `ệ ữ ặ ẫ ỗ ử`), có bản variable hoặc nhiều weight, giấy phép tự do. Ứng viên cần so sánh: **Inter**, **Geist**, **Be Vietnam Pro** (và font mono: **JetBrains Mono**, **Geist Mono**, **IBM Plex Mono**). Tải về, chụp ảnh thử với các chuỗi thật: `Tuyển kỹ sư MEP đến onboarding`, `Đề xuất lựa chọn sau hai vòng`, `Phê duyệt`, rồi chọn. Ghi lý do vào kế hoạch thiết kế. Chỉ dùng **tối đa 2 họ chữ**: một sans cho giao diện, một mono cho mã/ID/log.
- Self-host định dạng `woff2`, có `font-display: swap`, subset `latin` + `latin-ext` + `vietnamese`, preload font chính. Metric fallback (`size-adjust`...) để tránh nhảy layout.
- **Thang chữ** (điều chỉnh sau khi thử): 12 / 13 / 14 (nội dung mặc định) / 16 / 20 / 24 / 32. Chỉ 3 weight: 400, 500, 600. Chữ lớn có `letter-spacing` hơi âm; chữ nhỏ không. **Số liệu luôn `font-variant-numeric: tabular-nums`.**
- **Đừng dùng nhãn in hoa tracking rộng làm "lớp hoàn thiện"**. Nhãn nhóm dùng *sentence case*, cỡ nhỏ, màu muted. Không thêm nhãn nhỏ phía trên mỗi tiêu đề nếu nội dung không cần.
- **Line-height tiếng Việt**: thân bài ≥ 1.5, tiêu đề ≥ 1.25. **Kiểm tra dấu bị cắt** (clipping) ở mọi chỗ có `overflow:hidden`/`line-clamp`.
- Độ dài dòng văn bản < 80 ký tự. Rút gọn bằng `text-overflow`/`line-clamp` kèm tooltip hoặc nút mở rộng, không bao giờ cắt mà không có cách xem đầy đủ.

### 6.4 Khoảng cách, bo góc, độ sâu

- Lưới **4px**; thang khoảng cách `2, 4, 8, 12, 16, 20, 24, 32, 40, 56`.
- **Bo góc theo thứ bậc, không dùng một giá trị cho tất cả**: ví dụ điều khiển nhỏ 6px, ô nhập/nút 8px, panel 12px, popover/dialog 14px, avatar tròn. Ghi quy tắc vào kế hoạch.
- **Viền** `1px` là công cụ chính để chia lớp; vùng chia cắt dùng đường kẻ mảnh thay cho khối nền.
- **Focus ring** thống nhất toàn app: 2px ring + offset, dùng màu nhấn nhưng đủ tương phản trên mọi nền; chỉ hiện với `:focus-visible`. Sửa lỗi viền kép ở mục sidebar đang chọn.
- Hạn chế thẻ bo tròn giống hệt nhau xếp thành lưới. **Ưu tiên cấu trúc bằng đường kẻ, vùng và khoảng trắng**; chỉ dùng "thẻ" khi nội dung thật sự là một đối tượng độc lập.

### 6.5 Mật độ

- `data-density="compact|comfortable"` điều khiển **token**, không viết hai bộ CSS. Ví dụ: chiều cao hàng danh sách compact ≈ 36px / comfortable ≈ 56px; chiều cao nút, padding ô, cỡ chữ phụ cũng đi theo token.
- Công tắc mật độ đặt ở thanh trên hoặc menu hiển thị, **nhớ lựa chọn**, đổi có chuyển động ngắn (không giật layout).

### 6.6 Biểu tượng

- Một bộ icon duy nhất, nét đồng nhất (khuyến nghị **Lucide**, giấy phép ISC), đóng gói thành **SVG sprite inline** hoặc module, self-host. Lưới 16/20px, stroke ~1.5px, `currentColor`.
- Thay thế **toàn bộ** icon dạng ký tự. Icon-only button luôn có `aria-label` và tooltip. Icon trạng thái có hình dạng khác nhau (không chỉ khác màu).

---

## 7. Chuyển động (vừa phải đến phong phú, có kỷ luật)

**Nguyên tắc:** chuyển động để **giải thích sự thay đổi** (cái gì vừa mở, đóng, chuyển, xác nhận), không để trang trí. Chỉ **một khoảnh khắc đặc trưng** được phép "phô diễn"; phần còn lại phải êm và gần như vô hình.

- **Token chuyển động:** thời lượng `instant 80ms / fast 140ms / base 200ms / slow 320ms / emphasized 480ms`; easing chuẩn `cubic-bezier(0.2, 0, 0, 1)`, easing vào/ra, và một đường spring nhẹ (dùng `linear()`).
- **Tập hợp bắt buộc:**
  - Hover/press/focus trên mọi điều khiển (đổi nền/viền, `transform` tối đa 1px hoặc `scale(0.98)` khi nhấn).
  - **Chỉ báo trượt** (sliding indicator) cho mục sidebar đang chọn và cho tab lọc (Tất cả / Chờ tôi / Agent đang làm / Đã xong).
  - Mở/đóng panel (ví dụ "Giao việc mới") bằng animation chiều cao mượt (kỹ thuật `grid-template-rows: 0fr → 1fr`).
  - Menu, popover, dialog, drawer, toast: vào/ra có opacity + dịch chuyển nhỏ (4 đến 8px) + scale rất nhẹ; dialog trên mobile là **bottom sheet**.
  - **Chuyển trang**: fade + dịch chuyển ≤ 6px trong ~160ms. Dùng **View Transitions API** nếu có, kèm fallback.
  - Skeleton loading cho danh sách và chỉ số (shimmer rất nhẹ).
  - Con số trong chỉ số **đếm lên khi tải lần đầu** (≤ 600ms, `tabular-nums`, không đếm lại khi làm mới dữ liệu nền).
  - Chấm "trực tiếp" ở chân sidebar có nhịp thở rất nhẹ (chỉ khi kết nối thật sự hoạt động).
- **Khoảnh khắc đặc trưng (chọn một):** lần đầu vào trang Công việc, dải chỉ số hiện lên có thứ tự và danh sách "xếp" vào chỗ theo lớp ngắn (tổng ≤ 400ms, chỉ **12 hàng đầu**, chỉ chạy một lần mỗi phiên). Mọi thứ còn lại tĩnh lặng.
- **Cấm:** animation vòng lặp trang trí; hover nảy trên mọi thẻ; hiệu ứng so le trên hàng loạt phần tử; làm động 1.188 hàng; chỉ animate `transform` và `opacity` (không animate `width/height/top/left` trừ khi qua kỹ thuật an toàn).
- **`prefers-reduced-motion: reduce`**: bỏ dịch chuyển/scale/đếm số/shimmer, chỉ giữ fade ≤ 100ms hoặc thay đổi tức thì. Phải có kiểm thử tự động cho điều này.

---

## 8. Đặc tả thành phần và trang

Mọi thành phần phải có **đủ trạng thái**: mặc định, hover, active/pressed, `focus-visible`, disabled, loading, error, selected, và (với danh sách) empty, no-results.

### 8.1 Bộ thành phần nền tảng (làm trước khi đụng đến trang)

Button (primary / secondary / ghost / danger; kích thước sm/md/lg; có icon; loading), IconButton, Input, Select, Textarea, Checkbox, Radio, Switch, SegmentedControl/Tabs (có chỉ báo trượt), Badge/StatusPill, Avatar (chữ cái đầu, dùng cho agent/người), Panel/Section, ListRow/TableRow, Toolbar, Dialog, Drawer/BottomSheet, Popover/Menu, Tooltip, Toast, Skeleton, EmptyState, Kbd (hiển thị phím tắt), Callout/Banner, Breadcrumb/BackButton, CodeBlock/LogViewer (có nút sao chép, cuộn ngang, dòng dài xuống hàng tùy chọn), CopyableId (mã rút gọn ở giữa, bấm để sao chép, toast xác nhận).

### 8.2 Khung ứng dụng (App shell)

- **Sidebar**: thu gọn được bằng animation mượt, trạng thái chỉ-icon có tooltip. Mục đang chọn: nền nhẹ + chỉ báo trượt 2px màu nhấn, **không viền kép**. Nhãn nhóm sentence case, muted. Khối tên tổ chức ở đầu (*Orchestrator / O-Nexus*) thành một **workspace switcher** đúng nghĩa (kể cả khi chỉ có một mục). Chân sidebar: trạng thái `trực tiếp` + phiên bản schema ở cỡ nhỏ, mono, muted.
- **Thanh trên**: nút quay lại (kiểu ghost, hiển thị gợi ý phím `Esc`), breadcrumb, chuông thông báo có chấm chưa đọc, công tắc **VI/EN dạng segmented**, menu giao diện (chế độ sáng/tối/hệ thống, **chọn 6 màu nhấn có xem trước**, mật độ). Thanh trên dính (sticky), phân tách bằng đường kẻ mảnh.
- **Banner "Môi trường cục bộ · chưa bật xác thực"**: thành callout mỏng, có icon cảnh báo, **mở rộng được** để xem chi tiết/khuyến nghị bật xác thực. Có thể thu gọn trong phiên nhưng **không ẩn vĩnh viễn** (đây là cảnh báo bảo mật).

### 8.3 Trang Công việc (trang chủ lực, làm kỹ nhất)

**Tiêu đề trang**: tiêu đề + mô tả ngắn; nút chính **"Giao việc mới"** với icon và gợi ý phím tắt.

**Dải chỉ số (4 chỉ số)**: thay bốn thẻ giống nhau bằng **một dải thống nhất có đường kẻ dọc phân cách** (hoặc cấu trúc khác phù hợp hơn, nêu lý do). Quy tắc:
- *Chờ bạn* có **trọng lượng thị giác cao nhất** (đây là điều cần hành động); các chỉ số còn lại yên tĩnh hơn.
- Mỗi chỉ số là **bộ lọc nhanh có thể bấm** (bấm "Chờ bạn" sẽ chọn tab lọc tương ứng), có trạng thái hover/selected rõ.
- Con số cỡ lớn, `tabular-nums`; nhãn sentence case; chú thích phụ muted. Giá trị `0` hiển thị **tĩnh và rõ ràng** (muted nhưng có chủ ý), không như dữ liệu hỏng.

**Panel "Giao việc mới"**: disclosure mượt; form đúng chuẩn (nhãn, gợi ý, validate inline, trạng thái loading khi gửi, toast khi thành công/thất bại).

**Thanh công cụ danh sách**: ô tìm kiếm có icon, nút xóa nhanh, gợi ý phím `/` để focus; tab lọc là SegmentedControl **kèm số lượng từng tab**; bổ sung **sắp xếp** và (tùy chọn) **nhóm theo trạng thái**; hiển thị số kết quả (`1.188 / 1.188`) theo định dạng số Việt Nam.

**Hàng task (thành phần quan trọng nhất).** Giải phẫu đề xuất, điều chỉnh nếu có phương án tốt hơn:

```
[icon trạng thái] Tiêu đề task (1 dòng, rút gọn + tooltip)            [Người giữ: avatar + tên]  [bạn/Chờ bạn]
                  mã-task-rút-gọn (mono, bấm để chép) · loại · 21 việc con        thời gian tương đối
                  ⚠ Lý do bị từ chối (1 dòng, có thể mở rộng, có nút chép)
```
- **Trạng thái** là `StatusPill` (icon + chữ, màu ngữ nghĩa). Phủ **mọi** trạng thái tìm được ở giai đoạn 0.
- Dùng **cột/vùng có cấu trúc** thay cho chuỗi nối bằng dấu chấm. Loại task là chip trung tính nhỏ; số việc con có icon cây.
- **Lý do lỗi** mặc định 1 dòng, tông danger nhẹ; có thể mở rộng để thấy đầy đủ trong `CodeBlock` mono; nút sao chép. Thông báo lỗi dài của nhà cung cấp được trình bày dễ đọc, không tràn bố cục.
- Chữ **"bạn"** trở thành nhãn có nghĩa (ví dụ chip "Chờ bạn" có icon), căn nhất quán.
- Hover: nền nhẹ + hiện **hành động nhanh** (mở, sao chép mã, giao lại... chỉ những gì sản phẩm thực sự hỗ trợ). Selected/keyboard-focus rõ ràng. Phím `j/k` di chuyển, `Enter` mở, `/` tìm kiếm.
- Hiệu năng: **ảo hóa danh sách** hoặc dùng `content-visibility: auto` + `contain-intrinsic-size`, hoặc phân trang/"tải thêm"; phải mượt với 1.188 hàng (và đủ cho vài nghìn). **Bỏ thanh cuộn lồng**: để trang cuộn, thanh công cụ dính ở trên.
- Có đủ: skeleton khi tải, empty ("Chưa có công việc nào" kèm lời mời hành động), no-results (kèm nút xóa bộ lọc), error (nêu rõ chuyện gì xảy ra và cách xử lý).

### 8.4 Các trang còn lại

Với *Sự cố, Phê duyệt, Tổ chức, Quy trình, Thư viện, Dữ liệu nghiệp vụ, Vận hành, Cài đặt* và màn chi tiết (`give/<task_id>`...): **mở từng trang, dựng lại bằng chính các thành phần nền tảng**, không tạo CSS riêng lẻ cho từng trang trừ khi thật cần. Mỗi trang phải trông thuộc cùng một sản phẩm: cùng tiêu đề trang, cùng ngôn ngữ bảng/danh sách, cùng form, cùng empty state.
- **Cài đặt**: thay phần chọn màu hiện có bằng bộ chọn giao diện đẹp, có **xem trước trực tiếp** 6 màu nhấn × sáng/tối/hệ thống, cộng công tắc mật độ.
- **Màn chi tiết task/kết quả/nhật ký**: bố cục rõ vùng (tóm tắt, tiến trình, kết quả, nhật ký), nhật ký dùng LogViewer mono có tìm kiếm và sao chép.
- **Phê duyệt**: hành động duyệt/từ chối là điểm nhấn của trang; xác nhận bằng dialog có tóm tắt hệ quả, và toast kết quả.

### 8.5 Bảng lệnh và phím tắt

- **Command palette** (`Ctrl/⌘ + K`): điều hướng trang, tìm task theo tên/mã/người giữ, thao tác nhanh (Giao việc mới, đổi chế độ sáng/tối, đổi màu nhấn, đổi mật độ, đổi ngôn ngữ). Hộp thoại truy cập được (role `dialog`, combobox/listbox, `aria-activedescendant`, bẫy focus, `Esc` đóng, trả focus về chỗ cũ).
- **Màn hình trợ giúp phím tắt** (`?`). Tắt phím tắt khi đang gõ trong ô nhập.

---

## 9. Nội dung chữ (copy)

- Viết từ góc nhìn người dùng, động từ rõ ràng, *sentence case*, không hoa mỹ. Nút nói đúng điều sẽ xảy ra ("Lưu thay đổi", không "Gửi"). Một hành động giữ **một tên** xuyên suốt (nút "Giao việc" thì toast là "Đã giao việc").
- **Lỗi và trạng thái rỗng là lúc để chỉ dẫn**: nói chuyện gì xảy ra và làm gì tiếp. Không xin lỗi dài dòng, không mơ hồ.
- Thời gian tương đối bằng `Intl.RelativeTimeFormat('vi')`, số bằng `Intl.NumberFormat('vi-VN')`. Cập nhật `lang` của `<html>` khi đổi ngôn ngữ.
- **Giữ thuật ngữ nghiệp vụ hiện có** (Công việc, Sự cố, Phê duyệt, Quy trình...) trừ khi có lý do rõ ràng; ghi mọi thay đổi vào báo cáo.

---

## 10. Responsive đầy đủ (kể cả mobile)

- Breakpoint tham chiếu: `360, 640, 768, 1024, 1280, 1536`. Ưu tiên **container queries** cho thành phần.
- **Mobile**: sidebar thành **drawer off-canvas** (hoặc thanh điều hướng dưới nếu hợp lý, nêu lý do); dải chỉ số thành lưới 2×2 hoặc cuộn ngang có snap; **hàng task chuyển thành bố cục 2 đến 3 dòng** (trạng thái + tiêu đề, rồi meta, rồi lỗi); bảng chuyển thành danh sách xếp chồng; dialog là **bottom sheet**; command palette toàn màn hình.
- Mục tiêu chạm ≥ **44px** trên thiết bị cảm ứng, ≥ 24px trên desktop (WCAG 2.2). Tôn trọng `env(safe-area-inset-*)`. **Không có cuộn ngang ở 360px.** Nhập liệu không làm trình duyệt tự phóng to (cỡ chữ input ≥ 16px trên mobile).
- Zoom 200% và chữ lớn không vỡ bố cục.

---

## 11. Truy cập (Accessibility, WCAG 2.2 AA)

- Điều hướng bàn phím hoàn chỉnh, thứ tự focus hợp lý, `:focus-visible` rõ, không bẫy focus ngoài dialog.
- ARIA đúng chỗ: landmark (`nav`, `main`, `header`), `aria-current="page"`, tab/tablist, `aria-live` cho toast và cập nhật số liệu, `aria-sort`, `aria-expanded`, `aria-busy` khi tải.
- Không chỉ dùng màu để truyền nghĩa. Hỗ trợ `forced-colors` ở mức hợp lý. Có "skip to content".
- Kiểm tra bằng **axe-core** (cài làm devDependency, chạy qua Playwright); **không còn vi phạm mức serious/critical**.

---

## 12. Triển khai theo giai đoạn (mỗi giai đoạn có cổng)

| GĐ | Nội dung | Cổng để qua giai đoạn sau |
|---|---|---|
| 0 | Khảo sát + baseline + kế hoạch thiết kế (mục 5) | Có `00-audit.md`, `01-design-plan.md`, ảnh baseline, test hiện có đã chạy |
| 1 | **Nền tảng**: `@layer`, tokens (12 tổ hợp), font, icon sprite, mật độ, token chuyển động, script chống nhấp nháy, di chuyển cài đặt cũ | App vẫn chạy; script tương phản đạt cho cả 12 tổ hợp |
| 2 | **Thành phần nền tảng** + trang **Style guide** ẩn (ví dụ `#/_styleguide`, chỉ bật ở môi trường dev/cục bộ) hiển thị mọi thành phần × 12 tổ hợp × 2 mật độ | Đủ trạng thái cho mọi thành phần; axe sạch trên style guide |
| 3 | **Khung ứng dụng**: sidebar, thanh trên, banner, workspace switcher, menu giao diện | Ảnh so sánh trước/sau; điều hướng bàn phím ổn |
| 4 | **Trang Công việc** + màn chi tiết + panel "Giao việc mới" + hiệu năng danh sách | Cuộn mượt với 1.188 hàng; đủ empty/loading/error |
| 5 | **Từng trang còn lại** (một trang một commit) | Mỗi trang dùng lại thành phần chung, không CSS lẻ |
| 6 | **Command palette, phím tắt, toast, chuyển động**, tinh chỉnh chuyển động; **responsive/mobile**; a11y | Ma trận screenshot đầy đủ; reduced-motion đạt |
| 7 | **Đánh bóng và tự phản biện**, dọn CSS chết, tài liệu, báo cáo cuối | Toàn bộ Định nghĩa Hoàn thành (mục 14) |

**Quy tắc tự phản biện sau mỗi giai đoạn:** xem lại screenshot, rồi hỏi: *Có chỗ nào trông như "mặc định của mọi dashboard"? Có chi tiết nào thừa?* (Quy tắc Chanel: trước khi ra khỏi nhà, nhìn gương và **bỏ bớt một phụ kiện**.) Ghi những gì đã bỏ/sửa vào PROGRESS.md. **Tiêu điểm táo bạo chỉ dành cho một nơi**; mọi thứ xung quanh phải yên tĩnh.

---

## 13. Kiểm chứng (tự động, bằng Playwright)

Tạo `scripts/ui-shots.mjs` (hoặc `.py` nếu repo dùng Python) và các script kiểm tra. Lưu ảnh vào `artifacts/ui-shots/<giai-đoạn>/`.

**Ma trận chụp ảnh:**
- *Mọi trang* × {sáng, tối} × màu nhấn mặc định × {desktop 1440×900, mobile 390×844}.
- *Trang Công việc và style guide* × **cả 12 tổ hợp** (6 màu nhấn × sáng/tối) × {compact, comfortable}.
- *Trang Công việc* thêm: tablet 768×1024, 360×800, 1920×1080.
- *Trạng thái đặc biệt* bằng `page.route` giả lập API (**không động vào dữ liệu thật**): loading, empty, no-results, error, task có lỗi rất dài, tiêu đề rất dài, `0` ở mọi chỉ số.

**Kiểm tra tự động bắt buộc (script phải thất bại khi vi phạm):**
1. **Không có yêu cầu mạng tới host nào ngoài `127.0.0.1`/`localhost`** (lắng nghe `page.on('request')`).
2. **Không có lỗi/cảnh báo console.**
3. **Tương phản** tính từ `getComputedStyle` cho cặp chữ/nền chính ở cả 12 tổ hợp (ngưỡng ở mục 6.2).
4. **Không cuộn ngang** ở 360px (so `scrollWidth` với `clientWidth` của `document`).
5. **Không cắt dấu tiếng Việt**: với các phần tử có `overflow:hidden`, kiểm `scrollHeight` so với `clientHeight` trên chuỗi thử có dấu chồng.
6. **axe-core**: không có vi phạm serious/critical.
7. **Reduced-motion**: với `emulateMedia({ reducedMotion: 'reduce' })`, không còn `transform` animation đang chạy.
8. **Hiệu năng danh sách**: đo thời gian dựng ban đầu và số *long task* khi cuộn qua 1.188 hàng; ghi vào báo cáo (mốc trước/sau).
9. **Test hiện có của dự án** vẫn xanh; lint (nếu có) sạch.

---

## 14. Định nghĩa Hoàn thành

- [ ] Mọi trang và màn con dùng chung hệ thống thiết kế; không còn màu/khoảng cách hard-code ngoài token.
- [ ] 12 tổ hợp chủ đề đều đẹp và đạt tương phản; sáng và tối có "tính cách" riêng, không phải đảo màu.
- [ ] Trang Công việc: phân cấp rõ, trạng thái dạng badge, lỗi rút gọn/mở rộng/chép được, dải chỉ số có thứ bậc, cuộn mượt 1.188 hàng, không còn thanh cuộn lồng.
- [ ] Compact/Comfortable hoạt động và được nhớ.
- [ ] Command palette, phím tắt, toast, skeleton, tooltip, empty/error state hoạt động và truy cập được.
- [ ] Chuyển động đạt mục 7, có reduced-motion; đúng **một** khoảnh khắc đặc trưng.
- [ ] Responsive 360px đến 1920px; mobile dùng drawer/bottom sheet; không cuộn ngang.
- [ ] axe sạch, điều hướng bàn phím đầy đủ.
- [ ] Không có yêu cầu mạng ra ngoài; font/icon self-host; `THIRD_PARTY_NOTICES.md` đầy đủ.
- [ ] Hash routes, i18n, phím `Esc`, khóa cài đặt cũ, API contract đều nguyên vẹn; test hiện có xanh.
- [ ] CSS chết đã dọn; không còn `!important` mới; có tài liệu ngắn về cách thêm thành phần/token mới (`docs/ui-refresh/README.md`).

---

## 15. Báo cáo cuối (`docs/ui-refresh/FINAL-REPORT.md`)

1. Tóm tắt thay đổi theo giai đoạn (ngắn gọn, đọc được trong 2 phút).
2. **Ảnh trước/sau** của các màn chính (sáng và tối, desktop và mobile).
3. Quyết định thiết kế chính và lý do (font, thang màu, quy tắc bo góc, khoảnh khắc đặc trưng).
4. Danh sách file đã thêm/sửa/xóa; chỉ rõ **nếu có sửa backend**.
5. Kết quả các kiểm tra ở mục 13 (kèm số liệu hiệu năng trước/sau).
6. Giả định đã đưa ra, vấn đề tồn đọng, đề xuất tiếp theo (kể cả dữ liệu backend cần bổ sung để có sparkline/xu hướng).
7. Cách chạy lại bộ screenshot và kiểm tra; cách hoàn tác (`git revert`/bỏ nhánh `ui/refresh`).

---

**Bắt đầu ngay bây giờ với Giai đoạn 0.** Không sửa code sản phẩm cho đến khi `00-audit.md` và `01-design-plan.md` hoàn tất.
