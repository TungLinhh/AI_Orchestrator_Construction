# Chọn nguồn và nhập tài liệu cho task

> Cập nhật triển khai 15-10-2026: xem [SOURCE_WORKFLOW_STATUS.md](SOURCE_WORKFLOW_STATUS.md)
> cho luồng nguồn/mock, upload, executor và export đã viết, giới hạn và bằng chứng.
> Chuỗi execution hiện hành: NVIDIA Nemotron Ultra free → DOTS free; Ling/Inkling
> trong các snapshot bên dưới là lịch sử, đã bị thay thế. Các số test cũ không
> thay cho cổng kiểm chứng của thay đổi nguồn hiện tại.

Ngày quyết định: 2026-10-07. Đây là thiết kế triển khai tiếp theo, chưa phải tính
năng upload đã hoàn tất. Phạm vi MVP là tài liệu tổng hợp và nguồn do người dùng
chọn rõ ràng. Không tự coi dữ liệu đang có trong tenant là đầu vào cho mọi task.

## Hỏi nguồn trước khi chạy

Khi người dùng nhấn Chạy, hỏi: **"Bạn muốn chọn nguồn dữ liệu hay tạo file mock
để kiểm thử?"** Hai lựa chọn không được tự chọn thay người dùng.

1. **Chọn nguồn dữ liệu** mở kho tài liệu của tenant. Cho chọn phiên bản file,
   upload file mới hoặc chọn kết nối đã cấp quyền. Hiển thị tên, loại, kích thước,
   ngày cập nhật, trạng thái trích xuất và phạm vi truy cập.
2. **Tạo file mock để kiểm thử** tạo gói file có thật theo loại task, ghi
   `synthetic=true`, seed và phiên bản generator. File mock đi qua cùng pipeline
   upload, trích xuất và kiểm chứng. Không chỉ đưa dictionary vào prompt.
3. Trước khi chạy, hiện manifest nguồn, đầu ra phải có và tiêu chí hoàn thành.
   Task thiếu nguồn hoặc nguồn chưa đọc được ở `waiting_for_input`. Không gọi
   model rồi hy vọng nó tự đoán dữ liệu.
4. Cho bổ sung hoặc đổi nguồn trong vòng hỏi đáp. Thay phiên bản nguồn tạo revision,
   vô hiệu artifact và approval phụ thuộc. Lưu lịch sử, không sửa chứng cứ cũ.

Chọn Gmail, Drive hoặc plugin vẫn chỉ là chọn nơi tìm dữ liệu. Workflow phải
thực hiện tìm kiếm, tải, kiểm quyền, đọc attachment và ghi provenance theo các
bước hiện hành. File tải thủ công không được coi là đã nghiệm thu connector.

## Một dịch vụ nhập nguồn dùng chung

Chọn một `SourceIngestionService` ở application, với adapter ở infrastructure.
API nhận file và trả handle; parser chạy ở worker có giới hạn tài nguyên. Giữ
PostgreSQL làm chủ metadata và trạng thái. Local filesystem là storage MVP sau
một interface có thể thay bằng object storage. Temporal tiếp tục sở hữu công việc
bền vững. Domain chỉ chứa quy tắc và kiểu dữ liệu, không đọc file hay gọi OCR.

Đã so sánh ba cách:

| Cách | Quyết định |
| --- | --- |
| Mỗi workflow tự đọc file | Loại. Lặp parser, giới hạn archive và kiểm quyền; không có một provenance contract |
| Gửi mọi file trực tiếp cho model | Loại làm mặc định. Không kiểm chứng được parser/citation, khó giữ quyền và chi phí |
| Kho nguồn bất biến + adapter trích xuất + manifest theo task | Chọn. Workflow dùng chung nguồn đã kiểm tra, vẫn truy xuất được file gốc |

Luồng chính:

```text
upload hoặc connector fetch
→ quarantine + kiểm quyền + checksum
→ asset version
→ parser/OCR worker
→ nội dung có locator + cảnh báo
→ manifest được người dùng chọn
→ workflow stage → validator → artifact → review → export
```

## Contract dữ liệu và quyền sở hữu

| Kiểu | Nội dung và chủ sở hữu |
| --- | --- |
| `SourceAsset` | Tenant, người sở hữu, nguồn upload/connector/mock, tên gốc, MIME được phát hiện, số byte, SHA-256, classification, retention; kho nguồn sở hữu |
| `SourceVersion` | Blob bất biến, parent archive/message, phiên bản remote, checksum, thời điểm fetch, lineage; không cập nhật blob tại chỗ |
| `Extraction` | Parser/version, trạng thái queued/extracting/ready/needs_review/unsupported/failed, text, bảng, trang, bbox hoặc cell, OCR confidence, warnings |
| `TaskSourceSelection` | Discriminated union `mock` hoặc `sources`. `sources` có thể gồm nhiều library version và connector query. Không dùng nhiều cờ boolean mâu thuẫn |
| `TaskSourceManifest` | Task/revision, asset version, locator, classification, synthetic, purpose, connection scope, hash; controller sở hữu |
| `GeneratedArtifact` | Nội dung xuất, source manifest hash, execution/model provenance và kiểm tra nghiệp vụ; artifact service sở hữu |

Tái sử dụng `documents` và `document_versions` cho hồ sơ nghiệp vụ được kiểm soát.
Không cấp mã ONX cho attachment chưa phải tài liệu ban hành. Blob/extraction có
metadata riêng và liên kết tới tài liệu khi phù hợp; không nhét OCR vào audit log.
Migration mới phải có RLS, composite FK theo tenant, ORM sync và isolation tests.

Mỗi lần chọn, tải, trích xuất, search hoặc lấy chunk đều kiểm tenant và quyền
đơn vị. Dedup theo tenant + hash, không tiết lộ một tenant khác có cùng file.
Classification của task là mức cao nhất trong các nguồn và ngữ cảnh liên quan.
Gateway dùng giới hạn thấp nhất của profile và provider. Không tự hạ nhãn chỉ
vì file hoặc người gửi tự ghi `synthetic=true`.

## Tiếp nhận rộng, công bố rõ khả năng đọc

Không cam kết hiểu mọi định dạng. Có thể lưu file chưa hỗ trợ, nhưng phải hiện
`unsupported` và yêu cầu chuyển đổi hoặc nguồn thay thế trước khi dùng làm bằng chứng.

| Nhóm | MVP và điều kiện kiểm chứng |
| --- | --- |
| TXT, Markdown, CSV, JSON | Unicode, delimiter/schema, giới hạn byte và text; giữ số dòng hoặc JSON path |
| PDF có text | Giữ trang, bảng và thứ tự đọc. PDF mã hóa/hỏng cần hỏi người dùng |
| PDF scan, JPEG, PNG, TIFF | OCR VI/EN có confidence và locator. Chữ mờ, xoay, bảng hoặc kết quả rỗng cần review |
| DOCX | Đoạn, bảng, header/footer và relationship; không chạy macro hay tải external relationship |
| XLSX | Tái sử dụng reader hiện có; giữ sheet/cell và cả công thức lẫn cached value khi có. Không đánh đồng ô chưa tính với số 0 |
| ZIP | Container có cây file và manifest; kiểm từng member, giới hạn độ sâu, số file, tổng byte giải nén và compression ratio |
| PPTX, EML và các loại còn lại | Bổ sung theo nhu cầu tenant sau corpus kiểm chứng; file không đọc được không được bỏ qua âm thầm |

Từ chối path traversal, symlink, archive bomb và file vượt quota trước khi parser
ghi ra kho. Dùng tên storage do hệ thống cấp, không dùng đường dẫn trong upload.
Parser có timeout, CPU/RAM cap và không có quyền chạy nội dung hay ra mạng.
Connector giữ credential theo tenant/connection, cursor, remote version và receipt.

FastAPI đã có `python-multipart` trong dependencies; `UploadFile` hỗ trợ spooled
file, phù hợp adapter upload. [FastAPI request files](https://fastapi.tiangolo.com/tutorial/request-files/).
Đánh giá Docling trên corpus VI/EN để thống nhất PDF/DOCX/ảnh/bảng. Không thêm nó
vào runtime trước khi đo fidelity và footprint.
[Docling formats](https://github.com/docling-project/docling/blob/main/docs/usage/supported_formats.md).
PyMuPDF + Tesseract là phương án đối chứng cho PDF/OCR, cần kiểm giấy phép và
runtime dependencies trước khi chọn.
[PyMuPDF OCR](https://pymupdf.readthedocs.io/en/latest/recipes-ocr.html).

## Chia thành ba đợt có sản phẩm nghiệm thu

1. Kho nguồn + upload + chọn nguồn/mock cho task mới. Bắt đầu TXT/CSV/JSON/DOCX/PDF
   có text và XLSX. Nghiệm thu qua browser → API → storage → parser → manifest →
   một task hoàn thành → tải artifact, với hai tenant không nhìn thấy file của nhau.
2. ZIP + OCR + mock packet đa định dạng. Chạy file hỏng, scan VI, bảng xoay,
   archive bomb, đổi tên sai MIME, encrypted file và injection. Đối chiếu page/cell
   citation với file gốc; thiếu chứng cứ phải hỏi lại, không hoàn thành giả.
3. Gmail/Drive/plugin sources. Read-only canary bằng tài khoản được cấp riêng;
   kiểm fetch/retry/cursor/revocation/remote update/receipt. Sau đó mới mở write
   action theo approval, idempotency và read-back riêng của connector.

Các đợt trên là thay đổi API/schema/UI có phạm vi riêng. Thiết kế chi tiết và
migration cần được review trước khi triển khai rộng; đợt model này không âm thầm
thay controller hoặc cấu trúc UI đã nghiệm thu.
