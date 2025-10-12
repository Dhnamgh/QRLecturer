import streamlit as st
import json
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import qrcode
from PIL import Image
import io
import time

# Cấu hình quyền truy cập Google Sheets
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
creds_dict = json.loads(st.secrets["GOOGLE_CREDENTIALS"])
creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
client = gspread.authorize(creds)

# Mở sheet theo ID thật
sheet = client.open_by_key("1sWG3jE8lDezfmGcEQgdRCRSBXxNjj9Xz").worksheet("D25A")

st.set_page_config(page_title="QR Lecturer", layout="centered")
st.title("📋 Hệ thống điểm danh QR")

tab1, tab2 = st.tabs(["👨‍🏫 Giảng viên", "🎓 Sinh viên"])

with tab1:
    st.subheader("📸 Tạo mã QR điểm danh")

    buoi = st.selectbox("Chọn buổi học", ["Buổi 1", "Buổi 2", "Buổi 3", "Buổi 4", "Buổi 5", "Buổi 6"])

    if st.button("Tạo mã QR"):
        st.session_state["buoi"] = buoi

        # Tạo link QR
        qr_data = f"https://qrlecturer.streamlit.app/?buoi={buoi}"

        # Tạo ảnh QR
        qr = qrcode.make(qr_data)
        buf = io.BytesIO()
        qr.save(buf)
        buf.seek(0)
        img = Image.open(buf)

        st.image(img, caption="📱 Quét mã để điểm danh", width=250)
        st.write(f"🔗 Link: {qr_data}")

        # Đếm ngược 30 giây
        countdown = st.empty()
        for i in range(30, 0, -1):
            countdown.markdown(f"⏳ Thời gian còn lại: **{i} giây**")
            time.sleep(1)
        countdown.markdown("✅ Hết thời gian điểm danh")

    # Thống kê điểm danh
    if "buoi" in st.session_state:
        st.subheader("📊 Thống kê điểm danh")
        try:
            col = sheet.find(st.session_state["buoi"]).col
            data = sheet.col_values(col)[1:]  # Bỏ header
            diem_danh = sum(1 for x in data if x.strip())
            vang = len(data) - diem_danh
            ds_vang = [sheet.cell(i + 2, 3).value for i, x in enumerate(data) if not x.strip()]  # Cột tên

            st.metric("✅ Đã điểm danh", diem_danh)
            st.metric("❌ Vắng mặt", vang)
            st.write("📋 Danh sách vắng:")
            st.dataframe(ds_vang)
        except Exception as e:
            st.error(f"❌ Lỗi khi lấy thống kê: {e}")

with tab2:
    st.subheader("📲 Nhập thông tin điểm danh")

    mssv = st.text_input("Nhập MSSV")
    hoten = st.text_input("Nhập họ tên")
    buoi_sv = st.query_params.get("buoi", "Buổi 1")

    if st.button("Điểm danh"):
        try:
            cell = sheet.find(mssv)
            sheet.update_cell(cell.row, sheet.find(buoi_sv).col, "✅")
            st.success("🎉 Điểm danh thành công!")
        except Exception as e:
            st.error(f"❌ Lỗi khi điểm danh: {e}")
