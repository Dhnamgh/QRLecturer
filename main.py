import streamlit as st
import gspread
from google.oauth2.service_account import Credentials
import qrcode
from PIL import Image
import io, time, urllib.parse, re, base64
import pandas as pd
import altair as alt
from datetime import datetime, timezone, timedelta

# ===================== CẤU HÌNH =====================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SHEET_KEY = "1P7SOGsmb2KwBX50MU1Y1iVCYtjTiU7F7jLqgp6Bl8Bo"   # <-- ID Google Sheet của bạn
WRAPPER_URL = "https://dhnamgh.github.io/PresenceAI/"     # <-- Trang bọc GitHub Pages
CLASS_EXCLUDE_KEYWORDS = {"likert", "mcq", "question", "test"}
SESSION_PREFIX = "51125"  # tiền tố MSSV nếu cần ghép 4 số cuối (giữ như app cũ)

st.set_page_config(page_title="QR Lecturer", layout="wide")

# ===================== KẾT NỐI GOOGLE SHEETS (kèm "vá" private_key) =====================
@st.cache_resource
def _get_gspread_client():
    cred = dict(st.secrets["google_service_account"])
    pk = cred.get("private_key", "")
    if not pk:
        raise RuntimeError("Secrets thiếu 'private_key'.")
    # xuống dòng
    if "\\n" in pk:
        pk = pk.replace("\\n", "\n")
    pk = pk.replace("\r\n", "\n").replace("\r", "\n")
    header, footer = "-----BEGIN PRIVATE KEY-----", "-----END PRIVATE KEY-----"
    if header not in pk or footer not in pk:
        raise RuntimeError("private_key thiếu BEGIN/END PRIVATE KEY.")
    lines = [ln.strip() for ln in pk.split("\n") if ln.strip()]
    h_idx, f_idx = lines.index(header), lines.index(footer)
    body = re.sub(r"[^A-Za-z0-9+/=]", "", "".join(lines[h_idx+1:f_idx]))
    body = body.replace("=", "")
    if len(body) % 4:
        body += "=" * (4 - len(body) % 4)
    # validate sớm
    base64.b64decode(body, validate=True)
    pk_clean = header + "\n" + "\n".join(body[i:i+64] for i in range(0, len(body), 64)) + "\n" + footer + "\n"
    cred["private_key"] = pk_clean
    creds = Credentials.from_service_account_info(cred, scopes=SCOPES)
    return gspread.authorize(creds)

@st.cache_resource
def _get_spreadsheet():
    return _get_gspread_client().open_by_key(SHEET_KEY)

@st.cache_data(ttl=60)
def list_classes():
    """Danh sách lớp sau khi lọc các sheet không phải lớp."""
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

# ===================== TIỆN ÍCH CHUNG =====================
def get_query_params():
    return dict(st.query_params) if hasattr(st, "query_params") else st.experimental_get_query_params()

def normalize_name(name: str):
    return " ".join(w.capitalize() for w in name.strip().split())

# ===== Ghi dấu & thời gian (UTC+7) =====
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

# ===== Tìm dòng theo MSSV (so khớp đúng cột MSSV) =====
def find_row_by_mssv(sheet, mssv: str) -> int:
    try:
        col_mssv = sheet.find("MSSV").col
    except gspread.exceptions.CellNotFound:
        raise RuntimeError("Không tìm thấy cột 'MSSV' trong sheet.")
    values = sheet.col_values(col_mssv)
    target = str(mssv).strip()
    for i, v in enumerate(values, start=1):
        if str(v).strip() == target:
            return i
    raise RuntimeError(f"MSSV {target} không có trong danh sách.")

# ===== DataFrame & Thống kê theo tổ =====
def sheet_to_df(sheet) -> pd.DataFrame:
    records = sheet.get_all_records()
    return pd.DataFrame(records) if records else pd.DataFrame()

def group_stats_for_buoi(df: pd.DataFrame, buoi: str, group_col: str = "Tổ") -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame(columns=[group_col, "present", "absent", "total", "rate"])
    if group_col not in df.columns:
        raise RuntimeError(f"Không tìm thấy cột '{group_col}' trong sheet.")
    if buoi not in df.columns:
        raise RuntimeError(f"Không tìm thấy cột '{buoi}' trong sheet.")
    present_mask = df[buoi].astype(str).str.strip() != ""
    total = df.groupby(group_col, dropna=False).size().rename("total")
    present = df.loc[present_mask].groupby(group_col, dropna=False).size().rename("present")
    stats = pd.concat([total, present], axis=1).fillna(0).astype({"total": int, "present": int})
    stats["absent"] = stats["total"] - stats["present"]
    stats["rate"] = (stats["present"] / stats["total"] * 100).round(1)
    stats = stats.reset_index().rename(columns={group_col: "group"})
    return stats[["group", "present", "absent", "total", "rate"]]

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

# ===================== THAM SỐ URL & CHẾ ĐỘ SV-ONLY =====================
qp = get_query_params()
student_only = (qp.get("sv") == "1") or ("buoi" in qp) or ("lop" in qp)

# ===================== SINH VIÊN (SV-ONLY qua QR) =====================
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

    # Gợi ý tên theo 4 số (tránh gõ nhầm)
    if mssv_tail and len(mssv_tail.strip()) == 4 and mssv_tail.strip().isdigit():
        try:
            sheet_preview = get_sheet(lop_sv)
            col_mssv = sheet_preview.find("MSSV").col
            col_name = sheet_preview.find("Họ và Tên").col
            values = sheet_preview.col_values(col_mssv)
            row_idx_prev = next((i for i, v in enumerate(values, start=1) if str(v).strip() == mssv), None)
            if row_idx_prev:
                preview_name = sheet_preview.cell(row_idx_prev, col_name).value
                st.caption(f"🔎 Khớp MSSV: **{mssv}** • Họ tên trong DS: **{preview_name}**")
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
                try:
                    col_name = sheet.find("Họ và Tên").col
                except gspread.exceptions.CellNotFound:
                    st.error("Không tìm thấy cột 'Họ và Tên' trong sheet.")
                    st.stop()
                hoten_sheet = sheet.cell(row_idx, col_name).value
                if normalize_name(hoten_sheet or "") != normalize_name(hoten):
                    st.error("❌ Họ tên không khớp với MSSV trong danh sách.")
                else:
                    mark_present_with_time(sheet, buoi_sv, row_idx)
                    st.success("🎉 Điểm danh thành công!")
            except Exception as e:
                st.error(f"❌ Lỗi khi điểm danh: {e}")

    st.stop()

# ===================== SIDEBAR: CHẾ ĐỘ & ĐĂNG NHẬP GV =====================
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

# ===================== GIAO DIỆN CHÍNH =====================
st.title("🧾 Hệ thống điểm danh QR")

# Chọn lớp/buổi dùng chung
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

# ---------- GIẢNG VIÊN ----------
if mode == "👨‍🏫 Giảng viên":
    if not gv_authenticated():
        st.warning("Vui lòng đăng nhập để dùng chức năng giảng viên.")
        st.stop()

    st.subheader("📸 Tạo mã QR điểm danh")
    st.caption("Mã QR chứa lớp & buổi; SV quét sẽ ở trang gốc (wrapper).")
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
        t.markdown("Đã hết thời gian hiệu lực.")

    # Thống kê điểm danh (tổng)
    if "lop" in st.session_state and "buoi" in st.session_state:
        st.subheader(f"📊 Thống kê: Lớp **{st.session_state['lop']}** • {st.session_state['buoi']}")
        try:
            sheet = get_sheet(st.session_state["lop"])
            col = sheet.find(st.session_state["buoi"]).col
            data = sheet.col_values(col)[1:]
            dd = sum(1 for x in data if str(x).strip())
            vang = len(data) - dd
            ds_vang = [sheet.cell(i + 2, 3).value for i, x in enumerate(data) if not str(x).strip()]
            c1, c2 = st.columns(2)
            with c1: st.metric("Đã điểm danh", dd)
            with c2: st.metric("Vắng", vang)
            st.write("Danh sách vắng:")
            st.dataframe(ds_vang, use_container_width=True)
        except Exception as e:
            st.error(f"Lỗi khi lấy thống kê: {e}")
    else:
        st.info("Chưa có dữ liệu thống kê. Hãy tạo QR trước.")

    # -------- Thống kê theo tổ --------
    st.subheader("📈 Thống kê theo tổ")
    try:
        sheet_g = get_sheet(st.session_state.get("lop", lop_chon))
        df = sheet_to_df(sheet_g)
        stats = group_stats_for_buoi(df, st.session_state.get("buoi", buoi), group_col="Tổ")
        if stats.empty:
            st.info("Chưa có dữ liệu để thống kê.")
        else:
            chart = (
                alt.Chart(stats)
                .mark_bar()
                .encode(
                    x=alt.X("group:N", title="Tổ", sort=stats["group"].tolist()),
                    y=alt.Y("present:Q", title="Số đi học"),
                    color=alt.Color("group:N", title="Tổ", legend=None),
                    tooltip=[
                        alt.Tooltip("group:N", title="Tổ"),
                        alt.Tooltip("present:Q", title="Đi học"),
                        alt.Tooltip("absent:Q", title="Vắng"),
                        alt.Tooltip("total:Q", title="Tổng"),
                        alt.Tooltip("rate:Q", title="Tỷ lệ %"),
                    ],
                )
                .properties(height=300)
            )
            st.altair_chart(chart, use_container_width=True)
            st.dataframe(
                stats.rename(columns={"group":"Tổ","present":"Đi học","absent":"Vắng","total":"Tổng","rate":"Tỷ lệ %"}),
                use_container_width=True
            )
    except Exception as e:
        st.error(f"Lỗi thống kê theo tổ: {e}")

    # -------- Trợ lý AI --------
    st.subheader("🤖 Trợ lý AI")
    colq1, colq2 = st.columns([2, 1])
    with colq1:
        preset = st.selectbox(
            "Câu hỏi nhanh",
            [
                "Ai đi sớm nhất buổi hiện tại",
                "Ai đi muộn nhất buổi hiện tại",
                "Tổ nào có mặt nhiều nhất (theo số lượng)",
                "Tổ nào có mặt ít nhất (theo số lượng)",
                "Tổ nào có tỷ lệ cao nhất",
                "Tổ nào có tỷ lệ thấp nhất",
            ],
            index=0,
        )
    with colq2:
        buoi_ask = st.selectbox(
            "Buổi áp dụng",
            ["Buổi 1","Buổi 2","Buổi 3","Buổi 4","Buổi 5","Buổi 6"],
            index=["Buổi 1","Buổi 2","Buổi 3","Buổi 4","Buổi 5","Buổi 6"].index(st.session_state.get("buoi", buoi))
            if st.session_state.get("buoi", buoi) in ["Buổi 1","Buổi 2","Buổi 3","Buổi 4","Buổi 5","Buổi 6"] else 0
        )
    if st.button("Trả lời"):
        try:
            sheet2 = get_sheet(st.session_state.get("lop", lop_chon))
            df2 = sheet_to_df(sheet2)
            ts = parse_time_series(df2, buoi_ask)
            stats2 = group_stats_for_buoi(df2, buoi_ask, group_col="Tổ")
            p = preset.lower()
            if "sớm nhất" in p:
                if ts.empty: st.info("Chưa có dữ liệu thời gian cho buổi này.")
                else:
                    r = ts.sort_values("time", ascending=True).iloc[0]
                    st.success(f"✅ Sớm nhất: **{r['Họ và Tên']}** (MSSV **{r['MSSV']}**) lúc **{r['time'].strftime('%H:%M:%S')}**.")
            elif "muộn nhất" in p:
                if ts.empty: st.info("Chưa có dữ liệu thời gian cho buổi này.")
                else:
                    r = ts.sort_values("time", ascending=False).iloc[0]
                    st.success(f"⏰ Muộn nhất: **{r['Họ và Tên']}** (MSSV **{r['MSSV']}**) lúc **{r['time'].strftime('%H:%M:%S')}**.")
            elif "nhiều nhất (theo số lượng)" in p:
                if stats2.empty: st.info("Chưa có dữ liệu thống kê.")
                else:
                    r = stats2.sort_values("present", ascending=False).iloc[0]
                    st.success(f"📊 Nhiều nhất: **Tổ {r['group']}** — **{r['present']}/{r['total']}** (≈ **{r['rate']}%**).")
            elif "ít nhất (theo số lượng)" in p:
                if stats2.empty: st.info("Chưa có dữ liệu thống kê.")
                else:
                    r = stats2.sort_values("present", ascending=True).iloc[0]
                    st.success(f"📊 Ít nhất: **Tổ {r['group']}** — **{r['present']}/{r['total']}** (≈ **{r['rate']}%**).")
            elif "tỷ lệ cao nhất" in p:
                if stats2.empty: st.info("Chưa có dữ liệu thống kê.")
                else:
                    r = stats2.sort_values(["rate","present"], ascending=[False,False]).iloc[0]
                    st.success(f"🏅 Tỷ lệ cao nhất: **Tổ {r['group']}** — **{r['rate']}%** ({r['present']}/{r['total']}).")
            elif "tỷ lệ thấp nhất" in p:
                if stats2.empty: st.info("Chưa có dữ liệu thống kê.")
                else:
                    r = stats2.sort_values(["rate","present"], ascending=[True,True]).iloc[0]
                    st.success(f"📉 Tỷ lệ thấp nhất: **Tổ {r['group']}** — **{r['rate']}%** ({r['present']}/{r['total']}).")
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
            col_buoi = sheet.find(buoi).col
            try:
                col_name = sheet.find("Họ và Tên").col
            except gspread.exceptions.CellNotFound:
                st.error("Không tìm thấy cột 'Họ và Tên' trong sheet.")
                st.stop()
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







