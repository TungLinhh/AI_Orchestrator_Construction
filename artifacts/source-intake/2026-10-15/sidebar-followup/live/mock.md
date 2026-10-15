[SYNTHETIC SOURCE ACCEPTANCE] mock

CONTAINS MOCK SOURCES / CÓ NGUỒN MOCK

So sánh ba báo giá Valve-1 (số lượng 12): A tổng 1.520.000 VNĐ (có chứng chỉ), B tổng 1.320.000 VNĐ (không chứng chỉ), C tổng 1.560.000 VNĐ (có chứng chỉ). Tiêu chí: bắt buộc certified=true, sau đó chọn tổng chi phí thấp nhất. Nhà thầu A thắng nhờ đáp ứng chứng chỉ và tổng chi phí thấp nhất trong nhóm đủ điều kiện.

Winner: A | Total VND: 1520000

- Báo giá A: đơn giá 120.000 VNĐ, phí giao hàng 80.000 VNĐ, certified=true → tổng = 12×120.000 + 80.000 = 1.520.000 VNĐ
  [src_01m4ysf1z4peqx6qar5vqb5sn4 / lines:1-4] A,Valve-1,12,120000,80000,true

- Báo giá B: đơn giá 110.000 VNĐ, phí giao hàng 0, certified=false → tổng = 12×110.000 = 1.320.000 VNĐ nhưng không đủ điều kiện do thiếu chứng chỉ
  [src_01m4ysf1z4peqx6qar5vqb5sn4 / lines:1-4] B,Valve-1,12,110000,0,false

- Báo giá C: đơn giá 130.000 VNĐ, phí giao hàng 0, certified=true → tổng = 12×130.000 = 1.560.000 VNĐ, đáp ứng chứng chỉ nhưng đắt hơn A
  [src_01m4ysf1z4peqx6qar5vqb5sn4 / lines:1-4] C,Valve-1,12,130000,0,true

- Không có thông tin về điều khoản thanh toán, bảo hành, thời gian giao hàng cụ thể, hay năng lực tài chính của nhà thầu

- Chỉ so sánh trên một mặt hàng Valve-1, chưa xem xét gói thầu rộng hơn

src_01m4ysf1z4peqx6qar5vqb5sn4 | mock-quotes-1.csv | SHA-256 05ebf8e68b197ada8d5b88dcee5a7531a1e71ae547ca82596586e21768af37ce | internal | mock=True