import streamlit as st
import qrcode
from PIL import Image
import random
import string
import time
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ===================== CẤU HÌNH GOOGLE SHEET =====================
def get_sheet(buoi):
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
    client = gspread.authorize(creds)
    sheet = client.open("DiemDanhHocPhan").worksheet(buoi)
    return sheet

# ===================== CHUẨN HÓA HỌ TÊN =====================
def normalize_name(name):
    return ' '.join(word.capitalize() for word in name.strip().split())

# ===================== SINH MÃ QR ĐỘNG =====================
def generate_token():
    return ''.join(random.choices(string.ascii_letters + string.digits, k=8))

def generate_qr_image(buoi):
    token = generate_token()
    timestamp = int(time.time())
    link = f"https://diemdanh.app/?buoi={buoi}&token={token}&timestamp={timestamp}"
    qr = qrcode.make(link)
    return qr, token, timestamp

def is_token_valid(token, timestamp, expiry=30):
    return int(time.time()) - int(timestamp) <= expiry

# ===================== GHI ĐIỂM DANH =====================
def mark_attendance(buoi, mssv, hoten):
    sheet = get_sheet(buoi)
    data = sheet.get_all_records()
    for i, row in enumerate(data):
        if str(row["MSSV"]) == str(mssv) and normalize_name(row["Họ và tên"]) == hoten:
            sheet.update_cell(i+2, sheet.find("Hiện diện").col, "Có")
            sheet.update_cell(i+2, sheet.find("Thời gian").col, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            return True
    return False

# ===================== THỐNG KÊ =====================
def get_attendance_stats(buoi):
    sheet = get_sheet(buoi)
    data = sheet.get_all_records()
    diem_danh = sum(1 for row in data if row["Hiện diện"] == "Có")
    vang = sum(1 for row in data if row["Hiện diện"] != "Có")
    ds_vang = [row for row in data if row["Hiện diện"] != "Có"]
    return {
        "diem_danh": diem_danh,
        "vang": vang,
        "ds_vang": ds_vang
    }

# ===================== GIAO DIỆN STREAMLIT =====================
st.set_page_config(page_title="Điểm danh sinh viên", layout="centered")
tab_gv, tab_sv = st.tabs(["👩‍🏫 Giảng viên", "📲 Sinh viên điểm danh"])

# --------------------- GIẢNG VIÊN ---------------------
with tab_gv:
    st.header("🔐 Tạo mã QR điểm danh")
    buoi = st.selectbox("Chọn buổi học", ["Buoi1", "Buoi2", "Buoi3", "Buoi4", "Buoi5", "Buoi6"])

    if st.button("🎯 Tạo mã QR động"):
        qr_img, token, timestamp = generate_qr_image(buoi)
        st.image(qr_img, caption="Mã QR điểm danh (có hiệu lực trong 30 giây)")
        st.session_state["token"] = token
        st.session_state["timestamp"] = timestamp
        st.session_state["buoi"] = buoi

    if "buoi" in st.session_state:
        st.subheader("📊 Thống kê điểm danh")
        stats = get_attendance_stats(st.session_state["buoi"])
        st.metric("✅ Đã điểm danh", stats["diem_danh"])
        st.metric("❌ Vắng mặt", stats["vang"])
        st.write("📋 Danh sách vắng:")
        st.dataframe(stats["ds_vang"])

# --------------------- SINH VIÊN ---------------------
with tab_sv:
    st.header("📲 Sinh viên điểm danh")
    mssv = st.text_input("Nhập MSSV")
    hoten = st.text_input("Nhập họ và tên")

    if st.button("✅ Xác nhận điểm danh"):
        buoi = st.session_state.get("buoi")
        token = st.session_state.get("token")
        timestamp = st.session_state.get("timestamp")

        if not mssv.isdigit():
            st.warning("⚠️ MSSV phải là số.")
        elif not is_token_valid(token, timestamp):
            st.error("❌ Mã QR đã hết hạn hoặc không hợp lệ.")
        else:
            hoten_chuan = normalize_name(hoten)
            success = mark_attendance(buoi, mssv, hoten_chuan)
            if success:
                st.success(f"✅ Điểm danh thành công lúc {datetime.now().strftime('%H:%M:%S')}")
            else:
                st.error("❌ MSSV hoặc họ tên không khớp với danh sách.")
