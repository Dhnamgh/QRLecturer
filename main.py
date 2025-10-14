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
import datetime  # dùng module chuẩn để tránh shadow
def current_slot(now=None, step=30):
    import time as _t
    return int((_t.time() if now is None else now) // step)

def token_valid(t_str: str, step=30, strict=True) -> bool:
    # strict=True: bắt buộc đúng slot hiện tại (không ±1)
    if not t_str or not str(t_str).isdigit():
        return False
    t = int(t_str)
    now_slot = current_slot(step=step)
    if strict:
        return t == now_slot
    # lỡ mạng trễ có thể nới lỏng ±1 (không khuyến nghị)
    return abs(t - now_slot) <= 1

# ===================== CẤU HÌNH GOOGLE SHEETS =====================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SHEET_KEY = "1P7SOGsmb2KwBX50MU1Y1iVCYtjTiU7F7jLqgp6Bl8Bo"  # ID file Sheet của bạn
WORKSHEET_NAME = "D25C"  # Tên sheet con trong Google Sheets
VN_TZ = datetime.timezone(datetime.timedelta(hours=7))

# ===================== GUARD SECRETS VÀ ĐĂNG NHẬP GV =====================
st.set_page_config(page_title="QR Lecturer", layout="wide")

def _get_teacher_pw():
    # Ưu tiên root, nếu không có thì thử trong block google_service_account
    if "teacher_password" in st.secrets:
        return st.secrets["teacher_password"]
    if "google_service_account" in st.secrets:
        return st.secrets["google_service_account"].get("teacher_password")
    return None

# Chặn chạy nếu thiếu secrets quan trọng (trừ khi vào đường SV)
_qp_boot = st.query_params if hasattr(st, "query_params") else st.experimental_get_query_params()
_is_sv_boot = (_qp_boot.get("sv") == "1" if isinstance(_qp_boot, dict) else (_qp_boot.get("sv", [""])[0] == "1"))
if not _is_sv_boot:
    missing = []
    if not _get_teacher_pw():
        missing.append("teacher_password")
    if "google_service_account" not in st.secrets:
        missing.append("[google_service_account]")
    if missing:
        st.error("🔒 App bị khóa vì thiếu Secrets: " + ", ".join(missing) + ". Vào Settings → Secrets để cấu hình rồi reload.")
        st.stop()

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
    with st.sidebar.expander("🔒 Đăng nhập Giảng viên", expanded=True):
        pw_input = st.text_input("Mật khẩu GV", type="password")
        if st.button("Đăng nhập"):
            if _get_teacher_pw() and pw_input == _get_teacher_pw():
                st.session_state["gv_unlocked"] = True
                st.rerun()
            else:
                st.warning("Sai mật khẩu hoặc chưa cấu hình teacher_password trong Secrets.")

# ===================== HÀM CHUẨN HÓA PRIVATE KEY & KẾT NỐI =====================
@st.cache_resource
def _get_gspread_client():
    cred = dict(st.secrets["google_service_account"])
    pk = cred.get("private_key", "")
    if not pk:
        raise RuntimeError("Secrets thiếu private_key.")

    # 1. Chuẩn hóa xuống dòng
    if "\\n" in pk:
        pk = pk.replace("\\n", "\n")
    pk = pk.replace("\r\n", "\n").replace("\r", "\n")

    header = "-----BEGIN PRIVATE KEY-----"
    footer = "-----END PRIVATE KEY-----"
    if header not in pk or footer not in pk:
        raise RuntimeError("private_key thiếu header/footer BEGIN/END.")

    # 2. Lọc ký tự hợp lệ
    lines = [ln.strip() for ln in pk.split("\n")]
    try:
        h_idx = lines.index(header)
        f_idx = lines.index(footer)
    except ValueError:
        raise RuntimeError("Định dạng private_key không hợp lệ.")
    body_lines = [ln for ln in lines[h_idx + 1:f_idx] if ln]
    body_raw = re.sub(r"[^A-Za-z0-9+/=]", "", "".join(body_lines))

    # 3. Chuẩn hóa padding base64
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

    # 4. Ghép lại PEM chuẩn
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

# ===================== TIỆN ÍCH =====================
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

# ======= THỜI GIAN BÊN CẠNH CỘT BUỔI =======
def find_or_create_time_col(sheet, buoi_col: int, buoi_header: str) -> int:
    """Tìm 'Thời gian ...' ngay bên phải cột buổi; nếu chưa có thì tạo."""
    headers = sheet.row_values(1)
    n_cols = len(headers)
    right_col = buoi_col + 1
    # Nếu cột bên phải đã là "Thời gian ..."
    if right_col <= n_cols:
        h = (headers[right_col - 1] or "").lower()
        if ("thời gian" in h) or ("time" in h):
            return right_col
    # Dò theo số buổi (Buổi 1 -> Thời gian 1)
    m = re.search(r"(\d+)", buoi_header or "", flags=re.I)
    idx = m.group(1) if m else None
    if idx:
        for i, h in enumerate(headers, start=1):
            hl = (h or "").lower()
            if (("thời gian" in hl) or ("time" in hl)) and re.search(rf"\b{idx}\b", hl):
                return i
    # Không thấy -> tạo ở cột bên phải
    sheet.update_cell(1, right_col, f"Thời gian {buoi_header}")
    return right_col

# ===================== GIAO DIỆN =====================
qp = get_query_params()

# ===================== MÀN HÌNH SINH VIÊN =====================
if qp.get("sv") == "1":
    buoi_sv = qp.get("buoi", "Buổi 1")
    token_qr = qp.get("t", "")  # lấy token từ QR (đổi mỗi 30s)
    lock_key = f"locked_{buoi_sv}"
    info_key = f"lock_info_{buoi_sv}"

    st.title("🎓 Điểm danh sinh viên")
    st.info(f"Bạn đang điểm danh cho **{buoi_sv}**")

    # Nếu SV đã điểm danh trong phiên này → khóa form
    if st.session_state.get(lock_key):
        st.success(st.session_state.get(info_key, "Bạn đã điểm danh thành công."))
        st.stop()

    # 🔒 Kiểm tra token hợp lệ (chống refresh / điểm danh hộ)
    def current_slot(now=None, step=30):
        import time as _t
        return int((_t.time() if now is None else now) // step)

    def token_valid(t_str: str, step=30, strict=True) -> bool:
        if not t_str or not str(t_str).isdigit():
            return False
        t = int(t_str)
        now_slot = current_slot(step=step)
        if strict:
            return t == now_slot
        return abs(t - now_slot) <= 1  # nới lỏng ±1 nếu mạng trễ (không khuyến khích)

    if not token_valid(token_qr, step=30, strict=True):
        st.error("⏳ Link điểm danh đã hết hạn hoặc không hợp lệ. "
                 "Vui lòng **quét mã QR đang chiếu** để mở form mới.")
        import time as _t
        remain = 30 - (int(_t.time()) % 30)
        st.caption(f"Gợi ý: mã QR đổi sau khoảng {remain} giây.")
        st.stop()

    # Form nhập (chỉ hiển thị khi token còn hiệu lực)
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
                hoten_sheet = sheet.cell(
                    cell_mssv.row,
                    find_header_col(sheet, "Họ và Tên")
                ).value
                if normalize_name(hoten_sheet or "") != normalize_name(hoten):
                    st.error("❌ Họ tên không khớp với MSSV trong danh sách.")
                    st.stop()

                # Kiểm tra đã điểm danh chưa
                curr_mark = (sheet.cell(cell_mssv.row, col_buoi).value or "").strip()
                time_col = find_or_create_time_col(sheet, col_buoi, buoi_sv)
                if curr_mark:
                    exist_time = sheet.cell(cell_mssv.row, time_col).value or ""
                    msg = f"✅ MSSV **{mssv}** đã điểm danh trước đó" + (
                        f" lúc **{exist_time}**." if exist_time else ".")
                    st.info(msg)
                    st.session_state[lock_key] = True
                    st.session_state[info_key] = msg
                    st.rerun()

                # Ghi ✅ và thời gian thực
                sheet.update_cell(cell_mssv.row, col_buoi, "✅")
                now_str = datetime.datetime.now(VN_TZ).strftime("%Y-%m-%d %H:%M:%S")
                sheet.update_cell(cell_mssv.row, time_col, now_str)

                msg = f"🎉 Điểm danh thành công! MSSV **{mssv}** ({now_str})."
                st.success(msg)
                st.session_state[lock_key] = True
                st.session_state[info_key] = msg
                st.rerun()

            except Exception as e:
                st.error(f"❌ Lỗi khi điểm danh: {e}")

    st.stop()


# ===================== MÀN HÌNH GIẢNG VIÊN (bắt buộc đăng nhập) =====================
render_gv_auth()  # hiển thị khối đăng nhập ở Sidebar
st.title("📋 Hệ thống điểm danh QR")

if not gv_unlocked():
    st.error("🔒 Bạn chưa đăng nhập Giảng viên. Vào **Sidebar → Đăng nhập Giảng viên** để mở khóa.")
    st.stop()

tab_gv, tab_search, tab_stats, tab_ai = st.tabs(
    ["👨‍🏫 Giảng viên (QR động)", "🔎 Tìm kiếm", "📊 Thống kê", "🤖 Trợ lý AI"]
)


# ---------- TAB GIẢNG VIÊN ----------
with tab_gv:
    st.subheader("📸 Tạo mã QR điểm danh (động mỗi 30 giây)")
    buoi = st.selectbox(
        "Chọn buổi học",
        ["Buổi 1", "Buổi 2", "Buổi 3", "Buổi 4", "Buổi 5", "Buổi 6"],
        index=0,
        key="buoi_gv_select",
    )
    auto = st.toggle("Tự đổi QR mỗi 30 giây", value=True)
    show_link = st.toggle("🔎 Hiển thị link chi tiết (ẩn/hiện)", value=False)
    go = st.button("Tạo mã QR", use_container_width=True, type="primary")

    if go:
        qr_slot = st.empty()
        link_slot = st.empty()
        timer_slot = st.empty()
        try:
            while True:
                now = int(time.time())
                slot = now // 30
                token = f"{slot}"
                base_url = st.secrets["google_service_account"].get("app_base_url", "https://qrlecturer.streamlit.app")
                qr_data = f"{base_url}/?sv=1&buoi={urllib.parse.quote(buoi)}&t={token}"

                # QR image
                qr = qrcode.make(qr_data)
                buf = io.BytesIO(); qr.save(buf, format="PNG"); buf.seek(0)
                img = Image.open(buf)
                qr_slot.image(img, caption="📱 Quét mã để điểm danh", width=260)

                if show_link:
                    with link_slot.container():
                        st.markdown(f'<a href="{qr_data}" target="_blank" rel="noopener noreferrer">🌐 Mở link hiện tại</a>', unsafe_allow_html=True)
                        st.code(qr_data)
                else:
                    link_slot.empty()

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
            by_group[group]["present" if flag else "absent"] += 1

        # Chỉ số tổng quan
        c1, c2, c3 = st.columns(3)
        with c1: st.metric("✅ Có mặt", present)
        with c2: st.metric("❌ Vắng", absent)
        with c3:
            total = present + absent
            st.metric("📈 Tỷ lệ có mặt", f"{(present/total*100):.1f}%" if total else "-")

        # Chuẩn bị dữ liệu cho biểu đồ
        rows = []
        for g, v in sorted(by_group.items()):
            total_g = v["present"] + v["absent"]
            rate = (v["present"]/total_g*100) if total_g else 0.0
            rows.append({
                "Tổ": g,
                "Có mặt": v["present"],
                "Vắng": v["absent"],
                "Tổng": total_g,
                "Tỷ lệ (%)": round(rate, 1),
                "Nhãn": f"{v['present']} ({rate:.1f}%)"
            })
        import pandas as pd  # an toàn nếu bạn quên import trên đầu
        import altair as alt
        df = pd.DataFrame(rows)

        # Biểu đồ cột: mỗi tổ một màu + tooltip + nhãn
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
            text = base.mark_text(dy=-5).encode(text='Nhãn:N')
            chart = (bars + text).properties(height=340)
            st.altair_chart(chart, use_container_width=True)
        else:
            st.info("Không có dữ liệu để vẽ biểu đồ.")

        # Bảng thống kê dưới biểu đồ
        table = []
        for g, v in sorted(by_group.items()):
            total_g = v["present"] + v["absent"]
            rate_g = f"{(v['present']/total_g*100):.1f}%" if total_g else "-"
            table.append({"Tổ": g, "Có mặt": v["present"], "Vắng": v["absent"], "Tỷ lệ có mặt": rate_g})
        st.dataframe(table, use_container_width=True)

    except Exception as e:
        st.error(f"❌ Lỗi khi lấy thống kê: {e}")
# ---------- TAB TRỢ LÝ AI ----------
with tab_ai:
    st.subheader("🤖 Trợ lý AI phân tích điểm danh")
    st.caption("Nhập câu hỏi bằng tiếng Việt tự nhiên. Ví dụ: "
               "“Buổi 2 có bao nhiêu sinh viên đi học?”, "
               "“Sinh viên nào vắng nhiều nhất?”, "
               "hoặc “Tỷ lệ chuyên cần trung bình là bao nhiêu?”")

    user_q = st.text_input("Câu hỏi của bạn", placeholder="Nhập câu hỏi ở đây...")

    if st.button("Hỏi trợ lý", use_container_width=True) and user_q.strip():
        try:
            sheet = get_sheet()
            records = load_records(sheet)
            if not records:
                st.warning("Không tìm thấy dữ liệu điểm danh trong Sheet.")
                st.stop()

            # Chuẩn hoá
            q = user_q.lower().strip()
            headers = sheet.row_values(1)
            buoi_cols = [h for h in headers if h.lower().startswith("buổi ")]
            # Tạo bảng tổng hợp
            summary = {}
            for b in buoi_cols:
                present = sum(1 for r in records if str(r.get(b, "")).strip() != "")
                total = len(records)
                summary[b] = {"present": present, "absent": total - present, "total": total}

            def find_buoi_in_text(text):
                for b in buoi_cols:
                    if b.lower() in text:
                        return b
                # thử match số: "buổi 3"
                import re
                m = re.search(r"buổi\s*(\d+)", text)
                if m:
                    num = m.group(1)
                    for b in buoi_cols:
                        if num in b:
                            return b
                return None

            ans = ""
            b = find_buoi_in_text(q)

            # --- Câu hỏi dạng thống kê ---
            if "tổng" in q or "bao nhiêu" in q or "đi học" in q:
                if b:
                    p = summary[b]["present"]
                    t = summary[b]["total"]
                    ans = f"✅ {b}: {p}/{t} sinh viên có mặt ({p/t*100:.1f}%)."
                else:
                    total_present = sum(v["present"] for v in summary.values())
                    total_all = sum(v["total"] for v in summary.values())
                    ans = f"Toàn bộ các buổi có {total_present} lượt điểm danh / {total_all} SV-buổi."

            elif "tổ" in q:
                b = b or buoi_cols[-1]
                group_stats = {}
                for r in records:
                    g = str(r.get("Tổ", "")).strip() or "Chưa rõ"
                    if g not in group_stats:
                        group_stats[g] = {"present": 0, "absent": 0}
                    if str(r.get(b, "")).strip() != "":
                        group_stats[g]["present"] += 1
                    else:
                        group_stats[g]["absent"] += 1
                rows = [f"Tổ {g}: {v['present']} có mặt / {v['present']+v['absent']} SV" 
                        for g, v in group_stats.items()]
                ans = f"📊 Thống kê theo tổ cho {b}:\n" + "\n".join(rows)

            elif "vắng nhiều" in q or "hay nghỉ" in q:
                counts = {}
                for r in records:
                    vangs = sum(1 for b in buoi_cols if str(r.get(b, "")).strip() == "")
                    counts[r["Họ và Tên"]] = vangs
                top5 = sorted(counts.items(), key=lambda x: -x[1])[:5]
                ans = "😴 Sinh viên vắng nhiều nhất:\n" + "\n".join([f"{n}: {v} buổi" for n, v in top5])

            elif "chuyên cần" in q or "tỷ lệ" in q:
                total_present = sum(v["present"] for v in summary.values())
                total_all = sum(v["total"] for v in summary.values())
                rate = total_present / total_all * 100 if total_all else 0
                ans = f"📈 Tỷ lệ chuyên cần trung bình của lớp là {rate:.1f}%."

            elif "danh sách" in q and "vắng" in q:
                limit = 2
                counts = {}
                for r in records:
                    vangs = sum(1 for b in buoi_cols if str(r.get(b, "")).strip() == "")
                    if vangs > limit:
                        counts[r["Họ và Tên"]] = vangs
                if counts:
                    ans = f"Sinh viên vắng quá {limit} buổi:\n" + "\n".join([f"{k}: {v}" for k, v in counts.items()])
                else:
                    ans = f"Không có sinh viên nào vắng quá {limit} buổi."

            else:
                ans = "🤔 Xin lỗi, tôi chưa hiểu câu hỏi này. Hãy thử hỏi dạng: “Buổi 3 có bao nhiêu SV đi học?” hoặc “Tỷ lệ chuyên cần trung bình?”."

            st.markdown(f"**Trả lời:**\n\n{ans}")

        except Exception as e:
            st.error(f"❌ Lỗi khi xử lý câu hỏi: {e}")



