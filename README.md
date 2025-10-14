# 📲 Ứng dụng điểm danh sinh viên bằng Streamlit

Ứng dụng này cho phép giảng viên tạo mã QR động cho từng buổi học, sinh viên quét mã để điểm danh, và hệ thống ghi nhận dữ liệu vào Google Sheets. 

## 🚀 Tính năng chính

### 👩‍🏫 Giảng viên
- Chọn buổi học (Buoi1 → Buoi6)
- Tạo mã QR động (thay đổi mỗi 30 giây)
- Chiếu mã QR lên màn hình lớp
- Thống kê số lượng sinh viên đã điểm danh và danh sách vắng

### 📲 Sinh viên
- Quét mã QR bằng điện thoại
- Nhập MSSV và họ tên
- Hệ thống kiểm tra và ghi dữ liệu vào Google Sheet
- Hiển thị kết quả điểm danh ngay trên màn hình

---

## 📁 Cấu trúc Google Sheet

- Tên file: `DiemDanhHocPhan`
- Gồm các sheet: `Buoi1`, `Buoi2`, ..., `Buoi6`
- Mỗi sheet có các cột:
  - `MSSV` | `Họ và tên` | `Tổ` | `Hiện diện` | `Thời gian`

---

## 🔧 Cài đặt và chạy ứng dụng

### 1. Tạo tài khoản dịch vụ Google (Service Account)
- Truy cập [Google Cloud Console](https://console.cloud.google.com/)
- Tạo project → bật Google Sheets API
- Tạo Service Account → tải file `credentials.json`
- Chia sẻ quyền chỉnh sửa Google Sheet cho email của Service Account

### 2. Clone repo và cài thư viện

```bash
git clone https://github.com/Dhanngh/QRLecturer.git
cd QRLecturer
pip install -r requirements.txt
