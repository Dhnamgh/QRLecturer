# main.py — bản tối giản, đúng luồng cũ + tối ưu hiệu năng + secrets

import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
import qrcode
from PIL import Image
import io, time, urllib.parse, re, random, unicodedata
from datetime import datetime, timezone, timedelta
import pandas as pd
import altair as alt
import threading, requests

# ===================== THIẾT LẬP =====================
st.set_page_config(page_title="APP Điểm Danh", layout="wide")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# ---------- Đọc secrets (chỉ những gì cần) ----------
def _must(key, hint=""):
    v = st.secrets.get(key)
    if v in (None, ""):
        st.error(f"Thiếu `{key}` trong secrets. {hint}"); st.stop()
    return v.strip() if isinstance(v, str) else v

SHEET_KEY        = _must("SHEET_KEY", "ID giữa /d/ và /edit của Google Sheet.")
WRAPPER_URL      = _must("WRAPPER_URL", "VD: https://<github-pages>/")
ADMIN_PASSWORD   = _must("ADMIN_PASSWORD", "Mật khẩu GV.")
STUDENT_PASSWORD = st.secrets.get("STUDENT_PASSWORD", "")

APP_URL               = st.secrets.get("APP_URL", "")
HOST_PROVIDER         = st.secrets.get("HOST_PROVIDER", "streamlit").lower()
HOST_IDLE_TIMEOUT_MIN = int(st.secrets.get("HOST_IDLE_TIMEOUT_MIN", 720))
KEEPALIVE_ENABLED     = bool(st.secrets.get("KEEPALIVE_ENABLED", True))
SESSION_PREFIX        = st.secrets.get("SESSION_PREFIX", "51125")
USE_APPEND_LOG        = bool(st.secrets.get("USE_APPEND_LOG", False))

if "google_service_account" not in st.secrets:
    st.error("Thiếu [google_service_account] trong secrets."); st.stop()

# ---------- Kết nối Google Sheets ----------
@st.cache_resource
def gs():
    info = dict(st.secrets["google_service_account"])
    pk = info.get("private_key","")
    if "\\n" in pk: pk = pk.replace("\\n","\n")
    info["private_key"] = pk
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)

@st.cache_resource
def ss():
    return gs().open_by_key(SHEET_KEY)

def ws(title: str):
    return ss().worksheet(title)

# ===================== TIỆN ÍCH NHANH & BỀN =====================
def _strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(ch for ch in s if not unicodedata.combining(ch))

def _norm_header(s: str) -> str:
    s = _strip_accents(s).lower().strip()
    return re.sub(r"[\s\-_]+", "", s)

@st.cache_data(ttl=300)
def header_map(sheet_title: str) -> dict:
    row1 = ws(sheet_title).row_values(1)
    return { _norm_header(v): i for i, v in enumerate(row1, start=1) if v }

def _col_by_names(hmap: dict, candidates) -> int|None:
    for c in candidates:
        k = _norm_header(c)
        if k in hmap: return hmap[k]
    return None

def _buoi_num(buoi_label: str) -> int|None:
    m = re.search(r"(\d+)", buoi_label or "")
    return int(m.group(1)) if m else None

def col_indices(sheet_title: str, buoi_label: str) -> dict:
    """
    Khớp đúng với tiêu đề bạn đang dùng: 'MSSV', 'Họ và Tên', 'Buổi 1', 'Thời gian 1'
    (đồng thời chấp nhận các biến thể không dấu/không khoảng).
    """
    h = header_map(sheet_title)
    mssv_col  = _col_by_names(h, ["MSSV","MSV","Ma SV","MaSV","Mã số SV","Ma so SV"])
    hoten_col = _col_by_names(h, ["Họ và Tên","Họ tên","Ho va Ten","HoTen"])

    n = _buoi_num(buoi_label)
    buoi_candidates = [buoi_label, _strip_accents(buoi_label)]
    if n: buoi_candidates += [f"Buoi {n}", f"Buoi{n}"]
    diem_col = _col_by_names(h, buoi_candidates)

    time_candidates = []
    if n:
        time_candidates += [f"Thời gian {n}", f"Thoi gian {n}", f"thoigian{n}", f"Thời gian Buổi {n}", f"Thoi gian Buoi {n}"]
    time_candidates += [f"Thời gian {buoi_label}", f"Thoi gian {buoi_label}"]
    time_col = _col_by_names(h, time_candidates)

    if not all([mssv_col, hoten_col, diem_col, time_col]):
        missing = []
        if not mssv_col:  missing.append("MSSV")
        if not hoten_col: missing.append("Họ và Tên")
        if not diem_col:  missing.append(buoi_label)
        if not time_col:  missing.append(f"Thời gian {n or buoi_label}")
        st.error("Không tìm thấy cột: " + ", ".join(missing))
        st.info("Tiêu đề hiện có (chuẩn hoá): " + ", ".join(sorted(header_map(sheet_title).keys())))
        st.stop()
    return {"mssv": mssv_col, "hoten": hoten_col, "diem": diem_col, "time": time_col}

@st.cache_data(ttl=120)
def mssv_map(sheet_title: str) -> dict:
    h = header_map(sheet_title)
    mssv_col = _col_by_names(h, ["MSSV","MSV","Ma SV","MaSV","Mã số SV","Ma so SV"])
    values = ws(sheet_title).col_values(mssv_col)
    return { str(v).strip(): i for i, v in enumerate(values, start=1) if str(v).strip() }

def find_row(sheet_title: str, mssv: str) -> int|None:
    return mssv_map(sheet_title).get(str(mssv).strip())

def A1(col: int, row: int) -> str:
    s = ""
    while col > 0:
        col, r = divmod(col-1, 26)
        s = chr(65+r) + s
    return f"{s}{row}"

def with_retry(fn, retries=5):
    for i in range(retries):
        try: return fn()
        except Exception:
            if i==retries-1: raise
            time.sleep((2**i)*0.4 + random.random()*0.25)

def mark_present(sheet_title: str, buoi_label: str, row_idx: int):
    """Ghi ✅ & timestamp trong **1 request**."""
    w = ws(sheet_title)
    idx = col_indices(sheet_title, buoi_label)
    now = datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S")
    rng = f"{A1(idx['diem'],row_idx)}:{A1(idx['time'],row_idx)}"
    with_retry(lambda: w.update(rng, [["✅", now]], value_input_option="RAW"))

def append_log(sheet_title: str, lop: str, buoi: str, mssv: str, hoten: str):
    if not USE_APPEND_LOG: return
    try:
        log_ws = ss().worksheet("Checkins")
    except Exception:
        log_ws = ss().add_worksheet("Checkins", rows=1000, cols=6)
        log_ws.update("A1:F1", [["timestamp","lop","buoi","mssv","hoten","status"]])
    now = datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S")
    with_retry(lambda: log_ws.append_row([now, lop, buoi, mssv, hoten, "present"], value_input_option="RAW"))

# ===================== XÁC THỰC =====================
def gv_login_ok() -> bool:
    if st.session_state.get("gv_ok"):
        st.sidebar.success("Đã đăng nhập (GV)")
        if st.sidebar.button("Đăng xuất GV", key="logout_gv"):
            st.session_state["gv_ok"]=False; st.rerun()
        return True
    st.sidebar.subheader("Đăng nhập giảng viên")
    pwd = st.sidebar.text_input("Mật khẩu GV", type="password", key="gv_pwd")
    if st.sidebar.button("Đăng nhập GV", key="login_gv"):
        if pwd==ADMIN_PASSWORD:
            st.session_state["gv_ok"]=True; st.rerun()
        else:
            st.sidebar.error("Sai mật khẩu.")
    return False

def sv_login_ok() -> bool:
    if not STUDENT_PASSWORD: return True
    if st.session_state.get("sv_ok"):
        st.success("Đã đăng nhập (SV)")
        if st.button("Đăng xuất SV", key="logout_sv"):
            st.session_state["sv_ok"]=False; st.rerun()
        return True
    st.subheader("🔐 Đăng nhập Sinh viên")
    pwd = st.text_input("Mật khẩu SV", type="password", key="sv_pwd")
    if st.button("Vào trang Sinh viên", key="login_sv"):
        if pwd==STUDENT_PASSWORD:
            st.session_state["sv_ok"]=True; st.rerun()
        else:
            st.error("Sai mật khẩu.")
    return False

# ===================== UI =====================
st.title("🧾 Hệ thống điểm danh QR")

# ——— Luồng SV chỉ qua QR ———
params = st.query_params
if params.get("sv")=="1" and params.get("lop") and params.get("buoi"):
    # KHÔNG có tab SV ở ngoài. Chỉ form này.
    lop_qr  = params["lop"]
    buoi_qr = params["buoi"]
    if not sv_login_ok(): st.stop()

    st.subheader("📲 Điểm danh Sinh viên")
    st.info(f"Lớp: **{lop_qr}** • {buoi_qr}")
    st.write(f"MSSV có tiền tố: **{SESSION_PREFIX}**")

    mssv4  = st.text_input("Nhập 4 số cuối MSSV", key="mssv_tail_qr")
    hoten  = st.text_input("Nhập họ và tên", key="hoten_qr")

    if st.button("Xác nhận điểm danh", key="btn_checkin_qr", use_container_width=True):
        try:
            mssv = SESSION_PREFIX + (mssv4 or "").strip()
            row  = find_row(lop_qr, mssv)
            if not row:
                st.error("❌ Không tìm thấy MSSV trong danh sách lớp.")
            else:
                idx = col_indices(lop_qr, buoi_qr)
                hoten_sheet = with_retry(lambda: ws(lop_qr).cell(row, idx["hoten"]).value)
                if unicodedata.normalize("NFKC", (hoten_sheet or "").strip()) \
                   != unicodedata.normalize("NFKC", (hoten or "").strip()):
                    st.error("❌ Họ tên không khớp với MSSV.")
                else:
                    append_log(lop_qr, lop_qr, buoi_qr, mssv, hoten)
                    if not USE_APPEND_LOG:
                        mark_present(lop_qr, buoi_qr, row)
                    st.success("🎉 Điểm danh thành công!")
        except Exception as e:
            st.error(f"Lỗi điểm danh: {e}")
    st.stop()   # chặn phần GV bên dưới

# ——— Giao diện GIẢNG VIÊN (duy nhất ở ngoài) ———
st.sidebar.title("Giảng viên")
if not gv_login_ok(): st.stop()

# chọn lớp/buổi (để tạo QR & thống kê)
@st.cache_data(ttl=60)
def class_names():
    names=[]
    for w in ss().worksheets():
        t=(w.title or "").strip()
        if t and not any(k in t.lower() for k in {"likert","mcq","question","test"}):
            names.append(t)
    return names

classes = class_names()
if not classes:
    st.warning("Không tìm thấy lớp trong spreadsheet."); st.stop()

lop = st.selectbox("Chọn lớp", classes, key="class_gv")
buoi = st.selectbox("Chọn buổi", [f"Buổi {i}" for i in range(1,13)], key="buoi_gv")

tab_qr, tab_stats = st.tabs(["🧾 Tạo mã QR", "📊 Thống kê"])

with tab_qr:
    st.subheader("Tạo mã QR điểm danh")
    if st.button("Tạo mã QR mới", key="btn_make_qr", use_container_width=True):
        link = f"{WRAPPER_URL}?sv=1&lop={urllib.parse.quote(lop)}&buoi={urllib.parse.quote(buoi)}"
        img  = qrcode.make(link)
        buf = io.BytesIO(); img.save(buf, format="PNG")
        img_obj = Image.open(io.BytesIO(buf.getvalue()))
        # căn giữa QR
        c1,c2,c3 = st.columns([1,1,1])
        with c2:
            st.image(img_obj, width=320)  # không hiển thị URL/địa chỉ

        # đồng hồ đếm (UI)
        t=st.empty()
        for i in range(60,0,-1):
            t.markdown(f"⏳ Hiệu lực còn: **{i} giây**"); time.sleep(1)
        t.markdown("✅ Hết thời gian hiệu lực.")

with tab_stats:
    st.subheader(f"Thống kê: {lop} • {buoi}")
    try:
        data = ws(lop).get_all_values()
        if not data:
            st.info("Chưa có dữ liệu.")
        else:
            df = pd.DataFrame(data[1:], columns=data[0])
            if buoi in df.columns:
                present = (df[buoi].fillna("")=="✅").sum()
                total   = len(df); absent = total - present
                c1,c2,c3 = st.columns(3)
                c1.metric("Đi học", present); c2.metric("Vắng", absent); c3.metric("Tổng", total)
                # timeline (nếu có cột Thời gian X)
                tcol = f"Thời gian {re.search(r'(\\d+)', buoi).group(1)}"
                if tcol in df.columns:
                    ts = pd.to_datetime(df[tcol], errors="coerce").dropna().dt.floor("T").value_counts().sort_index()
                    if not ts.empty:
                        chart_df = pd.DataFrame({"time": ts.index, "count": ts.values})
                        st.altair_chart(alt.Chart(chart_df).mark_line(point=True).encode(x="time:T", y="count:Q"),
                                        use_container_width=True)
            else:
                st.info("Chưa tạo cột cho buổi này.")
    except Exception as e:
        st.error(f"Lỗi thống kê: {e}")

# ===================== KEEP-ALIVE (nhẹ) =====================
def _interval():
    buf = 180 if HOST_PROVIDER=="streamlit" else 120
    return max(60, HOST_IDLE_TIMEOUT_MIN*60 - buf - random.randint(0,30))

def _ping():
    if not KEEPALIVE_ENABLED: return
    url = (APP_URL or "").strip()
    if not url: return
    try: requests.get(url, timeout=6)
    except Exception: pass
    while True:
        time.sleep(_interval())
        try: requests.get(url, timeout=6)
        except Exception: pass

if "ka" not in st.session_state:
    threading.Thread(target=_ping, daemon=True).start()
    st.session_state["ka"] = True

# ---------- FOOTER  ----------

st.markdown("---")
st.markdown("© Bản quyền thuộc về TS. Đào Hồng Nam - Đại học Y Dược Thành phố Hồ Chí Minh.")

















