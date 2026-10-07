# UI refresh

Đây là hồ sơ triển khai theo `ui-refresh-brief.md`. Giai đoạn 0 khảo sát source,
chụp giao diện thật và thiết kế trước khi sửa sản phẩm.

Giai đoạn 0 đã hoàn tất: 57 URL, 338 ảnh có metadata, sáu font đã kiểm tra và
baseline tests đã chạy. [Receipt đóng cổng](../../artifacts/ui-shots/00-baseline/stage-0-gate.json)
ghi nhận các lỗi hiện trạng; Giai đoạn 1 chưa triển khai.

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
chốt API component và style guide; khi ấy tài liệu này sẽ được bổ sung bằng
ví dụ lấy trực tiếp từ component đã chạy, tránh hướng dẫn một API chưa tồn tại.
