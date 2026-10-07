# Nền tảng đã triển khai — Giai đoạn 1

`page.py` vẫn ghép HTML/CSS/JS từ file cục bộ, không bundler. Thứ tự layer:
`reset → tokens → base → layout → components → pages → utilities`.
`layout/components` dành cho giai đoạn sau; CSS legacy giữ nguyên thứ tự trong
`pages`. Không có `!important`; style inline legacy còn được xử lý từng trang.

## Token và cách dùng tiếp theo

- Primitive: `--primitive-surface`, `--primitive-text`, `--primitive-accent`, v.v.
  Palette JS chỉ cập nhật tầng này. Graphite chuyển màu cấu trúc thành grayscale;
  status primitives tách khỏi accent và không bị tint.
- Semantic: `--color-surface-1`, `--color-text-secondary`, `--color-border-control`,
  `--color-focus`, `--color-danger-fg/bg/border`, cùng success/warning/info/neutral.
- Component: `--btn-height`, `--field-height/padding`, `--row-min-height/padding`,
  `--cell-padding`, `--panel-padding`, `--label-size`. API component đầy đủ ở GĐ2.
- Alias `--surface`, `--text-2`, `--line-strong`, `--bad`, v.v. giữ renderer cũ chạy.
  Component mới dùng semantic và component token, không chép hex vào trang.
- Density thay padding/minimum, không ép chiều cao dòng. Coarse pointer vẫn
  giữ minimum nút/field 44px kể cả compact. Motion dùng 80/140/200/320/480ms;
  OS hoặc tùy chọn reduced đều tắt animation/transition trong foundation.

## Font, icon và tải đầu

Inter và JetBrains Mono tự host, 400/500/600, ba subset latin/latin-ext/vietnamese.
`fonts.css` có unicode range từ cmap thực và metric fallback; `font-metrics.json`
ghi phép đo, `fonts/sources.json` ghi nguồn/hash. Preload Inter regular latin và
vietnamese, `font-display: swap`. Không phân phối Arial/Courier New.

Sprite inline 30 symbol Lucide 0.468.0; `UIIcon(name)` chỉ nhận tên đã whitelist,
fallback circle-help. SVG trang trí có `aria-hidden`, tên truy cập thuộc control.
Không dùng icon font hoặc tải sprite từ CDN. Raw SVG và giấy phép còn nguyên.

Script head chạy palette/preferences trước CSS, lấy đúng khóa/field cũ. Khóa
`onx-ui-preferences-v1`, `ao-lang-v1`, `ao-sidebar-collapsed` giữ nguyên. Không
ghi localStorage khi chỉ đọc/normalize; save mới ghi. Mặc định VI; EN đã lưu
được giữ. Theme auto theo OS; đổi theme tắt transition qua hai animation frame.

Ngoại lệ backend: GET public `/api/v1/ui/assets/{filename}` chỉ trả 18 WOFF2
đúng tên đã whitelist, `font/woff2`, nosniff, cache 24h. Không mở static directory
hoặc đổi API nghiệp vụ. Bộ đóng gói phải chứa `web/assets` cùng các file web.

## Kiểm chứng và giới hạn

Receipt: `artifacts/ui-shots/01-foundation/stage-1-gate.json` và 30 ảnh. Browser
Chromium kiểm computed colors, nút thật, row containment, mobile, prepaint và
font shaping. Đây là cổng foundation, không thay axe trên component/style guide
GĐ2 hoặc ma trận toàn route/a11y/keyboard cuối. Safari/Firefox chưa được chạy.

GĐ2 tiếp theo: button/input/select/textarea, badge/status, alert/empty/loading,
card/section header, table/list row, menu/popover/dialog; đủ hover/focus/disabled/
loading/error, VI/EN, keyboard và 12 màu × 2 mật độ ở style guide local-only.
Không thay renderer tất cả trang cùng lúc; mỗi lần migration giữ contract cũ.
