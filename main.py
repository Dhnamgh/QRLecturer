import streamlit as st
import qrcode
from PIL import Image
import random
import string
import time
from datetime import datetime
import gspread
from oauth2client.service_account import ServiceAccountCredentials
import os
import json
import io

# ===================== CẤU HÌNH GOOGLE SHEET =====================
def get_sheet():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client = gspread.authorize(creds)
    sheet = client.open_by_key("1sWG3jE8lDezfmGcEQgdRCRSBXxNjj9Xz").worksheet("D25A")
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
    sheet = get_sheet()
    data = sheet.get_all_records()
    col_diemdanh = sheet.find(buoi).col
    col_thoigian = sheet.find(f"Thời gian {buoi[-1]}").col

    for i, row in enumerate(data):
        if str(row["MSSV"]) == str(mssv) and normalize_name(row["Họ và Tên"]) == hoten:
            sheet.update_cell(i+2, col_diemdanh, "Có")
            sheet.update_cell(i+2, col_thoigian, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            return True
    return False

# ===================== THỐNG KÊ =====================
def get_attendance_stats(buoi):
    sheet = get_sheet()
    data = sheet.get_all_records()
    diem_danh = sum(1 for row in data if row.get(buoi) == "Có")
    vang = sum(1 for row in data if row.get(buoi) != "Có")
    ds_vang = [row for row in data if row.get(buoi) != "Có"]
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
    buoi_hien_thi = st.selectbox("Chọn buổi học", ["Buổi 1", "Buổi 2", "Buổi 3", "Buổi 4", "Buổi 5", "Buổi 6"])
    buoi = buoi_hien_thi  # dùng trực tiếp tên cột

    if st.button("🎯 Tạo mã QR động"):
        qr_img, token, timestamp = generate_qr_image(buoi)
        buf = io.BytesIO()
        qr_img.save(buf, format="PNG")
        st.image(buf.getvalue(), caption="Mã QR điểm danh (có hiệu lực trong 30 giây)")
        st.session_state["token"] = token
        st.session_state["timestamp"] = timestamp
        st.session_state["buoi"] = buoi

    if "buoi" in st.session_state:
    st.subheader("📊 Thống kê điểm danh")
    try:
        stats = get_attendance_stats(st.session_state["buoi"])
        st.metric("✅ Đã điểm danh", stats["diem_danh"])
        st.metric("❌ Vắng mặt", stats["vang"])
        st.write("📋 Danh sách vắng:")
        st.dataframe(stats["ds_vang"])
    except Exception as e:
        st.error(f"❌ Lỗi khi lấy thống kê: {e}")

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

