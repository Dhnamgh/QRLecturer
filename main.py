import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
import qrcode
from PIL import Image
import io, time, urllib.parse, re, base64
import pandas as pd
import altair as alt
from datetime import datetime, timezone, timedelta
import unicodedata

# ===================== CẤU HÌNH =====================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SHEET_KEY = "1P7SOGsmb2KwBX50MU1Y1iVCYtjTiU7F7jLqgp6Bl8Bo"     # <-- thay bằng ID Google Sheet của bạn
WRAPPER_URL = "https://dhnamgh.github.io/PresenceAI/"       # <-- trang bọc GitHub Pages của bạn
CLASS_EXCLUDE_KEYWORDS = {"likert", "mcq", "question", "test"}
SESSION_PREFIX = "51125"  # tiền tố MSSV, để SV nhập 4 số cuối

st.set_page_config(page_title="QR Lecturer", layout="wide")

# ===================== GOOGLE SHEET KẾT NỐI =====================
@st.cache_resource
def _get_gspread_client():
    cred = dict(st.secrets["google_service_account"])
    pk = cred.get("private_key", "")
    if not pk:
        raise RuntimeError("Secrets thiếu 'private_key'.")
    # Chuẩn hóa newline
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
    # validate base64
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
    return " ".join(w.capitalize() for w in name.strip().split())

def _time_col_for(buoi: str) -> str:
    digits = "".join(ch for ch in buoi if ch.isdigit())
    return f"Thời gian {digits}" if digits else "Thời gian"

def mark_present_with_time(sheet, buoi: str, row_idx: int):
    col_diemdanh = sheet.find(buoi).col
    col_time = sheet.find(_time_col_for(buoi)).col
    vn_tz = timezone(timedelta(hours=7))
    now_str = datetime.now(vn_tz).strftime("%Y-%m-%d %H:%M:%S")
    sheet.update_cell(row_idx, col_diemdanh, "✅")
    sheet.update_cell(row_idx, col_time, now_str)

def find_row_by_mssv(sheet, mssv: str) -> int | None:
    col_mssv = sheet.find("MSSV").col
    values = sheet.col_values(col_mssv)
    target = str(mssv).strip()
    for i, v in enumerate(values, start=1):
        if str(v).strip() == target:
            return i
    return None

def sheet_to_df(sheet) -> pd.DataFrame:
    records = sheet.get_all_records()
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
    col_mssv = sheet.find("MSSV").col
    col_buoi = sheet.find(buoi).col
    col_name = sheet.find(name_col_title).col
    mssv_vals = sheet.col_values(col_mssv)[1:]
    buoi_vals = sheet.col_values(col_buoi)[1:]
    name_vals = sheet.col_values(col_name)[1:]
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

# ===================== TRỢ LÝ AI - HIỂU NGÔN NGỮ TỰ NHIÊN =====================
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
    if "vang" in t:
        return "absent_list"
    if "som nhat" in t or "som" in t:
        return "earliest"
    if "muon nhat" in t or "muon" in t:
        return "latest"
    if "nhieu nhat" in t:
        return "most_count"
    if "it nhat" in t:
        return "least_count"
    if "cao nhat" in t:
        return "highest_rate"
    if "thap nhat" in t:
        return "lowest_rate"
    if "ty le" in t:
        return "rate_overall"
    if "bao nhieu" in t or ("so" in t and "di hoc" in t):
        return "present_count"
    return "rate_overall"

# ===================== GIAO DIỆN (URL PARAMS) =====================
qp = dict(st.query_params)
student_only = (qp.get("sv") == "1") or ("buoi" in qp) or ("lop" in qp)

# ===== SINH VIÊN (SV-ONLY) =====
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

    st.write(f"Mã số sinh viên: {SESSION_PREFIX}")
    mssv_tail = st.text_input("Nhập 4 số cuối MSSV")
    mssv = SESSION_PREFIX + (mssv_tail or "").strip()
    hoten = st.text_input("Nhập họ và tên")

    # Gợi ý tên theo 4 số
    if mssv_tail and len(mssv_tail.strip()) == 4 and mssv_tail.strip().isdigit():
        try:
            sheet_preview = get_sheet(lop_sv)
            col_mssv = sheet_preview.find("MSSV").col
            col_name = sheet_preview.find("Họ và Tên").col
            values = sheet_preview.col_values(col_mssv)
            row_idx_prev = next((i for i, v in enumerate(values, start=1) if str(v).strip() == mssv), None)
            if row_idx_prev:
                preview_name = sheet_preview.cell(row_idx_prev, col_name).value
                st.caption(f"🔎 Khớp MSSV: **{mssv}** • Họ tên: **{preview_name}**")
        except Exception:
            pass

    if st.button("✅ Xác nhận điểm danh"):
        if not mssv.strip().isdigit():
            st.warning("⚠️ MSSV phải là số.")
        elif not hoten.strip():
            st.warning("⚠️ Vui lòng nhập họ và tên.")
        else:
            try:
                sheet = get_sheet(lop_sv)
                row_idx = find_row_by_mssv(sheet, mssv)
                if not row_idx:
                    st.error(f"❌ MSSV {mssv} không có trong danh sách.")
                    st.stop()
                col_name = sheet.find("Họ và Tên").col
                hoten_sheet = sheet.cell(row_idx, col_name).value
                if normalize_name(hoten_sheet or "") != normalize_name(hoten):
                    st.error("❌ Họ tên không khớp với MSSV.")
                else:
                    mark_present_with_time(sheet, buoi_sv, row_idx)
                    st.success("🎉 Điểm danh thành công!")
            except Exception as e:
                st.error(f"❌ Lỗi khi điểm danh: {e}")
    st.stop()

# ===== GIẢNG VIÊN =====
st.title("🧾 Hệ thống điểm danh QR")
st.sidebar.title("Điều hướng")
mode = st.sidebar.radio("Chọn chế độ", ["👨‍🏫 Giảng viên", "🎓 Sinh viên"], index=0)

def gv_authenticated() -> bool:
    ok = st.session_state.get("gv_auth_ok", False)
    if ok:
        return True
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

# chọn lớp/buổi dùng chung
classes = list_classes()
if not classes:
    st.error("Chưa có worksheet lớp trong file.")
    st.stop()
lop_chon = st.selectbox("Chọn lớp", classes)
buoi = st.selectbox("Chọn buổi", ["Buổi 1","Buổi 2","Buổi 3","Buổi 4","Buổi 5","Buổi 6"])

# ---------- GIẢNG VIÊN ----------
if mode == "👨‍🏫 Giảng viên":
    if not gv_authenticated():
        st.warning("Vui lòng đăng nhập để dùng chức năng giảng viên.")
        st.stop()

    # 3 tab: Tạo mã QR / Thống kê / Trợ lý lớp
    tab_qr, tab_stats, tab_ai = st.tabs(["🧾 Tạo mã QR", "📊 Thống kê", "🤖 Trợ lý lớp"])

    # ===== TAB: TẠO MÃ QR =====
    with tab_qr:
        st.subheader("📸 Tạo mã QR điểm danh")
        st.caption("Mã QR chứa thông tin Lớp & Buổi; sinh viên quét sẽ mở trang bọc (wrapper).")
        if st.button("Tạo mã QR"):
            st.session_state["lop"] = lop_chon
            st.session_state["buoi"] = buoi
            qr_link = f"{WRAPPER_URL}?sv=1&lop={urllib.parse.quote(lop_chon)}&buoi={urllib.parse.quote(buoi)}"
            img_qr = qrcode.make(qr_link)
            buf = io.BytesIO(); img_qr.save(buf, format="PNG"); buf.seek(0)
            st.image(Image.open(buf), caption="Quét để điểm danh", width=260)
            st.code(qr_link, language="text")
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

    # ===== TAB: TRỢ LÝ LỚP (NLP) =====
    with tab_ai:
        st.subheader("🤖 Trợ lý lớp — hỏi ngắn, hiểu ý")
        qtext = st.text_input(
            "Nhập câu hỏi",
            placeholder="ai đi sớm nhất buổi 1 / ai muộn nhất / tổ nào nhiều nhất / danh sách vắng buổi 3 ..."
        )
        if st.button("Trả lời"):
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

# ---------- SINH VIÊN (tab dự phòng trong app) ----------
else:
    st.subheader("📲 Nhập thông tin điểm danh (SV)")
    mssv = st.text_input("Nhập MSSV")
    hoten = st.text_input("Nhập họ và tên")
    if st.button("Điểm danh"):
        try:
            sheet = get_sheet(lop_chon)
            row_idx = find_row_by_mssv(sheet, mssv)
            if not row_idx:
                st.error("Không tìm thấy MSSV.")
            else:
                col_name = sheet.find("Họ và Tên").col
                hoten_sheet = sheet.cell(row_idx, col_name).value
                if normalize_name(hoten_sheet or "") != normalize_name(hoten):
                    st.error("Họ tên không khớp với MSSV.")
                else:
                    mark_present_with_time(sheet, buoi, row_idx)
                    st.success("Đã điểm danh!")
        except Exception as e:
            st.error(f"Lỗi khi điểm danh: {e}")



# ---------- FOOTER  ----------

st.markdown("---")
st.markdown("© Bản quyền thuộc về TS. Đào Hồng Nam - Đại học Y Dược Thành phố Hồ Chí Minh.")










