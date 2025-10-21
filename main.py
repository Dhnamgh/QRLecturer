import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
import qrcode
from PIL import Image
import io
import time
import urllib.parse
import re, base64

# ===================== CẤU HÌNH GOOGLE SHEETS =====================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SHEET_KEY = "1P7SOGsmb2KwBX50MU1Y1iVCYtjTiU7F7jLqgp6Bl8Bo"  # <-- ID file Google Sheet
CLASS_EXCLUDE_KEYWORDS = {"likert", "mcq", "question", "test"}

@st.cache_resource
def _get_gspread_client():
    """Kết nối Google Sheets + tự 'vá' các lỗi định dạng private_key phổ biến."""
    cred = dict(st.secrets["google_service_account"])
    pk = cred.get("private_key", "")
    if not pk:
        raise RuntimeError("Secrets thiếu 'private_key'.")

    # Chuẩn hoá xuống dòng & làm sạch base64
    if "\\n" in pk:
        pk = pk.replace("\\n", "\n")
    pk = pk.replace("\r\n", "\n").replace("\r", "\n")

    header, footer = "-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----"
    if header not in pk or footer not in pk:
        raise RuntimeError("private_key thiếu BEGIN/END PRIVATE KEY.")

    lines = [ln.strip() for ln in pk.split("\n")]
    h_idx, f_idx = lines.index(header), lines.index(footer)
    body_raw = re.sub(r"[^A-Za-z0-9+/=]", "", "".join(ln for ln in lines[h_idx+1:f_idx] if ln))
    body_str = body_raw.replace("=", "")
    if not body_str:
        raise RuntimeError("private_key base64 rỗng sau khi làm sạch.")
    rem = len(body_str) % 4
    if rem != 0:
        body_str += "=" * (4 - rem)
    try:
        base64.b64decode(body_str, validate=True)
    except Exception as e:
        svc = cred.get("client_email", "(không lấy được)")
        raise RuntimeError(
            "❌ private_key trong secrets hỏng/thiếu ký tự. Tạo key JSON mới và copy nguyên văn.\n"
            f"Service Account: {svc}\nLỗi gốc: {e}"
        )

    pk_clean = header + "\n" + "\n".join(body_str[i:i+64] for i in range(0, len(body_str), 64)) + "\n" + footer + "\n"
    cred["private_key"] = pk_clean

    creds = Credentials.from_service_account_info(cred, scopes=SCOPES)
    return gspread.authorize(creds)

@st.cache_resource
def _get_spreadsheet():
    client = _get_gspread_client()
    return client.open_by_key(SHEET_KEY)

@st.cache_data(ttl=60)
def list_classes():
    """
    Trả về danh sách lớp (worksheet titles) đã lọc:
    - Loại bỏ các sheet có tên chứa: likert, mcq, question, test (không phân biệt hoa/thường)
    - Loại bỏ các sheet mặc định như 'Sheet1', 'Form Responses'
    """
    ss = _get_spreadsheet()
    titles = [ws.title for ws in ss.worksheets()]

    banned_exact = {"sheet1", "form responses", "form responses 1", "responses"}
    out = []
    for t in titles:
        t_norm = t.strip().lower()
        if not t_norm:
            continue
        if t_norm in banned_exact:
            continue
        if any(k in t_norm for k in CLASS_EXCLUDE_KEYWORDS):
            continue
        out.append(t)
    return out


# ===================== TIỆN ÍCH =====================
def get_query_params():
    """Lấy query params (Streamlit mới)."""
    if hasattr(st, "query_params"):
        return dict(st.query_params)
    raw = st.experimental_get_query_params()
    return {k: (v[0] if isinstance(v, list) and v else v) for k, v in raw.items()}

def normalize_name(name: str):
    return " ".join(w.capitalize() for w in name.strip().split())

# ===================== GIAO DIỆN =====================
st.set_page_config(page_title="QR Lecturer", layout="centered")
qp = get_query_params()

# Nếu URL có sv=1 hoặc có buoi/lop thì chỉ hiển thị form SV
student_only = (qp.get("sv") == "1") or ("buoi" in qp) or ("lop" in qp)

# ===================== MÀN HÌNH CHỈ SINH VIÊN =====================
if student_only:
    buoi_sv = qp.get("buoi", "Buổi 1")
    lop_sv = qp.get("lop")  # bắt buộc nên cố gắng lấy từ URL
    # fallback: nếu thiếu 'lop' trong URL, lấy lớp đầu tiên (đỡ lỗi)
    if not lop_sv:
        classes = list_classes()
        lop_sv = classes[0] if classes else None

    st.title("🎓 Điểm danh sinh viên")
    if not lop_sv:
        st.error("Không xác định được lớp. Hãy quét lại QR hoặc liên hệ giảng viên.")
        st.stop()
    st.info(f"Lớp: **{lop_sv}** • Buổi: **{buoi_sv}**")

    st.write("Mã số sinh viên: 51125", unsafe_allow_html=True)
    mssv_tail = st.text_input("Nhập 4 số cuối MSSV")
    mssv = "51125" + (mssv_tail or "").strip()
    hoten = st.text_input("Nhập họ và tên")

    if st.button("✅ Xác nhận điểm danh"):
        if not mssv.strip().isdigit():
            st.warning("⚠️ MSSV phải là số.")
        elif not hoten.strip():
            st.warning("⚠️ Vui lòng nhập họ và tên.")
        else:
            try:
                sheet = get_sheet(lop_sv)
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

# Lấy danh sách lớp động
try:
    classes = list_classes()
    if not classes:
        st.error("Không có lớp nào (worksheet) trong file Google Sheet.")
        st.stop()
except Exception as e:
    st.error(f"Không đọc được danh sách lớp: {e}")
    st.stop()

# Giảng viên chọn lớp + buổi
col1, col2 = st.columns([1.2, 1])
with col1:
    lop_chon = st.selectbox("Chọn lớp (worksheet)", classes, index=0)
with col2:
    buoi = st.selectbox("Chọn buổi học", ["Buổi 1", "Buổi 2", "Buổi 3", "Buổi 4", "Buổi 5", "Buổi 6"])

tab_gv, tab_sv = st.tabs(["👨‍🏫 Giảng viên", "🎓 Sinh viên"])

# ---------- TAB GIẢNG VIÊN ----------
with tab_gv:
    st.subheader("📸 Tạo mã QR điểm danh")
    st.caption("Mã QR sẽ chứa cả **tên lớp** và **buổi** để SV vào đúng form.")

    if st.button("Tạo mã QR"):
        # Lưu vào session để tab SV dự phòng dùng lại
        st.session_state["buoi"] = buoi
        st.session_state["lop"] = lop_chon

        # Nhúng cả lớp và buổi vào QR
        qr_data = f"https://qrlecturer.streamlit.app/?sv=1&lop={urllib.parse.quote(lop_chon)}&buoi={urllib.parse.quote(buoi)}"

        qr = qrcode.make(qr_data)
        buf = io.BytesIO()
        qr.save(buf, format="PNG")
        buf.seek(0)
        img = Image.open(buf)

        st.image(img, caption="📱 Quét mã để điểm danh", width=260)
        st.write(f"🔗 Link: {qr_data}")

        countdown = st.empty()
        for i in range(60, 0, -1):  # 1 phút
            countdown.markdown(f"⏳ Hiệu lực còn: **{i} giây**")
            time.sleep(1)
        countdown.markdown("✅ Hết thời gian điểm danh")

    # ----------- Thống kê điểm danh -----------
    if "lop" in st.session_state and "buoi" in st.session_state:
        st.subheader(f"📊 Thống kê: Lớp **{st.session_state['lop']}** • {st.session_state['buoi']}")
        try:
            sheet = get_sheet(st.session_state["lop"])
            col = sheet.find(st.session_state["buoi"]).col
            data = sheet.col_values(col)[1:]  # bỏ header
            diem_danh = sum(1 for x in data if str(x).strip())
            vang = len(data) - diem_danh
            # giả định cột 3 là "Họ và Tên"
            ds_vang = [
                sheet.cell(i + 2, 3).value
                for i, x in enumerate(data)
                if not str(x).strip()
            ]

            c1, c2 = st.columns(2)
            with c1: st.metric("✅ Đã điểm danh", diem_danh)
            with c2: st.metric("❌ Vắng mặt", vang)
            st.write("📋 Danh sách vắng:")
            st.dataframe(ds_vang, use_container_width=True)
        except Exception as e:
            st.error(f"❌ Lỗi khi lấy thống kê: {e}")
    else:
        st.info("Chưa có dữ liệu thống kê. Hãy tạo QR trước.")

# ---------- TAB SINH VIÊN (DỰ PHÒNG) ----------
with tab_sv:
    st.subheader("📲 Nhập thông tin điểm danh (dành cho SV)")
    mssv = st.text_input("Nhập MSSV")
    hoten = st.text_input("Nhập họ và tên")
    buoi_sv = st.session_state.get("buoi", buoi)
    lop_sv = st.session_state.get("lop", lop_chon)

    if st.button("Điểm danh"):
        try:
            sheet = get_sheet(lop_sv)
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
# ---------- FOOTER (bản quyền, căn giữa) ----------
st.markdown(
    """
    <style>
    .footer-dhn {
        position: fixed;
        left: 0; right: 0; bottom: 0;
        padding: 8px 16px;
        background: rgba(0,0,0,0.04);
        color: #444;
        font-size: 12px;
        text-align: center;
        z-index: 1000;
        border-top: 1px solid rgba(0,0,0,0.1);
        width: 100%;
    }
    </style>
    <div class="footer-dhn"> Copyright © 2025 Bản quyền thuộc về <strong>TS. Đào Hồng Nam - Đại học Y Dược Thành phố Hồ Chí Minh</strong></div>
    """,
    unsafe_allow_html=True
)


