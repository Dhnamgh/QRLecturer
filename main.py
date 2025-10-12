import streamlit as st
import json
import gspread
from google.oauth2.service_account import Credentials
import qrcode
from PIL import Image
import io
import time
import urllib.parse

# ===================== CẤU HÌNH GOOGLE SHEETS =====================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SHEET_KEY = "1sWG3jE8lDezfmGcEQgdRCRSBXxNjj9Xz"
WORKSHEET_NAME = "D25A"

@st.cache_resource
def _get_gspread_client():
    raw = st.secrets.get("GOOGLE_CREDENTIALS")
    if raw is None:
        raise RuntimeError("Thiếu GOOGLE_CREDENTIALS trong Secrets.")
    creds_dict = raw if isinstance(raw, dict) else json.loads(raw)
    if "private_key" in creds_dict and "\\n" in creds_dict["private_key"]:
        creds_dict["private_key"] = creds_dict["private_key"].replace("\\n", "\n")
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    return gspread.authorize(creds)

def get_sheet():
    client = _get_gspread_client()
    ss = client.open_by_key(SHEET_KEY)
    return ss.worksheet(WORKSHEET_NAME)

# ===================== TIỆN ÍCH =====================
def get_query_params():
    if hasattr(st, "query_params"):
        return dict(st.query_params)
    raw = st.experimental_get_query_params()
    return {k: (v[0] if isinstance(v, list) and v else v) for k, v in raw.items()}

def normalize_name(name: str):
    return ' '.join(w.capitalize() for w in name.strip().split())

# ===================== GIAO DIỆN =====================
st.set_page_config(page_title="QR Lecturer", layout="centered")
qp = get_query_params()

# Điều kiện kích hoạt "chế độ chỉ SV": có sv=1 hoặc có buoi trong URL
student_only = (qp.get("sv") == "1") or ("buoi" in qp)

# ===================== MÀN HÌNH CHỈ SV (khi quét QR) =====================
if student_only:
    buoi_sv = qp.get("buoi", "Buổi 1")
    st.title("🎓 Điểm danh sinh viên")
    st.info(f"Bạn đang điểm danh cho **{buoi_sv}**")

    mssv = st.text_input("Nhập MSSV")
    hoten = st.text_input("Nhập họ và tên")

    if st.button("✅ Xác nhận điểm danh"):
        if not mssv.strip().isdigit():
            st.warning("MSSV phải là số.")
        elif not hoten.strip():
            st.warning("Vui lòng nhập họ và tên.")
        else:
            try:
                sheet = get_sheet()
                # Tìm cột buổi
                col_buoi = sheet.find(buoi_sv).col
                # Tìm MSSV theo đúng dòng trong sheet
                cell_mssv = sheet.find(str(mssv).strip())
                # (khuyến nghị) kiểm tra họ tên khớp
                hoten_sheet = sheet.cell(cell_mssv.row, sheet.find("Họ và Tên").col).value
                if normalize_name(hoten_sheet or "") != normalize_name(hoten):
                    st.error("Họ tên không khớp với MSSV trong danh sách.")
                else:
                    sheet.update_cell(cell_mssv.row, col_buoi, "✅")
                    st.success("🎉 Điểm danh thành công!")
            except Exception as e:
                st.error(f"❌ Lỗi khi điểm danh: {e}")

    # Không render gì thêm trong chế độ SV
    st.stop()

# ===================== MÀN HÌNH ĐẦY ĐỦ (GIẢNG VIÊN) =====================
st.title("📋 Hệ thống điểm danh QR")
tab_gv, tab_sv = st.tabs(["👨‍🏫 Giảng viên", "🎓 Sinh viên"])

with tab_gv:
    st.subheader("📸 Tạo mã QR điểm danh")
    buoi = st.selectbox("Chọn buổi học", ["Buổi 1", "Buổi 2", "Buổi 3", "Buổi 4", "Buổi 5", "Buổi 6"])

    if st.button("Tạo mã QR"):
        st.session_state["buoi"] = buoi

        # Tạo link QR: thêm sv=1 để ép vào chế độ SV khi mở trên điện thoại
        qr_data = f"https://qrlecturer.streamlit.app/?sv=1&buoi={urllib.parse.quote(buoi)}"

        qr = qrcode.make(qr_data)
        buf = io.BytesIO()
        qr.save(buf, format="PNG")
        buf.seek(0)
        img = Image.open(buf)

        st.image(img, caption="📱 Quét mã để điểm danh", width=250)
        st.write(f"🔗 Link: {qr_data}")

        # Đếm ngược 30 giây (tuỳ chọn)
        countdown = st.empty()
        for i in range(60, 0, -1):
            countdown.markdown(f"⏳ Thời gian còn lại: **{i} giây**")
            time.sleep(1)
        countdown.markdown("✅ Hết thời gian điểm danh")

    # Thống kê điểm danh
    if "buoi" in st.session_state:
        st.subheader("📊 Thống kê điểm danh")
        try:
            sheet = get_sheet()
            col = sheet.find(st.session_state["buoi"]).col
            data = sheet.col_values(col)[1:]  # bỏ header
            diem_danh = sum(1 for x in data if str(x).strip())
            vang = len(data) - diem_danh
            # giả định cột 3 là "Họ và Tên"
            ds_vang = [sheet.cell(i + 2, 3).value for i, x in enumerate(data) if not str(x).strip()]

            c1, c2 = st.columns(2)
            with c1: st.metric("✅ Đã điểm danh", diem_danh)
            with c2: st.metric("❌ Vắng mặt", vang)
            st.write("📋 Danh sách vắng:")
            st.dataframe(ds_vang)
        except Exception as e:
            st.error(f"❌ Lỗi khi lấy thống kê: {e}")

with tab_sv:
    st.subheader("📲 Nhập thông tin điểm danh (dành cho SV)")
    mssv = st.text_input("Nhập MSSV")
    hoten = st.text_input("Nhập họ và tên")
    buoi_sv = st.session_state.get("buoi", "Buổi 1")

    if st.button("Điểm danh"):
        try:
            sheet = get_sheet()
            col_buoi = sheet.find(buoi_sv).col
            cell_mssv = sheet.find(str(mssv).strip())
            hoten_sheet = sheet.cell(cell_mssv.row, sheet.find("Họ và Tên").col).value
            if normalize_name(hoten_sheet or "") != normalize_name(hoten):
                st.error("Họ tên không khớp với MSSV trong danh sách.")
            else:
                sheet.update_cell(cell_mssv.row, col_buoi, "✅")
                st.success("🎉 Điểm danh thành công!")
        except Exception as e:
            st.error(f"❌ Lỗi khi điểm danh: {e}")

