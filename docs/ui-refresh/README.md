# UI refresh

Đây là hồ sơ triển khai theo `ui-refresh-brief.md`. Giai đoạn 0 khảo sát source,
chụp giao diện thật và thiết kế trước khi sửa sản phẩm.

Handoff cuối Giai đoạn 5–7: [FINAL-REPORT.md](FINAL-REPORT.md).
Receipt tổng hợp và mục lục ảnh nằm tại
[`07-final/`](../../artifacts/ui-shots/07-final/). Các receipt Giai đoạn 0–4 dưới
đây là bằng chứng lịch sử; dùng receipt cuối để kiểm mã hiện tại.

Giai đoạn 0 đã hoàn tất: 57 URL, 338 ảnh có metadata, sáu font đã kiểm tra và
baseline tests đã chạy. [Receipt đóng cổng](../../artifacts/ui-shots/00-baseline/stage-0-gate.json)
ghi nhận các lỗi hiện trạng. Nền tảng Giai đoạn 1 đã triển khai; xem
[kiến trúc foundation](02-foundation.md) và [browser gate](../../artifacts/ui-shots/01-foundation/stage-1-gate.json).
[Ảnh và bảng tương phản foundation](../../artifacts/ui-shots/01-foundation/index.html)
đọc được offline.

[API component Giai đoạn 2](03-components.md) mô tả helper, trust boundary,
dialog/callback, log/copy và cách mở style guide local-only. Trang sản phẩm sẽ
chuyển dần sang các component này từ Giai đoạn 3.
[Ảnh và receipt Giai đoạn 2](../../artifacts/ui-shots/02-components/index.html)
ghi 24 tổ hợp, overlays và mobile đã kiểm tra.

- [Bản brief gốc được lưu trong repo](BRIEF.md)
- [Khảo sát và baseline](00-audit.md)
- [Kế hoạch thiết kế](01-design-plan.md)
- [Tiến độ và quyết định](PROGRESS.md)
- [Mục lục ảnh](../../artifacts/ui-shots/00-baseline/index.html)
- [Báo cáo browser](../../artifacts/ui-shots/00-baseline/report.json)
- [Màn phụ và fixture](../../artifacts/ui-shots/00-baseline/state-report.json)

Source frontend nằm ở `src/ai_orchestrator/web/`; backend ghép source vào HTML,
không có bundler. Không mở `index.html` source như một app độc lập vì đó là
template chứa placeholder server. Mục lục ảnh và các nghiên cứu HTML có thể
đọc offline; font/icon của nghiên cứu đều cục bộ.

## Chạy lại khảo sát

Với API đã chạy và tenant hiện có:

```sh
uv run --with playwright --with fonttools --with brotli python scripts/ui-font-study.py --download
uv run --with playwright python scripts/ui-shots.py --org ORG_ID --chromium CHROMIUM_PATH
uv run --with playwright python scripts/ui-state-shots.py --org ORG_ID --chromium CHROMIUM_PATH
uv run python scripts/ui-audit-index.py
uv run python scripts/ui-phase0-gate.py
```

`--download` chỉ cần để lấy lại asset phát triển có phiên bản cố định. Chụp ảnh
không gọi CDN; script cưỡng chế localhost và chặn mọi request ghi. Không chạy
hai probe ghi cùng thư mục đồng thời. `ui-shots.py --strict` kiểm các assertion
hiện có và cố ý thất bại với những lỗi a11y của baseline; chưa thay thế checklist
nghiệm thu Giai đoạn 7.

Fixture chứa nhãn `UI FIXTURE` và chỉ tồn tại trong phản hồi GET bị chặn ở browser.
Không submit form, chạy model, gửi mail, duyệt hồ sơ hoặc tạo task để chụp ảnh.
Backend tests chạy vào database test với provider fake/hash.

Nếu phải khởi động API để khảo sát, dùng provider fake/hash để startup không
khởi động lại workflow hoặc polling email đang chờ:

```sh
AO_API_AUTH_DISABLED=true AO_MODEL_PROVIDER_DEFAULT=fake AO_EMBEDDING_PROVIDER_DEFAULT=hash \
  uv run -m uvicorn ai_orchestrator.main:app --host 127.0.0.1 --port 8100
```

Không chạy seed/migrate/reset để chụp ảnh. `stage-0-gate.json` chỉ xác nhận hồ sơ
khảo sát đầy đủ; các lỗi giao diện hiện trạng được đếm trong `baseline_defects`.
Nó không chứng nhận giao diện đã đạt contrast/a11y hoặc các Giai đoạn 1–7.

## Tiếp tục triển khai

Đọc `PROGRESS.md` trước mỗi phiên. Giữ nhánh `ui/refresh`, commit từng giai đoạn
và từng trang ở Giai đoạn 5. Chỉ qua cổng Giai đoạn 0 khi audit/plan, ảnh, font
evidence và test baseline đã có. Không đưa tài liệu hay prototype nghiên cứu
vào navigation sản phẩm.

Giai đoạn 1 bổ sung layer/token/font/icon và prepaint preferences. Giai đoạn 2
có API `UI` và style guide local-only; ví dụ/helper và cách kiểm chứng nằm ở
`03-components.md`. Từ Giai đoạn 3 dùng lại API này để chuyển từng vùng/từng trang.

Kiểm tra nền tảng sau Giai đoạn 1, với API khảo sát đã chạy:

```sh
uv run --with playwright python scripts/ui-foundation-gate.py --org ORG_ID --chromium CHROMIUM_PATH
```

Gate chỉ đọc localhost, lưu ảnh/receipt tại `artifacts/ui-shots/01-foundation/`.
Không chạy lại `ui-phase0-gate.py` trên source đã thay đổi: gate đó cố ý xác nhận
source giữ nguyên baseline. Dùng receipt lịch sử Giai đoạn 0 để đối chiếu.

Khung Giai đoạn 3: [hành vi và handoff](04-shell.md),
[ảnh trước/sau và receipt](../../artifacts/ui-shots/03-shell/index.html).
Chạy gate với API khảo sát chỉ đọc đã bật:

```sh
uv run --with playwright python scripts/ui-shell-gate.py --org ORG_ID --chromium CHROMIUM_PATH
```

Gate GĐ3 kiểm shell/menu/drawer; chưa thay thế nghiệm thu toàn app ở GĐ7.

Trang Công việc Giai đoạn 4: [hành vi và handoff](05-work.md),
[ảnh trước/sau và receipt](../../artifacts/ui-shots/04-work/index.html).

```sh
uv run --with playwright python scripts/ui-work-gate.py --org ORG_ID --chromium CHROMIUM_PATH
uv run --with playwright python scripts/ui-work-performance.py --org ORG_ID --chromium CHROMIUM_PATH
```

Hai script này chỉ đọc dữ liệu server. Một số POST của form được route.fulfill
ngay tại browser bằng `UI FIXTURE`; chúng không được chuyển tới API, tạo task,
chạy model hoặc xác nhận phê duyệt thật. Performance script dựng baseline từ
commit `7a7c860` trong thư mục tạm, không sửa checkout. Cổng GĐ4 không thay thế
nghiệm thu toàn app ở GĐ7.

Trang Sự cố Giai đoạn 5: [hành vi và handoff](06-issues.md),
[ảnh trước/sau và receipt](../../artifacts/ui-shots/05-issues/index.html).

```sh
uv run --with playwright python scripts/ui-issues-gate.py --org ORG_ID --chromium CHROMIUM_PATH
```

Retry POST của gate bị fulfill 409 ngay tại browser; không retry task vận hành.
Empty/GET 503 và pending loading chỉ là fixture, không ghi DB. Cổng trang Sự cố
không đánh dấu toàn Giai đoạn 5 hoặc toàn app hoàn tất.

## Thêm thành phần và trang

Nguồn màu: `palettes.js` → primitive CSS trong `tokens.css` → semantic token
`--color-*` → component token. Giữ giá trị màu tại nguồn palette/token. Dùng
`--space-2/4/8/12/16/20/24/32/40/56` cho margin, padding và gap; dùng
`--row-padding`, `--cell-padding`, `--field-padding` để mật độ áp dụng nhất quán.
Width, breakpoint và đường viền không phải khoảng cách nội dung. Thêm semantic
token vào cả hai mode và kiểm lại toàn bộ 24 tổ hợp trước khi dùng.

Ưu tiên helper trong `components.js` và `Management`:

```js
UI.panel({
  title: UI.L('Records', 'Bản ghi'),
  content: UI.table({
    label: UI.L('Records', 'Bản ghi'),
    columns: [{label: UI.L('Name', 'Tên')}],
    rows: [{cells: [esc(record.name)]}]
  })
});
```

`content`, `cells` và HTML của action là markup tin cậy: escape mọi trường API
bằng `esc()` trước khi chèn. `UI.logViewer({text: rawLog})` và các helper nhận
text tự escape; không escape hai lần. Dùng `UI.dialog`/`UI.drawer` để có focus
trap, Escape, scroll lock và trả focus. Callback confirm phải trả promise và
throw lỗi để giữ hộp thoại mở, hiển thị lỗi và cho thử lại.

Trang quản lý dùng `Management.page/paint/body/failure`, `wireForm` và route
guard hiện tại. Giữ tên field, method và payload API; quyền quyết định vẫn ở
server. Không viết cache tài nguyên hoặc lỗi giả thành số 0 tại renderer.
`Management.enhance` nâng markup dùng chung mà không thay node đã gắn handler.
Không thêm lớp CSS riêng chỉ để mô phỏng panel, table hoặc form có sẵn.

Đăng ký module vào `page.py` theo thứ tự dependency, trước `boot.js`. Manifest
được import khi API khởi động: thêm module thì khởi động lại API. Sửa nội dung
file đã có chỉ cần tải lại trang. Không thêm CDN, bundler hoặc tải font từ mạng.
Đối chiếu giấy phép tại `THIRD_PARTY_NOTICES.md` trước khi đổi asset.

Giữ các khóa `onx-ui-preferences-v1`, `ao-lang-v1`, `ao-sidebar-collapsed`
và quy tắc normalize hiện tại. Appearance preview phải reset khi rời trang nếu
chưa lưu. Command palette chỉ mở route/form hoặc đổi tùy chọn trên thiết bị;
không dùng lệnh để vượt qua bước xác nhận nghiệp vụ.

## Chạy lại nghiệm thu cuối

API khảo sát phải dùng fake/hash như lệnh ở trên. Dùng org đang có dữ liệu;
không seed/reset database vận hành. Đặt `ORG_ID` và `CHROMIUM_PATH` tương ứng:

```sh
uv run --with playwright python scripts/ui-final-gate.py --org ORG_ID --chromium CHROMIUM_PATH
uv run --with playwright python scripts/ui-interactions-gate.py --org ORG_ID --chromium CHROMIUM_PATH
uv run --with playwright python scripts/ui-foundation-gate.py --org ORG_ID --chromium CHROMIUM_PATH --out artifacts/ui-shots/07-final/foundation
uv run --with playwright python scripts/ui-components-gate.py --org ORG_ID --chromium CHROMIUM_PATH --out artifacts/ui-shots/07-final/components
uv run --with playwright python scripts/ui-work-gate.py --org ORG_ID --chromium CHROMIUM_PATH --out artifacts/ui-shots/07-final/work
uv run --with playwright python scripts/ui-shell-gate.py --org ORG_ID --chromium CHROMIUM_PATH --out artifacts/ui-shots/07-final/shell
uv run --with playwright python scripts/ui-issues-gate.py --org ORG_ID --chromium CHROMIUM_PATH --out artifacts/ui-shots/07-final/issues
uv run --with playwright python scripts/ui-work-performance.py --org ORG_ID --chromium CHROMIUM_PATH --out artifacts/ui-shots/07-final/performance
uv run --with playwright python scripts/ui-commit-boot-gate.py --org ORG_ID --chromium CHROMIUM_PATH --after 9ed2e84 --through e049314
uv run python scripts/ui-style-contract.py
node scripts/ui-components-contract.mjs
make lint
make typecheck
make test-fresh
make test-e2e
# Dừng API khảo sát, rồi bật runtime sản phẩm (OpenRouter):
make serve
uv run --with playwright python scripts/ui-runtime-check.py --org ORG_ID --chromium CHROMIUM_PATH
uv run python scripts/ui-release-gate.py
```

Không sửa source sản phẩm trong lúc chạy ma trận; gate cuối so hash đầu/cuối.
Chạy phép đo performance riêng khi các probe browser khác đã kết thúc để tránh
tranh CPU. `make test-fresh` chỉ reset database test, sau đó nạp lại catalogue
tham chiếu; không dùng database vận hành. E2E cần PostgreSQL/NATS/Temporal thật
và vẫn dùng model fake/hash. Xem `ui-release-gate.py --help` để biết các log cần
lưu dưới `07-final/verification/` trước khi tổng hợp.

Gate interaction nhận POST bằng `route.fulfill` trong browser, bao gồm các lỗi
403/409 có chủ ý. Chúng được ghi riêng thành `expected_fixture_console`, không
được coi là server thành công hay lỗi vận hành. Mọi request ghi không có fixture
và mọi host ngoài loopback làm gate thất bại.

Test demo mặc định đã cách ly vào database test. `test_seeded_company_runs.py`
khởi chạy subprocess với slug công ty test thực đã lưu và kiểm lại task bằng
query ở database test. Test `live_model` là opt-in và có thể ghi công ty dev;
không đưa nó vào gate chụp UI hoặc suite mặc định. Các task “Board pack” từ
phiên bản test cũ là demo ScriptedRuntime, không phải chứng cứ đầu ra model.
