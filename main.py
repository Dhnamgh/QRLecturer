import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
import qrcode
from PIL import Image
import io, time, urllib.parse, re, base64

# ===================== CẤU HÌNH =====================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SHEET_KEY = "1P7SOGsmb2KwBX50MU1Y1iVCYtjTiU7F7jLqgp6Bl8Bo"  # ID file của bạn
CLASS_EXCLUDE_KEYWORDS = {"likert", "mcq", "question", "test"}
WRAPPER_URL = "https://dhnamgh.github.io/PresenceAI/"

st.set_page_config(page_title="QR Lecturer", layout="wide")  # wide để bố cục thoáng

# ===================== KẾT NỐI GOOGLE SHEETS (kèm sửa key) =====================
@st.cache_resource
def _get_gspread_client():
    cred = dict(st.secrets["google_service_account"])
    pk = cred.get("private_key", "")
    if "\\n" in pk: pk = pk.replace("\\n", "\n")
    pk = pk.replace("\r\n", "\n").replace("\r", "\n")
    header, footer = "-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----"
    lines = [ln.strip() for ln in pk.split("\n") if ln.strip()]
    h, f = lines.index(header), lines.index(footer)
    body = re.sub(r"[^A-Za-z0-9+/=]", "", "".join(lines[h+1:f]))
    body = body.replace("=", "")
    if len(body) % 4: body += "=" * (4 - len(body) % 4)
    base64.b64decode(body, validate=True)  # validate sớm
    pk_clean = header + "\n" + "\n".join(body[i:i+64] for i in range(0, len(body), 64)) + "\n" + footer + "\n"
    cred["private_key"] = pk_clean
    creds = Credentials.from_service_account_info(cred, scopes=SCOPES)
    return gspread.authorize(creds)

@st.cache_resource
def _get_spreadsheet():
    return _get_gspread_client().open_by_key(SHEET_KEY)

@st.cache_data(ttl=60)
def list_classes():
    ss = _get_spreadsheet()
    titles = [ws.title for ws in ss.worksheets()]
    banned_exact = {"sheet1", "form responses", "form responses 1", "responses"}
    out = []
    for t in titles:
        tn = t.strip().lower()
        if not tn or tn in banned_exact: continue
        if any(k in tn for k in CLASS_EXCLUDE_KEYWORDS): continue
        out.append(t)
    return out

def get_sheet(lop: str):
    ss = _get_spreadsheet()
    return ss.worksheet(lop)

# ===================== TIỆN ÍCH =====================
def get_query_params():
    return dict(st.query_params) if hasattr(st, "query_params") else st.experimental_get_query_params()

def normalize_name(name: str):
    return " ".join(w.capitalize() for w in name.strip().split())
# ===================== CẬP NHẬT CẢ DẤU VÀ THỜI GIAN =====================
from datetime import datetime, timezone, timedelta

def _time_col_for(buoi: str) -> str:
    """Tự động xác định tên cột Thời gian tương ứng với Buổi"""
    digits = "".join(ch for ch in buoi if ch.isdigit())
    return f"Thời gian {digits}" if digits else "Thời gian"

def mark_present_with_time(sheet, buoi: str, row_idx: int):
    """Đánh dấu ✅ và ghi thời gian vào cột tương ứng"""
    col_diemdanh = sheet.find(buoi).col
    col_time = sheet.find(_time_col_for(buoi)).col

    # Giờ Việt Nam (UTC+7)
    vn_tz = timezone(timedelta(hours=7))
    now_str = datetime.now(vn_tz).strftime("%Y-%m-%d %H:%M:%S")

    sheet.update_cell(row_idx, col_diemdanh, "✅")
    sheet.update_cell(row_idx, col_time, now_str)

# ===================== URL PARAMS & CHẾ ĐỘ SV-ONLY =====================
qp = get_query_params()
student_only = (qp.get("sv") == "1") or ("buoi" in qp) or ("lop" in qp)

if student_only:
    buoi_sv = qp.get("buoi", "Buổi 1")
    lop_sv = qp.get("lop")
    if not lop_sv:
        classes = list_classes()
        lop_sv = classes[0] if classes else None

    st.title("🎓 Điểm danh sinh viên")
    if not lop_sv:
        st.error("Không xác định được lớp.")
        st.stop()
    st.info(f"Lớp: **{lop_sv}** • Buổi: **{buoi_sv}**")

    st.write("Mã số sinh viên: 51125")
    mssv_tail = st.text_input("Nhập 4 số cuối MSSV")
    mssv = "51125" + (mssv_tail or "").strip()
    hoten = st.text_input("Nhập họ và tên")

    if st.button("✅ Xác nhận điểm danh"):
        try:
            sheet = get_sheet(lop_sv)
            col_buoi = sheet.find(buoi_sv).col
            cell_mssv = sheet.find(str(mssv).strip())
            hoten_sheet = sheet.cell(cell_mssv.row, sheet.find("Họ và Tên").col).value
            if normalize_name(hoten_sheet or "") != normalize_name(hoten):
                st.error("Họ tên không khớp với MSSV trong danh sách.")
            else:
                mark_present_with_time(sheet, buoi_sv, cell_mssv.row)
                st.success("Đã điểm danh!")
        except Exception as e:
            st.error(f"Lỗi khi điểm danh: {e}")
    st.stop()

# ===================== SIDEBAR: CHỌN CHẾ ĐỘ & ĐĂNG NHẬP GV =====================
st.sidebar.title("Điều hướng")
mode = st.sidebar.radio("Chọn chế độ", ["👨‍🏫 Giảng viên", "🎓 Sinh viên"], index=0)

# Đăng nhập giảng viên
def gv_authenticated() -> bool:
    ok = st.session_state.get("gv_auth_ok", False)
    if ok: return True
    st.sidebar.subheader("Đăng nhập giảng viên")
    pwd = st.sidebar.text_input("Mật khẩu", type="password")
    if st.sidebar.button("Đăng nhập"):
        admin_pw = st.secrets.get("ADMIN_PASSWORD", "")
        if admin_pw and pwd == admin_pw:
            st.session_state["gv_auth_ok"] = True
            st.sidebar.success("Đăng nhập thành công.")
            return True
        else:
            st.sidebar.error("Sai mật khẩu.")
    return False

# ===================== GIAO DIỆN CHÍNH =====================
st.title("🧾 Hệ thống điểm danh QR")

# chọn lớp/buổi (dùng cho cả 2 chế độ)
try:
    classes = list_classes()
    if not classes:
        st.error("Chưa có worksheet lớp trong file.")
        st.stop()
except Exception as e:
    st.error(f"Không đọc được danh sách lớp: {e}")
    st.stop()

col1, col2 = st.columns([1.2, 1])
with col1:
    lop_chon = st.selectbox("Chọn lớp (worksheet)", classes, index=0)
with col2:
    buoi = st.selectbox("Chọn buổi học", ["Buổi 1", "Buổi 2", "Buổi 3", "Buổi 4", "Buổi 5", "Buổi 6"])

# ---------- CHẾ ĐỘ GIẢNG VIÊN ----------
if mode == "👨‍🏫 Giảng viên":
    if not gv_authenticated():
        st.warning("Vui lòng đăng nhập để dùng chức năng giảng viên.")
        st.stop()

    st.subheader("📸 Tạo mã QR điểm danh")
    st.caption("Mã QR sẽ chứa tên lớp và buổi để SV vào đúng form.")
    if st.button("Tạo mã QR"):
        st.session_state["lop"] = lop_chon
        st.session_state["buoi"] = buoi
        # Trỏ về trang bọc (ở lại trang gốc khi SV quét)
        qr_link = f"{WRAPPER_URL}?sv=1&lop={urllib.parse.quote(lop_chon)}&buoi={urllib.parse.quote(buoi)}"

        img_qr = qrcode.make(qr_link)
        buf = io.BytesIO(); img_qr.save(buf, format="PNG"); buf.seek(0)
        st.image(Image.open(buf), caption="Quét để điểm danh", width=260)
        st.code(qr_link, language="text")

        t = st.empty()
        for i in range(60, 0, -1):
            t.markdown(f"⏳ Hiệu lực còn: **{i} giây**")
            time.sleep(1)
        t.markdown("Đã hết thời gian hiệu lực.")

    # Thống kê
    if "lop" in st.session_state and "buoi" in st.session_state:
        st.subheader(f"📊 Thống kê: Lớp **{st.session_state['lop']}** • {st.session_state['buoi']}")
        try:
            sheet = get_sheet(st.session_state["lop"])
            col = sheet.find(st.session_state["buoi"]).col
            data = sheet.col_values(col)[1:]
            dd = sum(1 for x in data if str(x).strip())
            vang = len(data) - dd
            ds_vang = [sheet.cell(i+2, 3).value for i, x in enumerate(data) if not str(x).strip()]
            c1, c2 = st.columns(2)
            with c1: st.metric("Đã điểm danh", dd)
            with c2: st.metric("Vắng", vang)
            st.write("Danh sách vắng:")
            st.dataframe(ds_vang, use_container_width=True)
        except Exception as e:
            st.error(f"Lỗi khi lấy thống kê: {e}")

# ---------- CHẾ ĐỘ SINH VIÊN ----------
else:
    st.subheader("📲 Nhập thông tin điểm danh (SV)")
    mssv = st.text_input("Nhập MSSV")
    hoten = st.text_input("Nhập họ và tên")
    if st.button("Điểm danh"):
        try:
            sheet = get_sheet(lop_chon)
            col_buoi = sheet.find(buoi).col
            cell_mssv = sheet.find(str(mssv).strip())
            hoten_sheet = sheet.cell(cell_mssv.row, sheet.find("Họ và Tên").col).value
            if normalize_name(hoten_sheet or "") != normalize_name(hoten):
                st.error("Họ tên không khớp với MSSV.")
            else:
                mark_present_with_time(sheet, buoi, cell_mssv.row)
                st.success("Đã điểm danh!")
        except Exception as e:
            st.error(f"Lỗi khi điểm danh: {e}")

# ---------- FOOTER  ----------

st.markdown("---")
st.markdown("© Bản quyền thuộc về TS. Đào Hồng Nam - Đại học Y Dược Thành phố Hồ Chí Minh.")





