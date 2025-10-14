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
import unicodedata
from difflib import get_close_matches
import datetime  # 👈 dùng module chuẩn, an toàn
import pandas as pd
import altair as alt

# ===================== CẤU HÌNH GOOGLE SHEETS =====================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SHEET_KEY = "1P7SOGsmb2KwBX50MU1Y1iVCYtjTiU7F7jLqgp6Bl8Bo"
WORKSHEET_NAME = "D25A"
VN_TZ = datetime.timezone(datetime.timedelta(hours=7))  # 👈 timezone Việt Nam
# ===================== CHUẨN HÓA PRIVATE KEY & KẾT NỐI =====================
@st.cache_resource
def _get_gspread_client():
    cred = dict(st.secrets["google_service_account"])
    pk = cred.get("private_key", "")
    if not pk:
        raise RuntimeError("Secrets thiếu private_key.")

    # 1) Chuẩn hoá xuống dòng
    if "\\n" in pk:
        pk = pk.replace("\\n", "\n")
    pk = pk.replace("\r\n", "\n").replace("\r", "\n")

    header = "-----BEGIN PRIVATE KEY-----"
    footer = "-----END PRIVATE KEY-----"
    if header not in pk or footer not in pk:
        raise RuntimeError("private_key thiếu header/footer BEGIN/END.")

    # 2) Làm sạch base64 và chuẩn padding
    lines = [ln.strip() for ln in pk.split("\n")]
    try:
        h_idx = lines.index(header)
        f_idx = lines.index(footer)
    except ValueError:
        raise RuntimeError("Định dạng private_key không hợp lệ.")
    body_lines = [ln for ln in lines[h_idx + 1:f_idx] if ln]
    body_raw = re.sub(r"[^A-Za-z0-9+/=]", "", "".join(body_lines))

    body = body_raw.replace("=", "")
    if not body:
        raise RuntimeError("private_key rỗng sau khi làm sạch.")
    rem = len(body) % 4
    if rem:
        body += "=" * (4 - rem)
    try:
        base64.b64decode(body, validate=True)
    except Exception as e:
        svc = cred.get("client_email", "(không rõ)")
        raise RuntimeError(f"❌ private_key lỗi base64: {e}\nService Account: {svc}")

    # 3) Ghép lại PEM chuẩn
    pk_clean = (
        header + "\n" +
        "\n".join(body[i:i + 64] for i in range(0, len(body), 64)) +
        "\n" + footer + "\n"
    )
    cred["private_key"] = pk_clean

    creds = Credentials.from_service_account_info(cred, scopes=SCOPES)
    return gspread.authorize(creds)

def get_sheet():
    client = _get_gspread_client()
    ss = client.open_by_key(SHEET_KEY)
    return ss.worksheet(WORKSHEET_NAME)

# ===================== TIỆN ÍCH & TÌM KIẾM =====================
def get_query_params():
    if hasattr(st, "query_params"):
        return dict(st.query_params)
    raw = st.experimental_get_query_params()
    return {k: (v[0] if isinstance(v, list) and v else v) for k, v in raw.items()}

def normalize_name(name: str):
    return " ".join(w.capitalize() for w in name.strip().split())

def strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return unicodedata.normalize("NFC", s)

def norm_search(s: str) -> str:
    return " ".join(strip_accents(s).lower().split())

def load_records(sheet):
    return sheet.get_all_records(expected_headers=None, default_blank="")

def find_header_col(sheet, header_name):
    return sheet.find(header_name).col

def find_student_candidates(records, query: str):
    q = query.strip()
    if not q:
        return []
    if q.isdigit() and len(q) == 4:
        return [r for r in records if str(r.get("MSSV", "")).strip().endswith(q)]
    qn = norm_search(q)
    contains = [r for r in records if qn in norm_search(r.get("Họ và Tên", ""))]
    if contains:
        return contains
    names = [r.get("Họ và Tên", "") for r in records]
    name_map = {n: r for n, r in zip(names, records)}
    close = get_close_matches(q, names, n=5, cutoff=0.6)
    if not close:
        names_no = [norm_search(n) for n in names]
        name_map_no = {norm_search(n): n for n in names}
        close_no = get_close_matches(qn, names_no, n=5, cutoff=0.6)
        close = [name_map_no[c] for c in close_no]
    return [name_map[n] for n in close]

def attendance_flag(val):
    return str(val).strip() != ""

# ===================== GHI THỜI GIAN CẠNH CỘT BUỔI =====================
def find_or_create_time_col(sheet, buoi_col: int, buoi_header: str) -> int:
    """
    Trả về index cột 'Thời gian' tương ứng với cột buổi:
    - Ưu tiên cột ngay bên phải nếu tiêu đề chứa 'thời gian' hoặc 'time'.
    - Nếu không, dò theo số buổi (VD: 'Buổi 3' -> 'Thời gian 3') ở bất kỳ vị trí nào.
    - Nếu vẫn không thấy, đặt tiêu đề 'Thời gian {buoi_header}' ở cột bên phải và dùng cột đó.
    """
    headers = sheet.row_values(1)
    n_cols = len(headers)

    # 1) Cột ngay bên phải
    next_col = buoi_col + 1
    if next_col <= n_cols:
        next_header = headers[next_col - 1] or ""
        if ("thời gian" in next_header.lower()) or ("time" in next_header.lower()):
            return next_col

    # 2) Dò theo số buổi (Buổi 1/2/3 => Thời gian 1/2/3)
    m = re.search(r"(\d+)", buoi_header or "", flags=re.I)
    buoi_idx = m.group(1) if m else None
    if buoi_idx:
        for idx, h in enumerate(headers, start=1):
            hlow = (h or "").lower()
            if (("thời gian" in hlow) or ("time" in hlow)) and re.search(rf"\b{buoi_idx}\b", hlow):
                return idx

    # 3) Không thấy -> đặt tiêu đề ở cột ngay phải (kể cả khi cột tiêu đề trống)
    sheet.update_cell(1, next_col, f"Thời gian {buoi_header}")
    return next_col

# ===================== AUTH GIẢNG VIÊN =====================
def gv_unlocked() -> bool:
    return bool(st.session_state.get("gv_unlocked"))

def render_gv_auth():
    if gv_unlocked():
        with st.sidebar:
            st.success("👨‍🏫 GV: đã đăng nhập")
            if st.button("Đăng xuất"):
                st.session_state.clear()
                st.rerun()
        return

    teacher_pw = st.secrets.get("teacher_password", "")
    with st.sidebar.expander("🔒 Đăng nhập Giảng viên", expanded=False):
        pw = st.text_input("Mật khẩu GV", type="password")
        if st.button("Đăng nhập"):
            if teacher_pw and pw == teacher_pw:
                st.session_state["gv_unlocked"] = True
                st.rerun()
            else:
                st.warning("Sai mật khẩu hoặc chưa cấu hình teacher_password trong Secrets.")

# ===================== GIAO DIỆN STREAMLIT =====================
st.set_page_config(page_title="QR Lecturer", layout="wide")
qp = get_query_params()

# KHÔNG phải SV vào bằng QR thì hiển thị form đăng nhập GV
if qp.get("sv") != "1":
    render_gv_auth()

# ===================== MÀN HÌNH SINH VIÊN (?sv=1&buoi=...) =====================
if qp.get("sv") == "1":
    buoi_sv = qp.get("buoi", "Buổi 1")
    lock_key = f"locked_{buoi_sv}"        # khóa theo từng buổi (theo session trình duyệt)
    info_key = f"lock_info_{buoi_sv}"

    st.title("🎓 Điểm danh sinh viên")
    st.info(f"Bạn đang điểm danh cho **{buoi_sv}**")

    # Nếu đã khóa phiên này -> chỉ hiển thị thông tin, không hiện form nữa
    if st.session_state.get(lock_key):
        st.success(st.session_state.get(info_key, "Bạn đã điểm danh thành công."))
        st.stop()

    # Form nhập
    mssv = st.text_input("Nhập MSSV")
    hoten = st.text_input("Nhập họ và tên")

    if st.button("✅ Xác nhận điểm danh", use_container_width=True):
        if not mssv.strip().isdigit():
            st.warning("⚠️ MSSV phải là số.")
        elif not hoten.strip():
            st.warning("⚠️ Vui lòng nhập họ và tên.")
        else:
            try:
                sheet = get_sheet()
                col_buoi = find_header_col(sheet, buoi_sv)
                cell_mssv = sheet.find(str(mssv).strip())

                # Kiểm tra họ tên khớp
                hoten_sheet = sheet.cell(cell_mssv.row, find_header_col(sheet, "Họ và Tên")).value
                if normalize_name(hoten_sheet or "") != normalize_name(hoten):
                    st.error("❌ Họ tên không khớp với MSSV trong danh sách.")
                    st.stop()

                # Nếu đã có dấu điểm danh trước đó -> không ghi đè, khoá phiên
                curr_mark = (sheet.cell(cell_mssv.row, col_buoi).value or "").strip()
                if curr_mark:
                    # Đọc thời gian đã ghi (nếu có)
                    try:
                        time_col = find_or_create_time_col(sheet, col_buoi, buoi_sv)
                        exist_time = sheet.cell(cell_mssv.row, time_col).value or ""
                    except Exception:
                        exist_time = ""
                    msg = f"✅ MSSV **{mssv}** đã điểm danh trước đó" + (f" lúc **{exist_time}**." if exist_time else ".")
                    st.info(msg)
                    st.session_state[lock_key] = True
                    st.session_state[info_key] = msg
                    st.rerun()

                # Chưa có -> tiến hành ghi ✅ và thời gian
                sheet.update_cell(cell_mssv.row, col_buoi, "✅")
                time_col = find_or_create_time_col(sheet, col_buoi, buoi_sv)
                now_str = datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M:%S")
                sheet.update_cell(cell_mssv.row, time_col, now_str)

                msg = f"🎉 Điểm danh thành công! MSSV **{mssv}** ({now_str})."
                st.success(msg)
                st.session_state[lock_key] = True
                st.session_state[info_key] = msg
                st.rerun()

            except Exception as e:
                st.error(f"❌ Lỗi khi điểm danh: {e}")
    st.stop()

# ===================== MÀN HÌNH GIẢNG VIÊN & CÔNG CỤ =====================
st.title("📋 Hệ thống điểm danh QR")

if gv_unlocked():
    tab_gv, tab_search, tab_stats = st.tabs(
        ["👨‍🏫 Giảng viên (QR động)", "🔎 Tìm kiếm", "📊 Thống kê"]
    )
else:
    st.info("🔒 Tab Giảng viên đang khóa. Vào **Sidebar → Đăng nhập Giảng viên** để mở.")
    tab_gv = None
    tab_search, tab_stats = st.tabs(["🔎 Tìm kiếm", "📊 Thống kê"])

# ---------- TAB GIẢNG VIÊN (chỉ hiển thị khi đã đăng nhập) ----------
if tab_gv is not None:
    with tab_gv:
        st.subheader("📸 Tạo mã QR điểm danh (động mỗi 30 giây)")
        buoi = st.selectbox(
            "Chọn buổi học",
            ["Buổi 1", "Buổi 2", "Buổi 3", "Buổi 4", "Buổi 5", "Buổi 6"],
            index=0,
            key="buoi_gv_select",
        )

        auto = st.toggle("Tự đổi QR mỗi 30 giây", value=True)
        show_link = st.toggle(
            "🔎 Hiển thị link chi tiết (ẩn/hiện)", value=False,
            help="Bật khi cần xem toàn bộ URL để debug"
        )
        go = st.button("Tạo mã QR", use_container_width=True, type="primary")

        if go:
            # placeholder cố định -> mỗi vòng ghi đè, không sinh widget mới
            qr_slot = st.empty()       # ảnh QR
            link_slot = st.empty()     # chỗ hiển thị link (nếu bật)
            timer_slot = st.empty()    # đồng hồ đếm ngược

            try:
                while True:
                    now = int(time.time())
                    slot_val = now // 30
                    token = f"{slot_val}"
                    base_url = st.secrets["google_service_account"].get(
                        "app_base_url", "https://qrlecturer.streamlit.app"
                    )
                    qr_data = f"{base_url}/?sv=1&buoi={urllib.parse.quote(buoi)}&t={token}"

                    # Tạo ảnh QR
                    qr = qrcode.make(qr_data)
                    buf = io.BytesIO()
                    qr.save(buf, format="PNG")
                    buf.seek(0)
                    img = Image.open(buf)

                    # Cập nhật ảnh QR
                    qr_slot.image(img, caption="📱 Quét mã để điểm danh", width=260)

                    # Hiển thị link (click được) + ô copy (không là widget nên không lỗi ID)
                    if show_link:
                        with link_slot.container():
                            st.markdown(
                                f'<a href="{qr_data}" target="_blank" rel="noopener noreferrer">🌐 Mở link hiện tại</a>',
                                unsafe_allow_html=True
                            )
                            st.code(qr_data)
                    else:
                        link_slot.empty()

                    # Đồng hồ đếm ngược
                    remain = 30 - (now % 30)
                    timer_slot.markdown(f"⏳ QR đổi sau: **{remain} giây**  •  Buổi: **{buoi}**")

                    if not auto:
                        break
                    time.sleep(1)

            except Exception as e:
                st.error(f"❌ Lỗi khi tạo QR: {e}")

# ---------- TAB TÌM KIẾM ----------
with tab_search:
    st.subheader("🔎 Tìm sinh viên (4 số cuối MSSV hoặc họ và tên)")
    q = st.text_input("Nhập từ khóa tìm kiếm", placeholder="VD: 1234 hoặc 'Nguyen Van A'")
    run = st.button("Tìm", use_container_width=True)

    if run and q.strip():
        try:
            sheet = get_sheet()
            records = load_records(sheet)
            results = find_student_candidates(records, q)

            if not results:
                st.warning("🙁 Không tìm thấy kết quả phù hợp.")
            else:
                st.success(f"Tìm thấy {len(results)} kết quả:")
                show_cols = list(records[0].keys()) if records else []
                pref = ["MSSV", "Họ và Tên", "Tổ"]
                buoi_cols = [c for c in show_cols if c.lower().startswith("buổi ")]
                cols = [c for c in pref if c in show_cols] + buoi_cols

                tidy = []
                for r in results:
                    row = {c: r.get(c, "") for c in cols}
                    for bc in buoi_cols:
                        row[bc] = "✅" if attendance_flag(r.get(bc, "")) else ""
                    tidy.append(row)
                st.dataframe(tidy, use_container_width=True)
        except Exception as e:
            st.error(f"❌ Lỗi khi tìm kiếm: {e}")

# ---------- TAB THỐNG KÊ ----------
with tab_stats:
    st.subheader("📊 Thống kê điểm danh theo buổi & theo Tổ")
    try:
        sheet = get_sheet()
        headers = sheet.row_values(1)
        buoi_list = [h for h in headers if h.lower().startswith("buổi ")]
        buoi_chon = st.selectbox("Chọn buổi", buoi_list or ["Buổi 1"], index=0)
        records = load_records(sheet)

        present, absent = 0, 0
        by_group = {}
        for r in records:
            flag = attendance_flag(r.get(buoi_chon, ""))
            if flag:
                present += 1
            else:
                absent += 1
            group = str(r.get("Tổ", "")).strip() or "Chưa rõ"
            if group not in by_group:
                by_group[group] = {"present": 0, "absent": 0}
            if flag:
                by_group[group]["present"] += 1
            else:
                by_group[group]["absent"] += 1

        # Tổng quan
        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("✅ Có mặt", present)
        with c2:
            st.metric("❌ Vắng", absent)
        with c3:
            total = present + absent
            rate_total = f"{(present / total * 100):.1f}%" if total else "-"
            st.metric("📈 Tỷ lệ có mặt", rate_total)

        # Chuẩn bị dữ liệu theo Tổ để vẽ biểu đồ
        rows = []
        for g, v in sorted(by_group.items()):
            total_g = v["present"] + v["absent"]
            rate = (v["present"] / total_g * 100) if total_g else 0.0
            rows.append({
                "Tổ": g,
                "Có mặt": v["present"],
                "Vắng": v["absent"],
                "Tổng": total_g,
                "Tỷ lệ (%)": round(rate, 1),
                "Nhãn": f"{v['present']} ({rate:.1f}%)"
            })
        df = pd.DataFrame(rows)

        # Biểu đồ cột: mỗi Tổ một màu, có nhãn "Số (Tỷ lệ%)" + tooltip khi rê chuột
        if not df.empty:
            base = alt.Chart(df).encode(
                x=alt.X('Tổ:N', sort=None, title='Tổ'),
                y=alt.Y('Có mặt:Q', title='Số SV có mặt'),
                color=alt.Color('Tổ:N', legend=None),
                tooltip=[
                    alt.Tooltip('Tổ:N', title='Tổ'),
                    alt.Tooltip('Có mặt:Q', title='Có mặt'),
                    alt.Tooltip('Vắng:Q', title='Vắng'),
                    alt.Tooltip('Tổng:Q', title='Tổng'),
                    alt.Tooltip('Tỷ lệ (%):Q', title='Tỷ lệ (%)')
                ]
            )
            bars = base.mark_bar()
            text = base.mark_text(dy=-5).encode(text='Nhãn:N')  # nhãn trên cột
            chart = (bars + text).properties(height=340)
            st.altair_chart(chart, use_container_width=True)
        else:
            st.info("Không có dữ liệu để vẽ biểu đồ.")


        # Bảng thống kê đặt dưới biểu đồ
        table = []
        for g, v in sorted(by_group.items()):
            total_g = v["present"] + v["absent"]
            if total_g:
                rate_g = f"{(v['present'] / total_g * 100):.1f}%"
            else:
                rate_g = "-"
            table.append({
                "Tổ": g,
                "Có mặt": v["present"],
                "Vắng": v["absent"],
                "Tỷ lệ có mặt": rate_g
            })
        st.dataframe(table, use_container_width=True)

    except Exception as e:
        st.error(f"❌ Lỗi khi lấy thống kê: {e}")



