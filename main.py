import streamlit as st
import json
import gspread
from google.oauth2.service_account import Credentials  # ✅ dùng lib mới
import qrcode
from PIL import Image
import io
import time
import random
import string
import urllib.parse

# ========== CẤU HÌNH GOOGLE SHEETS ==========
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

@st.cache_resource
def _get_gspread_client():
    # Lấy JSON từ st.secrets (có thể là str hoặc dict)
    raw = st.secrets.get("GOOGLE_CREDENTIALS")
    if raw is None:
        raise RuntimeError("Thiếu GOOGLE_CREDENTIALS trong Secrets.")

    creds_dict = raw if isinstance(raw, dict) else json.loads(raw)

    # Fix trường hợp private_key bị double-escape: "\\n" -> "\n"
    pk = creds_dict.get("private_key", "")
    if "\\n" in pk:
        creds_dict["private_key"] = pk.replace("\\n", "\n")

    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    return client

def get_sheet():
    client = _get_gspread_client()
    # Mở sheet theo ID & tab của bạn
    ss = client.open_by_key("1sWG3jE8lDezfmGcEQgdRCRSBXxNjj9Xz")
    return ss.worksheet("D25A")

# ========== TIỆN ÍCH ==========
def get_query_params():
    # Streamlit mới: st.query_params; cũ: experimental_get_query_params
    if hasattr(st, "query_params"):
        return dict(st.query_params)
    raw = st.experimental_get_query_params()
    return {k: (v[0] if isinstance(v, list) and v else v) for k, v in raw.items()}

def generate_token(k=8):
    import string, random
    return ''.join(random.choices(string.ascii_letters + string.digits, k=k))

# ========== APP ==========
st.set_page_config(page_title="QR Lecturer", layout="centered")
st.title("📋 Hệ thống điểm danh QR")

tab1, tab2 = st.tabs(["👨‍🏫 Giảng viên", "🎓 Sinh viên"])

with tab1:
    st.subheader("📸 Tạo mã QR điểm danh")
    buoi = st.selectbox("Chọn buổi học", ["Buổi 1", "Buổi 2", "Buổi 3", "Buổi 4", "Buổi 5", "Buổi 6"])

    if st.button("Tạo mã QR"):
        st.session_state["buoi"] = buoi

        # Tạo link QR (giữ nguyên logic của bạn)
        # Khuyến nghị: thêm token/timestamp nếu cần TTL
        qr_data = f"https://qrlecturer.streamlit.app/?buoi={urllib.parse.quote(buoi)}"

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
            sheet = get_sheet()
            col = sheet.find(st.session_state["buoi"]).col
            data = sheet.col_values(col)[1:]  # Bỏ header
            diem_danh = sum(1 for x in data if str(x).strip())
            vang = len(data) - diem_danh
            # Giả định cột 3 là "Họ và Tên" như code cũ của bạn
            ds_vang = [sheet.cell(i + 2, 3).value for i, x in enumerate(data) if not str(x).strip()]

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
    qp = get_query_params()
    buoi_sv = qp.get("buoi", "Buổi 1")

    if st.button("Điểm danh"):
        try:
            sheet = get_sheet()
            cell = sheet.find(mssv)
            sheet.update_cell(cell.row, sheet.find(buoi_sv).col, "✅")
            st.success("🎉 Điểm danh thành công!")
        except Exception as e:
            st.error(f"❌ Lỗi khi điểm danh: {e}")
