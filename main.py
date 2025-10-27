# main.py
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
import qrcode
from PIL import Image
import io, time, urllib.parse, re, random
import pandas as pd
import altair as alt
from datetime import datetime, timezone, timedelta
import unicodedata
import threading, requests

# ===================== THIẾT LẬP =====================
st.set_page_config(page_title="APP Điểm Danh", layout="wide")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# --- ĐỌC SECRETS AN TOÀN & CHẨN ĐOÁN ---
def _must_get_secret(key: str, hint: str = ""):
    val = st.secrets.get(key)
    if val in (None, ""):
        st.error(
            f"Thiếu khóa secret: `{key}`.\n"
            + (hint or "Vào App settings → Secrets, thêm khóa này (TOML).")
        )
        try:
            st.info("Các khóa đang nạp: " + ", ".join(sorted(st.secrets.to_dict().keys())))
        except Exception:
            pass
        st.stop()
    if isinstance(val, str):
        val = val.strip()
    return val

SHEET_KEY        = _must_get_secret(
    "SHEET_KEY",
    "ID nằm giữa '/d/' và '/edit' trong URL Google Sheets. Ví dụ: SHEET_KEY=\"1abcDEF...\""
)
WRAPPER_URL      = _must_get_secret("WRAPPER_URL", "VD: https://<your-gh-pages>/")
ADMIN_PASSWORD   = _must_get_secret("ADMIN_PASSWORD", "Mật khẩu GV trong secrets.")

SESSION_PREFIX   = st.secrets.get("SESSION_PREFIX", "51125")
STUDENT_PASSWORD = st.secrets.get("STUDENT_PASSWORD", "")        # rỗng -> không yêu cầu SV login

# Hiệu năng
USE_APPEND_LOG   = bool(st.secrets.get("USE_APPEND_LOG", False))  # True -> ghi vào sheet Checkins (append-only)

# Keep-alive
APP_URL               = st.secrets.get("APP_URL", "")
HOST_PROVIDER         = st.secrets.get("HOST_PROVIDER", "streamlit").lower()
HOST_IDLE_TIMEOUT_MIN = int(st.secrets.get("HOST_IDLE_TIMEOUT_MIN", 720))
KEEPALIVE_ENABLED     = bool(st.secrets.get("KEEPALIVE_ENABLED", True))

# Loại trừ sheet phụ
CLASS_EXCLUDE_KEYWORDS = {"likert", "mcq", "question", "test"}

# ==== Kiểm tra service account tối thiểu ====
if "google_service_account" not in st.secrets:
    st.error("Thiếu khối [google_service_account] trong secrets."); st.stop()
for k in ("private_key", "client_email", "token_uri"):
    if not st.secrets["google_service_account"].get(k):
        st.error(f"Thiếu trường `{k}` trong [google_service_account]."); st.stop()

# ===================== KẾT NỐI SHEETS =====================
@st.cache_resource
def _get_gspread_client():
    cred = dict(st.secrets["google_service_account"])
    # chuẩn hoá private_key nếu lỡ dùng \n
    pk = cred.get("private_key", "")
    if "\\n" in pk:
        pk = pk.replace("\\n", "\n")
    cred["private_key"] = pk
    creds = Credentials.from_service_account_info(cred, scopes=SCOPES)
    return gspread.authorize(creds)

@st.cache_resource
def _get_spreadsheet():
    return _get_gspread_client().open_by_key(SHEET_KEY)

def list_classes():
    ss = _get_spreadsheet()
    names = []
    for ws in ss.worksheets():
        t = (ws.title or "").strip()
        if not t:
            continue
        if any(k in t.lower() for k in CLASS_EXCLUDE_KEYWORDS):
            continue
        names.append(t)
    return names

def get_sheet(title: str):
    return _get_spreadsheet().worksheet(title)

# ===================== TIỆN ÍCH =====================
def _vn_norm(s: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", (s or "").strip()).lower())

def normalize_name(s: str) -> str:
    return unicodedata.normalize("NFKC", (s or "").strip())

def _time_col_for(buoi: str) -> str:
    return f"Thời gian {buoi}"

def _colnum_to_a1(c: int) -> str:
    s = ""
    while c > 0:
        c, r = divmod(c - 1, 26)
        s = chr(65 + r) + s
    return s

def _with_retry(fn, retries=5):
    for i in range(retries):
        try:
            return fn()
        except Exception:
            if i == retries - 1:
                raise
            time.sleep((2 ** i) * 0.4 + random.random() * 0.25)

@st.cache_data(ttl=60)
def _get_col_indices(sheet_key: str, ws_title: str, buoi: str) -> dict:
    ws = _get_spreadsheet().worksheet(ws_title)
    return {
        "mssv": _with_retry(lambda: ws.find("MSSV").col),
        "hoten": _with_retry(lambda: ws.find("Họ và Tên").col),
        "diem": _with_retry(lambda: ws.find(buoi).col),
        "time": _with_retry(lambda: ws.find(_time_col_for(buoi)).col),
    }

@st.cache_data(ttl=60)
def _get_mssv_map(sheet_key: str, ws_title: str) -> dict:
    ws = _get_spreadsheet().worksheet(ws_title)
    col = _with_retry(lambda: ws.find("MSSV").col)
    vals = _with_retry(lambda: ws.col_values(col))
    m = {}
    for i, v in enumerate(vals, start=1):
        vv = str(v).strip()
        if vv:
            m[vv] = i
    return m

def sheet_to_df(sheet) -> pd.DataFrame:
    vals = _with_retry(lambda: sheet.get_all_values())
    if not vals:
        return pd.DataFrame()
    return pd.DataFrame(vals[1:], columns=vals[0])

# ===================== GHI ĐIỂM DANH =====================
def mark_present_with_time(sheet, buoi: str, row_idx: int):
    """
    Cập nhật dấu ✅ và timestamp trong 1 request -> giảm nghẽn khi đông SV.
    """
    idx = _get_col_indices(SHEET_KEY, sheet.title, buoi)
    c_diem, c_time = idx["diem"], idx["time"]
    now_str = datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S")
    a1 = f"{_colnum_to_a1(c_diem)}{row_idx}:{_colnum_to_a1(c_time)}{row_idx}"
    _with_retry(lambda: sheet.update(a1, [["✅", now_str]], value_input_option="RAW"))

def append_checkin_log(ss, lop: str, buoi: str, mssv: str, hoten: str):
    """
    Tùy chọn: ghi log vào sheet 'Checkins' (append-only).
    """
    try:
        ws = ss.worksheet("Checkins")
    except Exception:
        ws = ss.add_worksheet(title="Checkins", rows=1000, cols=6)
        ws.update("A1:F1", [["timestamp", "lop", "buoi", "mssv", "hoten", "status"]])
    now_str = datetime.now(timezone(timedelta(hours=7))).strftime("%Y-%m-%d %H:%M:%S")
    _with_retry(lambda: ws.append_row([now_str, lop, buoi, mssv, hoten, "present"], value_input_option="RAW"))

def find_row_by_mssv(sheet, mssv: str):
    return _get_mssv_map(SHEET_KEY, sheet.title).get(str(mssv).strip())

# ===================== THỐNG KÊ & TRỢ LÝ =====================
def group_stats_for_buoi(df: pd.DataFrame, buoi: str):
    if df.empty:
        return 0, 0, 0
    p = (df[buoi].fillna("") == "✅").sum()
    t = len(df)
    return p, t - p, t

def attendance_counts(df: pd.DataFrame, buoi: str):
    if df.empty:
        return pd.Series(dtype=int)
    return df[buoi].fillna("").apply(lambda x: "Đi học" if x == "✅" else "Vắng").value_counts()

def parse_time_series(df: pd.DataFrame, buoi: str):
    col = _time_col_for(buoi)
    if df.empty or col not in df.columns:
        return pd.DataFrame(columns=["time", "count"])
    ts = pd.to_datetime(df[col], errors="coerce").dropna().dt.floor("T").value_counts().sort_index()
    return pd.DataFrame({"time": ts.index, "count": ts.values})

def infer_buoi_from_text(s: str) -> str | None:
    s = _vn_norm(s)
    m = re.search(r"bu(?:oi|ổi)\s*(\d+)", s)
    return f"Buổi {int(m.group(1))}" if m else None

def classify_intent(s: str) -> str:
    s = _vn_norm(s)
    if "vắng" in s or "absent" in s:
        return "absent_list"
    if "đi học" in s or "present" in s:
        return "present_count"
    return "rate_overall"

# ===================== XÁC THỰC =====================
def gv_authenticated() -> bool:
    # Đã đăng nhập -> hiện nút Đăng xuất ở sidebar
    if st.session_state.get("gv_auth_ok"):
        st.sidebar.success("Đã đăng nhập (GV).")
        if st.sidebar.button("Đăng xuất GV", key="btn_logout_gv"):
            st.session_state["gv_auth_ok"] = False
            st.rerun()
        return True

    # Form đăng nhập
    st.sidebar.subheader("Đăng nhập giảng viên")
    pwd = st.sidebar.text_input("Mật khẩu GV", type="password", key="gv_pwd")
    if st.sidebar.button("Đăng nhập GV", key="btn_login_gv"):
        if pwd == ADMIN_PASSWORD:
            st.session_state["gv_auth_ok"] = True
            st.rerun()
        else:
            st.sidebar.error("Sai mật khẩu.")
    return False

def sv_authenticated() -> bool:
    """
    Đăng nhập SV bằng STUDENT_PASSWORD (nếu có). Nếu rỗng -> vào thẳng.
    """
    if not STUDENT_PASSWORD:
        return True

    if st.session_state.get("sv_auth_ok"):
        st.success("Đã đăng nhập (SV).")
        if st.button("Đăng xuất SV", key="btn_logout_sv"):
            st.session_state["sv_auth_ok"] = False
            st.rerun()
        return True

    st.subheader("🔐 Đăng nhập Sinh viên")
    pwd = st.text_input("Mật khẩu SV", type="password", key="sv_pwd")
    if st.button("Vào trang Sinh viên", key="btn_login_sv"):
        if pwd == STUDENT_PASSWORD:
            st.session_state["sv_auth_ok"] = True
            st.rerun()
        else:
            st.error("Sai mật khẩu.")
    return False

# ===================== GIAO DIỆN =====================
st.title("🧾 Hệ thống điểm danh QR")

# Nhận query params S/ V
params = st.query_params
sv_param = params.get("sv")
lop_qr = params.get("lop", "")
buoi_qr = params.get("buoi", "")

# Điều hướng mặc định: nếu có sv=1 trong URL → bật trang Sinh viên
default_index = 1 if sv_param == "1" else 0

st.sidebar.title("Điều hướng")
mode = st.sidebar.radio("Chọn chế độ", ["👨‍🏫 Giảng viên", "🎓 Sinh viên"], index=default_index, key="mode_radio")

classes = list_classes()
if not classes:
    st.warning("Không tìm thấy lớp hợp lệ trong Spreadsheet.")
    st.stop()

# ---------- GIẢNG VIÊN ----------
if mode == "👨‍🏫 Giảng viên":
    if not gv_authenticated():
        st.stop()

    # Lựa chọn lớp/buổi chỉ hiển thị ở GV
    lop_chon = st.selectbox("Chọn lớp", classes, key="class_global")
    buoi = st.selectbox("Chọn buổi", [f"Buổi {i}" for i in range(1, 13)], key="buoi_global")

    tab_qr, tab_stats, tab_ai = st.tabs(["🧾 Tạo mã QR", "📊 Thống kê", "🤖 Trợ lý lớp"])

    # --- TẠO MÃ QR ---
    with tab_qr:
        st.subheader("Tạo mã QR điểm danh")
        if st.button("Tạo mã QR mới", use_container_width=True, key="btn_make_qr"):
            st.session_state["lop"] = lop_chon
            st.session_state["buoi"] = buoi
            link = f"{WRAPPER_URL}?sv=1&lop={urllib.parse.quote(lop_chon)}&buoi={urllib.parse.quote(buoi)}"

            # Tạo ảnh QR
            img = qrcode.make(link)
            buf = io.BytesIO(); img.save(buf, format="PNG")
            img_obj = Image.open(io.BytesIO(buf.getvalue()))

            # CĂN GIỮA ảnh QR bằng 3 cột
            c1, c2, c3 = st.columns([1, 1, 1])
            with c2:
                st.image(img_obj, caption=None, width=320)  # không hiển thị link

            # đếm ngược hiển thị (UI)
            t = st.empty()
            for i in range(60, 0, -1):
                t.markdown(f"⏳ Hiệu lực còn: **{i} giây**"); time.sleep(1)
            t.markdown("✅ Hết thời gian hiệu lực.")

    # --- THỐNG KÊ ---
    with tab_stats:
        st.subheader(f"Thống kê: {lop_chon} • {buoi}")
        try:
            sheet = get_sheet(lop_chon)
            df = sheet_to_df(sheet)
            if df.empty:
                st.info("Chưa có dữ liệu.")
            else:
                p, a, t = group_stats_for_buoi(df, buoi)
                c1, c2, c3 = st.columns(3)
                c1.metric("Đi học", p); c2.metric("Vắng", a); c3.metric("Tổng", t)

                st.write("### Biểu đồ tỷ lệ")
                cnt = attendance_counts(df, buoi)
                chart = alt.Chart(cnt.reset_index(names="Trạng thái").rename(columns={"count": "Số lượng"}))\
                        .mark_bar().encode(x="Trạng thái", y="Số lượng")
                st.altair_chart(chart, use_container_width=True)

                st.write("### Dòng thời gian điểm danh")
                ts = parse_time_series(df, buoi)
                if not ts.empty:
                    line = alt.Chart(ts).mark_line(point=True).encode(x="time:T", y="count:Q")
                    st.altair_chart(line, use_container_width=True)

                st.write("### Danh sách vắng")
                if buoi in df.columns:
                    v = df[df[buoi].fillna("") != "✅"][["MSSV", "Họ và Tên"]]
                    st.dataframe(v, use_container_width=True)
        except Exception as e:
            st.error(f"Lỗi thống kê: {e}")

    # --- TRỢ LÝ LỚP ---
    with tab_ai:
        st.subheader("Trợ lý lớp (hỏi nhanh)")
        q = st.text_input("Ví dụ: 'Vắng buổi 2?', 'Tỷ lệ buổi 3?'", key="qa_input")
        if q:
            try:
                buoi_q = infer_buoi_from_text(q) or buoi
                sheet = get_sheet(st.session_state.get("lop", lop_chon))
                df = sheet_to_df(sheet)
                if df.empty:
                    st.info("Chưa có dữ liệu.")
                else:
                    intent = classify_intent(q)
                    if intent == "absent_list":
                        v = df[df[buoi_q].fillna("") != "✅"][["MSSV", "Họ và Tên"]]
                        st.dataframe(v, use_container_width=True) if len(v) else st.write("Không có ai vắng.")
                    elif intent == "present_count":
                        p, a, t = group_stats_for_buoi(df, buoi_q)
                        st.success(f"Đi học {buoi_q}: {p}/{t} ({round(p/t*100,1) if t else 0}%).")
                    else:
                        p, a, t = group_stats_for_buoi(df, buoi_q)
                        st.success(f"Tổng quan {buoi_q}: Đi học {p}, Vắng {a}, Tỷ lệ {round(p/t*100,1) if t else 0}%")
            except Exception as e:
                st.error(f"Lỗi trợ lý lớp: {e}")

# ---------- SINH VIÊN ----------
else:
    # Đăng nhập SV (nếu bật trong secrets)
    if not sv_authenticated():
        st.stop()

    st.subheader("📲 Nhập thông tin điểm danh (SV)")

    if sv_param == "1" and lop_qr and buoi_qr:
        # === luồng từ QR: KHÔNG hiển thị chọn lớp/buổi, chỉ nhập thông tin và xác nhận ===
        st.info(f"Lớp: **{lop_qr}** • {buoi_qr}")
        st.write(f"MSSV có tiền tố: **{SESSION_PREFIX}**")

        mssv_tail = st.text_input("Nhập 4 số cuối MSSV", key="mssv_tail_qr")
        hoten = st.text_input("Nhập họ và tên", key="hoten_qr")

        # gợi ý theo 4 số cuối (nếu có)
        try:
            sheet = get_sheet(lop_qr)
            df = sheet_to_df(sheet)
            if mssv_tail and "MSSV" in df.columns and "Họ và Tên" in df.columns:
                g = df[df["MSSV"].astype(str).str.endswith(mssv_tail)][["MSSV", "Họ và Tên"]]
                if len(g) > 0:
                    st.write("Gợi ý:")
                    st.dataframe(g, use_container_width=True, hide_index=True)
        except Exception:
            pass

        if st.button("Xác nhận điểm danh", use_container_width=True, key="btn_checkin_qr"):
            try:
                ss = _get_spreadsheet()
                sheet = ss.worksheet(lop_qr)
                mssv = SESSION_PREFIX + (mssv_tail or "").strip()
                row_idx = find_row_by_mssv(sheet, mssv)
                if not row_idx:
                    st.error("❌ Không tìm thấy MSSV trong danh sách lớp.")
                else:
                    hoten_sheet = _with_retry(lambda: sheet.cell(row_idx, _get_col_indices(SHEET_KEY, sheet.title, buoi_qr)["hoten"]).value)
                    if normalize_name(hoten_sheet or "") != normalize_name(hoten):
                        st.error("❌ Họ tên không khớp với MSSV.")
                    else:
                        if USE_APPEND_LOG:
                            append_checkin_log(ss, lop_qr, buoi_qr, mssv, hoten)
                        else:
                            mark_present_with_time(sheet, buoi_qr, row_idx)
                        st.success("🎉 Điểm danh thành công!")
            except Exception as e:
                st.error(f"❌ Lỗi khi điểm danh: {e}")

    else:
        # === luồng không dùng QR (cho SV tự chọn lớp/buổi) ===
        classes = list_classes()
        lop = st.selectbox("Chọn lớp", classes, key="class_sv_manual")
        buoi_sv = st.selectbox("Chọn buổi", [f"Buổi {i}" for i in range(1, 13)], key="buoi_sv_manual")
        st.write(f"MSSV có tiền tố: **{SESSION_PREFIX}**")
        mssv_tail = st.text_input("Nhập 4 số cuối MSSV", key="mssv_tail_manual")
        hoten = st.text_input("Nhập họ và tên", key="hoten_manual")
        if st.button("Xác nhận điểm danh", use_container_width=True, key="btn_checkin_manual"):
            try:
                ss = _get_spreadsheet()
                sheet = ss.worksheet(lop)
                mssv = SESSION_PREFIX + (mssv_tail or "").strip()
                row_idx = find_row_by_mssv(sheet, mssv)
                if not row_idx:
                    st.error("Không tìm thấy MSSV trong danh sách lớp.")
                else:
                    hoten_sheet = _with_retry(lambda: sheet.cell(row_idx, _get_col_indices(SHEET_KEY, sheet.title, buoi_sv)["hoten"]).value)
                    if normalize_name(hoten_sheet or "") != normalize_name(hoten):
                        st.error("Họ tên không khớp với MSSV.")
                    else:
                        if USE_APPEND_LOG:
                            append_checkin_log(ss, lop, buoi_sv, mssv, hoten)
                        else:
                            mark_present_with_time(sheet, buoi_sv, row_idx)
                        st.success("✅ Đã điểm danh!")
            except Exception as e:
                st.error(f"Lỗi khi điểm danh: {e}")

# ===================== KEEP-ALIVE NHẸ CHO STREAMLIT =====================
def _calc_keepalive_interval():
    buffer = 180 if HOST_PROVIDER == "streamlit" else 120
    return max(60, HOST_IDLE_TIMEOUT_MIN * 60 - buffer - random.randint(0, 30))

def _keep_alive_ping():
    if not KEEPALIVE_ENABLED:
        return
    url = (APP_URL or "").strip()
    if not url:
        return
    try:
        requests.get(url, timeout=6)
    except Exception:
        pass
    while True:
        time.sleep(_calc_keepalive_interval())
        try:
            requests.get(url, timeout=6)
        except Exception:
            pass

if "keepalive_started" not in st.session_state:
    threading.Thread(target=_keep_alive_ping, daemon=True).start()
    st.session_state["keepalive_started"] = True

# ---------- FOOTER  ----------

st.markdown("---")
st.markdown("© Bản quyền thuộc về TS. Đào Hồng Nam - Đại học Y Dược Thành phố Hồ Chí Minh.")















