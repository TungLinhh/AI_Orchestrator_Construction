# Giai đoạn 3 — khung ứng dụng

Khung giữ các ID, route và bộ nhớ hiện có; không thay API nghiệp vụ, schema,
controller workflow hoặc quyền phê duyệt. `shell.js` được ghép sau renderer và
trước boot. `shell.css` thay các rule khung cũ đã gỡ khỏi console/management;
CSS được scope bằng ID/class khung, không thêm `!important`.

## Hành vi

- Sidebar 216px, rail 64px, icon/chữ cách 8px. Nhãn nhóm sentence case. Một
  indicator 2px dùng cùng node, đo lại khi đổi route, resize, scroll và thu gọn.
  Chọn hàng chỉ có nền nhẹ, không box-shadow/viền kép. Rail có tooltip khi hover
  và focus; tooltip dùng bộ thành phần GĐ2.
- Workspace là GET `/organizations/{ORG}` của tenant hiện tại. Phiên hiện có
  một mục; không suy diễn danh sách tenant từ quyền hiện tại hoặc tạo workspace
  giả. Tên được lấy từ API, ID vẫn thấy trong menu, có lối vào Cài đặt tổ chức.
  Nếu GET lỗi, hiện ID và thông báo chưa tải được tên. Không gọi API đổi tenant.
- Topbar sticky: back ghost và Esc, breadcrumb cũ, chuông có chấm chưa đọc và
  accessible label chứa số lượng, VI/EN segmented, menu giao diện.
  Bell dùng nguyên inbox/notify/mark-read cũ; fixture browser không chứng minh
  khả năng gửi notification hay phê duyệt thật của backend.
- Menu giao diện dùng UI.tabs cho mode/density, sáu màu từ UIPalettes. Lựa chọn
  preview trực tiếp và lưu ngay bằng UIPreferences; mode Auto theo OS.
  Không sửa khóa prefs, locale hoặc sidebar đã có.
- Auth-off callout nằm trước mọi view, mỏng, mở rộng để đọc thông tin dev:no-auth
  và khuyến nghị service token. Đóng details chỉ thu gọn nội dung; cảnh báo vẫn
  hiện khi auth-off và trở lại sau reload. Auth-on giữ nút Token cũ.
- Mobile dùng dialog offcanvas thật, chuyển nguyên node sidebar vào modal rồi
  trả lại vị trí gốc. Không clone ID/handlers hoặc lưu collapse của desktop.
  Workspace popover đi cùng dialog để tránh bị inert bên ngoài modal.
  Tab ở trong drawer, Esc đóng popover trước drawer, focus trở về nút mở;
  chọn trang đưa focus vào main. Resize sang desktop trả sidebar và focus cho
  nút thu gọn. Scroll body được khóa đến khi drawer đóng.

## Kiểm chứng

`ui-shell-gate.py` cưỡng chế localhost và phương thức chỉ đọc. Ma trận sáu màu ×
sáng/tối × hai mật độ mở menu qua control thật, kiểm prefs, geometry, axe trên
shell và chụp ảnh. Thao tác kiểm keyboard, tooltip, indicator, security callout,
workspace, bell, Auto/reduced motion, reload và drawer 390/320px.

Fixture có khai báo trong receipt: một notification chỉ ở bộ nhớ browser,
HTML auth-on không gửi token và GET tên workspace bị trả 503. Không dùng fixture
để nhận là đã có approval/model/mail thật. Axe scope shell/menu/drawer; chưa
đánh giá lại mọi trang legacy hoặc Safari/Firefox. Kết quả cuối ở PROGRESS.md
và `artifacts/ui-shots/03-shell/stage-3-gate.json`.

## Tiếp tục

GĐ4 chuyển trang Công việc/detail sang bộ thành phần: dải chỉ số thống nhất,
tìm kiếm/tab/count/sort, hàng và panel kết quả/log/approval, form giao việc và
cập nhật danh sách tăng dần. Đo hiệu năng bằng corpus thật, không thay nghĩa
`settled`, không thêm KPI hoặc dashboard mới. GĐ5 mới chuyển các trang còn lại.
