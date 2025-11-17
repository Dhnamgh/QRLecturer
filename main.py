import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
import qrcode
from PIL import Image
import io, time, urllib.parse, re, base64, random
import pandas as pd
import altair as alt
from datetime import datetime, timezone, timedelta
import unicodedata
import requests
import threading

# ===================== CẤU HÌNH =====================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
CLASS_EXCLUDE_KEYWORDS = {"likert", "mcq", "question", "test"}

st.set_page_config(page_title="QR Lecturer", layout="wide")

# ====== SECRETS (bắt buộc) ======
def _must(key, hint=""):
    v = st.secrets.get(key)
    if v in (None, ""):
        st.error(f"Thiếu `{key}` trong secrets. {hint}")
        st.stop()
    return v.strip() if isinstance(v, str) else v

SHEET_KEY      = _must("SHEET_KEY", "ID giữa /d/ và /edit của Google Sheet.")
WRAPPER_URL    = _must("WRAPPER_URL", "VD: https://<github-pages>/")
SESSION_PREFIX = st.secrets.get("SESSION_PREFIX", "51125")
ADMIN_PASSWORD = _must("ADMIN_PASSWORD", "Mật khẩu GV.")
STUDENT_PASSWORD = st.secrets.get("STUDENT_PASSWORD", "")  # rỗng = không yêu cầu SV nhập mật khẩu

# Keep-alive (tùy chọn)
APP_URL               = st.secrets.get("APP_URL", "")
HOST_PROVIDER         = st.secrets.get("HOST_PROVIDER", "streamlit").lower()
HOST_IDLE_TIMEOUT_MIN = int(st.secrets.get("HOST_IDLE_TIMEOUT_MIN", 720))
KEEPALIVE_ENABLED     = bool(st.secrets.get("KEEPALIVE_ENABLED", True))

# ===================== GOOGLE SHEET KẾT NỐI =====================
def with_retry(fn, retries=5):
    for i in range(retries):
        try:
            return fn()
        except Exception:
            if i == retries - 1:
                raise
            time.sleep((2 ** i) * 0.4 + random.random() * 0.25)

@st.cache_resource
def _get_gspread_client():
    cred = dict(st.secrets["google_service_account"])
    pk = cred.get("private_key", "")
    if not pk:
        raise RuntimeError("Secrets thiếu 'private_key'.")
    if "\\n" in pk:
        pk = pk.replace("\\n", "\n")
    pk = pk.replace("\r\n", "\n").replace("\r", "\n")
    header, footer = "-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----"
    lines = [ln.strip() for ln in pk.split("\n") if ln.strip()]
    h_idx, f_idx = lines.index(header), lines.index(footer)
    body = re.sub(r"[^A-Za-z0-9+/=]", "", "".join(lines[h_idx + 1:f_idx]))
    body = body.replace("=", "")
    if len(body) % 4:
        body += "=" * (4 - len(body) % 4)
    base64.b64decode(body, validate=True)
    pk_clean = header + "\n" + "\n".join(body[i:i + 64] for i in range(0, len(body), 64)) + "\n" + footer + "\n"
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
        tn = (t or "").strip().lower()
        if not tn or tn in banned_exact:
            continue
        if any(k in tn for k in CLASS_EXCLUDE_KEYWORDS):
            continue
        out.append(t)
    return out

def get_sheet(lop: str):
    ss = _get_spreadsheet()
    try:
        return ss.worksheet(lop)
    except gspread.exceptions.WorksheetNotFound:
        raise RuntimeError(f"Không tìm thấy lớp/worksheet '{lop}'.")

# ===================== HÀM TIỆN ÍCH =====================
def normalize_name(name: str):
    # giữ y như bản gốc của bạn
    return " ".join(w.capitalize() for w in name.strip().split())

def _time_col_for(buoi: str) -> str:
    digits = "".join(ch for ch in buoi if ch.isdigit())
    return f"Thời gian {digits}" if digits else "Thời gian"

def _a1(col: int, row: int) -> str:
    s = ""
    while col > 0:
        col, r = divmod(col-1, 26)
        s = chr(65+r) + s
    return f"{s}{row}"

def mark_present_with_time(sheet, buoi: str, row_idx: int):
    """Ghi ✅ + timestamp trong 1 lần update (đỡ nghẽn khi 200 SV)."""
    col_diem = with_retry(lambda: sheet.find(buoi).col)
    col_time = with_retry(lambda: sheet.find(_time_col_for(buoi)).col)
    vn_tz = timezone(timedelta(hours=7))
    now_str = datetime.now(vn_tz).strftime("%Y-%m-%d %H:%M:%S")
    rng = f"{_a1(col_diem,row_idx)}:{_a1(col_time,row_idx)}"
    with_retry(lambda: sheet.update(rng, [["✅", now_str]], value_input_option="RAW"))

def find_row_by_mssv(sheet, mssv: str) -> int | None:
    col_mssv = with_retry(lambda: sheet.find("MSSV").col)
    values = with_retry(lambda: sheet.col_values(col_mssv))
    target = str(mssv).strip()
    for i, v in enumerate(values, start=1):
        if str(v).strip() == target:
            return i
    return None

def sheet_to_df(sheet) -> pd.DataFrame:
    records = with_retry(lambda: sheet.get_all_records())
    return pd.DataFrame(records) if records else pd.DataFrame()

def group_stats_for_buoi(df: pd.DataFrame, buoi: str, group_col: str = "Tổ") -> pd.DataFrame:
    if df.empty or group_col not in df.columns or buoi not in df.columns:
        return pd.DataFrame(columns=["group", "present", "absent", "total", "rate"])
    present_mask = df[buoi].astype(str).str.strip() != ""
    total = df.groupby(group_col, dropna=False).size().rename("total")
    present = df.loc[present_mask].groupby(group_col, dropna=False).size().rename("present")
    stats = pd.concat([total, present], axis=1).fillna(0).astype(int)
    stats["absent"] = stats["total"] - stats["present"]
    stats["rate"] = (stats["present"] / stats["total"] * 100).round(1)
    stats = stats.reset_index().rename(columns={group_col: "group"})
    return stats

def attendance_counts(sheet, buoi: str, name_col_title: str = "Họ và Tên"):
    col_mssv = with_retry(lambda: sheet.find("MSSV").col)
    col_buoi = with_retry(lambda: sheet.find(buoi).col)
    col_name = with_retry(lambda: sheet.find(name_col_title).col)
    mssv_vals = with_retry(lambda: sheet.col_values(col_mssv))[1:]
    buoi_vals = with_retry(lambda: sheet.col_values(col_buoi))[1:]
    name_vals = with_retry(lambda: sheet.col_values(col_name))[1:]
    is_student = [str(x).strip() != "" for x in mssv_vals]
    total = sum(is_student)
    present, absent_names = 0, []
    for i, stu in enumerate(is_student):
        if not stu:
            continue
        present_flag = str(buoi_vals[i] if i < len(buoi_vals) else "").strip() != ""
        if present_flag:
            present += 1
        else:
            nm = str(name_vals[i] if i < len(name_vals) else "").strip()
            absent_names.append(nm)
    absent = total - present
    return present, total, absent, absent_names

def parse_time_series(df: pd.DataFrame, buoi: str) -> pd.DataFrame:
    time_col = _time_col_for(buoi)
    cols_needed = ["MSSV", "Họ và Tên", "Tổ"]
    for c in cols_needed:
        if c not in df.columns:
            df[c] = None
    if time_col not in df.columns:
        return pd.DataFrame(columns=["MSSV", "Họ và Tên", "Tổ", "time", "buoi"])
    tmp = df[["MSSV", "Họ và Tên", "Tổ", time_col]].copy()
    tmp = tmp[tmp[time_col].astype(str).str.strip() != ""]
    def _parse(x):
        x = str(x).strip()
        for fmt in ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                return datetime.strptime(x, fmt)
            except:
                pass
        return None
    tmp["time"] = tmp[time_col].apply(_parse)
    tmp = tmp[tmp["time"].notna()]
    tmp["buoi"] = buoi
    return tmp.drop(columns=[time_col])

# ===================== NLP trợ lý =====================
def _vn_norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return s.lower()

def infer_buoi_from_text(text: str, default_buoi: str) -> str:
    t = _vn_norm(text)
    m = re.search(r"\bbuo?i?\s*(\d+)\b", t) or re.search(r"\bb\s*(\d+)\b", t)
    if m:
        return f"Buổi {int(m.group(1))}"
    return default_buoi

def classify_intent(text: str) -> str:
    t = _vn_norm(text)
    if "vang" in t: return "absent_list"
    if "som nhat" in t or "som" in t: return "earliest"
    if "muon nhat" in t or "muon" in t: return "latest"
    if "nhieu nhat" in t: return "most_count"
    if "it nhat" in t: return "least_count"
    if "cao nhat" in t: return "highest_rate"
    if "thap nhat" in t: return "lowest_rate"
    if "ty le" in t: return "rate_overall"
    if "bao nhieu" in t or ("so" in t and "di hoc" in t): return "present_count"
    return "rate_overall"

# ===================== GIAO DIỆN (URL PARAMS) =====================
qp = dict(st.query_params)
student_only = (qp.get("sv") == "1") or ("buoi" in qp) or ("lop" in qp)

# ===== SINH VIÊN (chỉ qua QR) =====
if student_only:

    # -------------------------
    # 1. KIỂM TRA HẾT HẠN QR
    # -------------------------
    raw_exp = qp.get("exp")  # từ query string
    if isinstance(raw_exp, (list, tuple)):
        raw_exp = raw_exp[0] if raw_exp else None

    # Nếu reload app → lấy exp từ session
    if not raw_exp:
        raw_exp = st.session_state.get("sv_exp")

    if raw_exp:
        try:
            exp_ts = int(str(raw_exp).strip())
            st.session_state["sv_exp"] = exp_ts
            if time.time() > exp_ts:
                st.title("🎓 Điểm danh sinh viên")
                st.error("⏰ Mã QR đã hết thời hạn hiệu lực. Vui lòng quét mã mới.")
                st.stop()
        except:
            pass

    # -------------------------
    # 2. KHÓA – SV ĐÃ ĐIỂM DANH
    # -------------------------
    if st.session_state.get("sv_locked"):
        locked_mssv = st.session_state.get("sv_mssv", "")
        locked_hoten = st.session_state.get("sv_hoten", "")

        st.title("🎓 Điểm danh sinh viên")
        st.success(f"✅ Bạn đã điểm danh với MSSV **{locked_mssv}**, họ tên **{locked_hoten}**.")
        st.info("Nếu cần chỉnh sửa thông tin, vui lòng liên hệ giảng viên.")
        st.stop()

    # -------------------------
    # 3. LẤY CLASS & BUỔI
    # -------------------------
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

    # -------------------------
    # 4. NHẬP MẬT KHẨU SV (nếu dùng)
    # -------------------------
    if STUDENT_PASSWORD:
        sv_pwd = st.text_input("Mật khẩu SV", type="password", key="sv_pwd_qr")
        if not sv_pwd:
            st.stop()
        if sv_pwd != STUDENT_PASSWORD:
            st.error("Sai mật khẩu SV.")
            st.stop()

    # -------------------------
    # 5. NHẬP MSSV + TÊN
    # -------------------------
    st.write(f"MSSV phải bắt đầu bằng **{SESSION_PREFIX}**")
    mssv_tail = st.text_input("Nhập 4 số cuối MSSV", max_chars=4, key="mssv_tail_qr")
    mssv_tail = (mssv_tail or "").strip()

    hoten = st.text_input("Nhập họ và tên", key="hoten_qr")

    # -------------------------
    # 6. GỢI Ý TÊN THEO MSSV
    # -------------------------
    if len(mssv_tail) == 4 and mssv_tail.isdigit():
        mssv_preview = SESSION_PREFIX + mssv_tail

        try:
            sheet_preview = get_sheet(lop_sv)
            cell_mssv = with_retry(lambda: sheet_preview.find("MSSV"))
            cell_name = with_retry(lambda: sheet_preview.find("Họ và Tên"))

            if cell_mssv and cell_name:
                col_mssv = cell_mssv.col
                col_name = cell_name.col
                values = with_retry(lambda: sheet_preview.col_values(col_mssv))

                row_idx_prev = next(
                    (i for i, v in enumerate(values, start=1)
                     if str(v).strip() == mssv_preview),
                    None
                )
                if row_idx_prev:
                    preview_name = with_retry(lambda: sheet_preview.cell(row_idx_prev, col_name).value)
                    st.caption(f"🔎 Khớp MSSV: **{mssv_preview}** • Họ tên: **{preview_name}**")
        except Exception:
            pass

    # -------------------------
    # 7. XÁC NHẬN ĐIỂM DANH
    # -------------------------
    if st.button("✅ Xác nhận điểm danh", use_container_width=True):

        # kiểm tra hợp lệ
        if not mssv_tail:
            st.warning("⚠️ Vui lòng nhập 4 số cuối MSSV.")
            st.stop()

        if len(mssv_tail) != 4 or not mssv_tail.isdigit():
            st.warning("⚠️ 4 số cuối MSSV phải là 4 chữ số.")
            st.stop()

        if not hoten.strip():
            st.warning("⚠️ Vui lòng nhập họ và tên.")
            st.stop()

        mssv = SESSION_PREFIX + mssv_tail

        try:
            sheet = get_sheet(lop_sv)
            row_idx = find_row_by_mssv(sheet, mssv)
            if not row_idx:
                st.error(f"❌ MSSV {mssv} không có trong danh sách.")
                st.stop()

            cell_name = with_retry(lambda: sheet.find("Họ và Tên"))
            if not cell_name:
                st.error("❌ Không tìm thấy cột 'Họ và Tên'.")
                st.stop()

            col_name = cell_name.col
            hoten_sheet = with_retry(lambda: sheet.cell(row_idx, col_name).value)

            if normalize_name(hoten_sheet or "") != normalize_name(hoten):
                st.error("❌ Họ tên không khớp với MSSV.")
                st.stop()

            # Ghi điểm danh
            mark_present_with_time(sheet, buoi_sv, row_idx)
            st.success("🎉 Điểm danh thành công!")

            # 🔐 KHÓA SESSION – KHÔNG CHO SỬA LẠI
            st.session_state["sv_locked"] = True
            st.session_state["sv_mssv"] = mssv
            st.session_state["sv_hoten"] = hoten_sheet or hoten

            st.stop()

        except Exception as e:
            st.error(f"❌ Lỗi khi điểm danh: {e}")
            st.stop()

    st.stop()

# ===== GIẢNG VIÊN =====
st.title("🧾 Hệ thống điểm danh QR")
st.sidebar.title("Giảng viên")

def gv_authenticated() -> bool:
    ok = st.session_state.get("gv_auth_ok", False)
    if ok:
        st.sidebar.success("Đã đăng nhập (GV)")
        if st.sidebar.button("Đăng xuất GV", key="btn_logout_gv"):
            st.session_state["gv_auth_ok"] = False
            st.rerun()
        return True
    st.sidebar.subheader("Đăng nhập giảng viên")
    pwd = st.sidebar.text_input("Mật khẩu", type="password")
    if st.sidebar.button("Đăng nhập"):
        if pwd == ADMIN_PASSWORD:
            st.session_state["gv_auth_ok"] = True
            st.rerun()
        else:
            st.sidebar.error("Sai mật khẩu.")
    return False

# chọn lớp/buổi dùng cho GV
classes = list_classes()
if not classes:
    st.error("Chưa có worksheet lớp trong file."); st.stop()
lop_chon = st.selectbox("Chọn lớp", classes, key="class_gv")
buoi = st.selectbox("Chọn buổi", [f"Buổi {i}" for i in range(1, 13)], key="buoi_gv")

if not gv_authenticated():
    st.warning("Vui lòng đăng nhập để dùng chức năng giảng viên.")
    st.stop()

# 3 tab: Tạo mã QR / Thống kê / Trợ lý lớp
tab_qr, tab_stats, tab_ai = st.tabs(["🧾 Tạo mã QR", "📊 Thống kê", "🤖 Trợ lý lớp"])

# ===== TAB: TẠO MÃ QR =====
with tab_qr:
    st.subheader("📸 Tạo mã QR điểm danh")
    st.caption("Mã QR chứa thông tin Lớp & Buổi; sinh viên quét sẽ mở trang bọc (wrapper).")

    if st.button("Tạo mã QR", key="btn_make_qr", use_container_width=True):
        st.session_state["lop"] = lop_chon
        st.session_state["buoi"] = buoi

        # ⏱ Hạn sử dụng QR: 60 giây kể từ lúc tạo
        expires_ts = int(time.time()) + 60   # cần: import time ở đầu file
        st.session_state["qr_exp"] = expires_ts

        # 👉 NHỚ: gắn exp vào link QR
        qr_link = (
            f"{WRAPPER_URL}"
            f"?sv=1"
            f"&lop={urllib.parse.quote(lop_chon)}"
            f"&buoi={urllib.parse.quote(buoi)}"
            f"&exp={expires_ts}"
        )

        img_qr = qrcode.make(qr_link)
        buf = io.BytesIO()
        img_qr.save(buf, format="PNG")
        buf.seek(0)
        img_obj = Image.open(buf)

        # căn giữa, không hiển thị link
        c1, c2, c3 = st.columns([1, 1, 1])
        with c2:
            st.image(img_obj, width=320)

        # đếm ngược (chỉ là UI cho GV)
        t = st.empty()
        for i in range(60, 0, -1):
            t.markdown(f"⏳ Hiệu lực còn: **{i} giây**")
            time.sleep(1)
        t.markdown("✅ Hết thời gian hiệu lực.")

# ===== TAB: THỐNG KÊ =====
with tab_stats:
    lop_stat = st.session_state.get("lop", lop_chon)
    buoi_stat = st.session_state.get("buoi", buoi)
    st.subheader(f"📊 Lớp **{lop_stat}** • {buoi_stat}")
    try:
        sheet = get_sheet(lop_stat)
        present, total, absent, _ = attendance_counts(sheet, buoi_stat)
        c1, c2, c3 = st.columns(3)
        with c1: st.metric("Đi học", present)
        with c2: st.metric("Vắng", absent)
        with c3: st.metric("Tỷ lệ", f"{round(present/total*100,1) if total else 0}%")

        df = sheet_to_df(sheet)
        stats = group_stats_for_buoi(df, buoi_stat)
        if not stats.empty:
            chart = alt.Chart(stats).mark_bar().encode(
                x=alt.X("group:N", title="Tổ"),
                y=alt.Y("present:Q", title="Số đi học"),
                color="group:N",
                tooltip=["group","present","absent","total","rate"]
            ).properties(height=300)
            st.altair_chart(chart, use_container_width=True)
    except Exception as e:
        st.error(f"Lỗi thống kê: {e}")

# ===== TAB: TRỢ LÝ LỚP =====
with tab_ai:
    st.subheader("🤖 Trợ lý lớp — hỏi ngắn, hiểu ý")
    qtext = st.text_input(
        "Nhập câu hỏi",
        placeholder="ai đi sớm nhất buổi 1 / ai muộn nhất / tổ nào nhiều nhất / danh sách vắng buổi 3 ..."
    )
    if st.button("Trả lời", key="btn_qa"):
        if not qtext.strip():
            st.info("Hãy nhập câu hỏi trước nhé.")
        else:
            try:
                buoi_q = infer_buoi_from_text(qtext, st.session_state.get("buoi", buoi))
                sheet2 = get_sheet(st.session_state.get("lop", lop_chon))
                df2 = sheet_to_df(sheet2)
                ts = parse_time_series(df2, buoi_q)
                stats2 = group_stats_for_buoi(df2, buoi_q)
                present, total, absent, abs_names = attendance_counts(sheet2, buoi_q)
                intent = classify_intent(qtext)

                if intent == "earliest":
                    if ts.empty:
                        st.info("Chưa có dữ liệu thời gian cho buổi này.")
                    else:
                        r = ts.sort_values("time").iloc[0]
                        st.success(f"✅ Sớm nhất {buoi_q}: {r['Họ và Tên']} ({r['MSSV']}) lúc {r['time'].strftime('%H:%M:%S')}")

                elif intent == "latest":
                    if ts.empty:
                        st.info("Chưa có dữ liệu thời gian cho buổi này.")
                    else:
                        r = ts.sort_values("time", ascending=False).iloc[0]
                        st.success(f"⏰ Muộn nhất {buoi_q}: {r['Họ và Tên']} ({r['MSSV']}) lúc {r['time'].strftime('%H:%M:%S')}")

                elif intent == "most_count":
                    if stats2.empty:
                        st.info("Chưa có dữ liệu thống kê theo tổ.")
                    else:
                        r = stats2.sort_values("present", ascending=False).iloc[0]
                        st.success(f"📊 Nhiều nhất {buoi_q}: Tổ {r['group']} — {r['present']}/{r['total']} (~{r['rate']}%)")

                elif intent == "least_count":
                    if stats2.empty:
                        st.info("Chưa có dữ liệu thống kê theo tổ.")
                    else:
                        r = stats2.sort_values("present").iloc[0]
                        st.success(f"📊 Ít nhất {buoi_q}: Tổ {r['group']} — {r['present']}/{r['total']} (~{r['rate']}%)")

                elif intent == "highest_rate":
                    if stats2.empty:
                        st.info("Chưa có dữ liệu thống kê theo tổ.")
                    else:
                        r = stats2.sort_values(["rate","present"], ascending=[False, False]).iloc[0]
                        st.success(f"🏅 Tỷ lệ cao nhất {buoi_q}: Tổ {r['group']} — {r['rate']}% ({r['present']}/{r['total']})")

                elif intent == "lowest_rate":
                    if stats2.empty:
                        st.info("Chưa có dữ liệu thống kê theo tổ.")
                    else:
                        r = stats2.sort_values(["rate","present"], ascending=[True, True]).iloc[0]
                        st.success(f"📉 Tỷ lệ thấp nhất {buoi_q}: Tổ {r['group']} — {r['rate']}% ({r['present']}/{r['total']})")

                elif intent == "absent_list":
                    st.info(f"Vắng {buoi_q}: {absent}/{total} ({round(present/total*100,1) if total else 0}%).")
                    if abs_names:
                        st.dataframe(pd.DataFrame({"Họ và Tên": abs_names}), use_container_width=True)
                    else:
                        st.write("Không có ai vắng.")

                elif intent == "present_count":
                    st.success(f"Đi học {buoi_q}: {present}/{total} ({round(present/total*100,1) if total else 0}%).")

                else:  # rate_overall
                    st.success(f"Tổng quan {buoi_q}: Đi học {present}, Vắng {absent}, Tỷ lệ {round(present/total*100,1) if total else 0}%")
            except Exception as e:
                st.error(f"Lỗi trợ lý lớp: {e}")

# ===================== KEEP-ALIVE NHẸ =====================
def _ka_interval():
    buf = 180 if HOST_PROVIDER == "streamlit" else 120
    return max(60, HOST_IDLE_TIMEOUT_MIN * 60 - buf - random.randint(0, 30))

def _keep_alive():
    if not KEEPALIVE_ENABLED: return
    url = (APP_URL or "").strip()
    if not url: return
    try: requests.get(url, timeout=6)
    except Exception: pass
    while True:
        time.sleep(_ka_interval())
        try: requests.get(url, timeout=6)
        except Exception: pass

if "ka_started" not in st.session_state:
    threading.Thread(target=_keep_alive, daemon=True).start()
    st.session_state["ka_started"] = True

# ---------- FOOTER  ----------

st.markdown("---")
st.markdown("© Bản quyền thuộc về TS. Đào Hồng Nam")






