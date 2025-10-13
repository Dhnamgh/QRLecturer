import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
import qrcode
from PIL import Image
import io
import time
import urllib.parse
import re
import base64

# ===================== CẤU HÌNH GOOGLE SHEETS =====================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SHEET_KEY = "1P7SOGsmb2KwBX50MU1Y1iVCYtjTiU7F7jLqgp6Bl8Bo"  # <-- thay ID thật của bạn
WORKSHEET_NAME = "D25A"

@st.cache_resource
def _get_gspread_client():
    """
    Kết nối Google Sheets và tự động 'sửa' các lỗi thường gặp của private_key:
    - secrets lưu 1 dòng với \\n
    - base64 bị quấn dòng, thiếu '=' padding
    - khoảng trắng/ký tự lạ
    """
    cred = dict(st.secrets["google_service_account"])
    pk = cred.get("private_key", "")
    if not pk:
        raise RuntimeError("Secrets thiếu 'private_key'.")

    # 1️⃣ Thay \\n -> \n, chuẩn hóa dòng
    if "\\n" in pk:
        pk = pk.replace("\\n", "\n")
    pk = pk.replace("\r\n", "\n").replace("\r", "\n")

    header = "-----BEGIN PRIVATE KEY-----"
    footer = "-----END PRIVATE KEY-----"

    if header not in pk or footer not in pk:
        raise RuntimeError("private_key thiếu header/footer BEGIN/END PRIVATE KEY.")

    # 2️⃣ Lấy thân base64
    lines = [ln.strip() for ln in pk.split("\n")]
    try:
        h_idx = lines.index(header)
        f_idx = lines.index(footer)
    except ValueError:
        raise RuntimeError("Định dạng private_key không hợp lệ (không tìm thấy header/footer).")

    body_lines = [ln for ln in lines[h_idx + 1 : f_idx] if ln]
    body_raw = "".join(body_lines)
    body_raw = re.sub(r"[^A-Za-z0-9+/=]", "", body_raw)  # loại ký tự lạ

    # 3️⃣ Bổ sung padding
    rem = len(body_raw) % 4
    if rem != 0:
        body_raw += "=" * (4 - rem)

    # 4️⃣ Thử decode base64 (kiểm tra lỗi Short substrate / Incorrect padding)
    try:
        base64.b64decode(body_raw, validate=True)
    except Exception as e:
        svc = cred.get("client_email", "(không lấy được)")
        raise RuntimeError(
            f"❌ private_key trong secrets bị hỏng hoặc thiếu ký tự.\n"
            f"Hãy tạo key JSON mới và copy nguyên văn (không thêm ...).\n"
            f"Service Account: {svc}\nLỗi gốc: {e}"
        )

    # 5️⃣ Gộp lại PEM chuẩn
    pk_clean = header + "\n"
    for i in range(0, len(body_raw), 64):
        pk_clean += body_raw[i : i + 64] + "\n"
    pk_clean += footer + "\n"

    cred["private_key"] = pk_clean

    # 6️⃣ Tạo credentials
    creds = Credentials.from_service_account_info(cred, scopes=SCOPES)
    return gspread.authorize(creds)


def get_sheet():
    client = _get_gspread_client()
    ss = client.open_by_key(SHEET_KEY)
    return ss.worksheet(WORKSHEET_NAME)


# ===================== TIỆN ÍCH =====================
def get_query_params():
    """Lấy query params, tương thích bản Streamlit mới"""
    if hasattr(st, "query_params"):
        return dict(st.query_params)
    else:
        raw = st.experimental_get_query_params()
        return {k: (v[0] if isinstance(v, list) and v else v) for k, v in raw.items()}


def normalize_name(name: str):
    """Chuẩn hóa họ tên"""
    return " ".join(w.capitalize() for w in name.strip().split())


# ===================== GIAO DIỆN STREAMLIT =====================
st.set_page_config(page_title="QR Lecturer", layout="centered")
qp = get_query_params()

# Nếu URL có sv=1 hoặc buoi=... thì chỉ hiển thị form SV
student_only = (qp.get("sv") == "1") or ("buoi" in qp)

# ===================== MÀN HÌNH CHỈ SINH VIÊN =====================
if student_only:
    buoi_sv = qp.get("buoi", "Buổi 1")
    st.title("🎓 Điểm danh sinh viên")
    st.info(f"Bạn đang điểm danh cho **{buoi_sv}**")

    st.write("Mã số sinh viên: 51125", unsafe_allow_html=True)
    mssv_tail = st.text_input("Nhập 4 số cuối MSSV")
    mssv = "51125" + mssv_tail.strip()
    hoten = st.text_input("Nhập họ và tên")

    if st.button("✅ Xác nhận điểm danh"):
        if not mssv.strip().isdigit():
            st.warning("⚠️ MSSV phải là số.")
        elif not hoten.strip():
            st.warning("⚠️ Vui lòng nhập họ và tên.")
        else:
            try:
                sheet = get_sheet()
                col_buoi = sheet.find(buoi_sv).col
                cell_mssv = sheet.find(str(mssv).strip())
                hoten_sheet = sheet.cell(cell_mssv.row, sheet.find("Họ và Tên").col).value
                if normalize_name(hoten_sheet or "") != normalize_name(hoten):
                    st.error("❌ Họ tên không khớp với MSSV trong danh sách.")
                else:
                    sheet.update_cell(cell_mssv.row, col_buoi, "✅")
                    st.success("🎉 Điểm danh thành công!")
            except Exception as e:
                st.error(f"❌ Lỗi khi điểm danh: {e}")

    st.stop()

# ===================== MÀN HÌNH GIẢNG VIÊN =====================
st.title("📋 Hệ thống điểm danh QR")

tab_gv, tab_sv = st.tabs(["👨‍🏫 Giảng viên", "🎓 Sinh viên"])

# ---------- TAB GIẢNG VIÊN ----------
with tab_gv:
    st.subheader("📸 Tạo mã QR điểm danh")
    buoi = st.selectbox(
        "Chọn buổi học",
        ["Buổi 1", "Buổi 2", "Buổi 3", "Buổi 4", "Buổi 5", "Buổi 6"],
    )

    if st.button("Tạo mã QR"):
        st.session_state["buoi"] = buoi
        qr_data = f"https://qrlecturer.streamlit.app/?sv=1&buoi={urllib.parse.quote(buoi)}"

        qr = qrcode.make(qr_data)
        buf = io.BytesIO()
        qr.save(buf, format="PNG")
        buf.seek(0)
        img = Image.open(buf)

        st.image(img, caption="📱 Quét mã để điểm danh", width=250)
        st.write(f"🔗 Link: {qr_data}")

        countdown = st.empty()
        for i in range(60, 0, -1):  # 1 phút
            countdown.markdown(f"⏳ Thời gian còn lại: **{i} giây**")
            time.sleep(1)
        countdown.markdown("✅ Hết thời gian điểm danh")

    # ----------- Thống kê điểm danh -----------
    if "buoi" in st.session_state:
        st.subheader("📊 Thống kê điểm danh")
        try:
            sheet = get_sheet()
            col = sheet.find(st.session_state["buoi"]).col
            data = sheet.col_values(col)[1:]  # bỏ header
            diem_danh = sum(1 for x in data if str(x).strip())
            vang = len(data) - diem_danh
            ds_vang = [
                sheet.cell(i + 2, 3).value
                for i, x in enumerate(data)
                if not str(x).strip()
            ]

            c1, c2 = st.columns(2)
            with c1:
                st.metric("✅ Đã điểm danh", diem_danh)
            with c2:
                st.metric("❌ Vắng mặt", vang)
            st.write("📋 Danh sách vắng:")
            st.dataframe(ds_vang)
        except Exception as e:
            st.error(f"❌ Lỗi khi lấy thống kê: {e}")

# ---------- TAB SINH VIÊN (DỰ PHÒNG) ----------
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
            hoten_sheet = sheet.cell(
                cell_mssv.row, sheet.find("Họ và Tên").col
            ).value
            if normalize_name(hoten_sheet or "") != normalize_name(hoten):
                st.error("❌ Họ tên không khớp với MSSV trong danh sách.")
            else:
                sheet.update_cell(cell_mssv.row, col_buoi, "✅")
                st.success("🎉 Điểm danh thành công!")
        except Exception as e:
            st.error(f"❌ Lỗi khi điểm danh: {e}")
