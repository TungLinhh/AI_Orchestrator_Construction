[SYNTHETIC SOURCE ACCEPTANCE] mock

CONTAINS MOCK SOURCES / CÓ NGUỒN MOCK

Đánh giá ba báo giá cho Valve-1: A được chọn vì có chứng chỉ và tổng chi phí thấp nhất trong các báo giá có chứng chỉ.

Winner: A | Total VND: 1520000

- Báo giá A: số lượng 12, giá đơn vị 120000, giao hàng 80000, đã certify.
  [src_01m4y7nfj59dq01vmmpm895vb4 / lines:1-4] A,Valve-1,12,120000,80000,true

- Báo giá B không có chứng chỉ (certified=false) nên loại.
  [src_01m4y7nfj59dq01vmmpm895vb4 / lines:1-4] B,Valve-1,12,110000,0,false

- Báo giá C có chứng chỉ nhưng tổng chi phí cao hơn A.
  [src_01m4y7nfj59dq01vmmpm895vb4 / lines:1-4] C,Valve-1,12,130000,0,true

- Tổng chi phí cho A: 12 * 120000 + 80000 = 1520000 VND.
  [src_01m4y7nfj59dq01vmmpm895vb4 / lines:1-4] A,Valve-1,12,120000,80000,true

- Chỉ có 3 báo giá được phân tích

- Không có thông tin về yêu cầu chứng chỉ cụ thể

src_01m4y7nfj59dq01vmmpm895vb4 | mock-quotes-1.csv | SHA-256 05ebf8e68b197ada8d5b88dcee5a7531a1e71ae547ca82596586e21768af37ce | internal | mock=True