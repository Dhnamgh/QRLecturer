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

# ===================== CẤU HÌNH GOOGLE SHEETS =====================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SHEET_KEY = "1P7SOGsmb2KwBX50MU1Y1iVCYtjTiU7F7jLqgp6Bl8Bo"  # <-- thay bằng ID thật của Google Sheet
WORKSHEET_NAME = "D25A"  # <-- thay bằng tên sheet thật

# ============== HỖ TRỢ: CHUẨN HÓA PRIVATE KEY & KẾT NỐI ==============
@st.cache_resource
def _get_gspread_client():
    """
    Kết nối Google Sheets và tự động 'sửa khóa' nếu định dạng private_key bị lỗi:
    - '\\n' vs xuống dòng thật
    - kí tự lạ / khoảng trắng
    - padding base64 ('Incorrect padding', 'Excess data after padding', 'Short substrate on input', ...)
    """
    cred = dict(st.secrets["google_service_account"])
    pk = cred.get("private_key", "")
    if not pk:
        raise RuntimeError("Secrets thiếu 'private_key'.")

    # 1) Chuẩn hóa xuống dòng
    if "\\n" in pk:
        pk = pk.replace("\\n", "\n")
    pk = pk.replace("\r\n", "\n").replace("\r", "\n")

    header = "-----BEGIN PRIVATE KEY-----"
    footer = "-----END PRIVATE KEY-----"
    if header not in pk or footer not in pk:
        raise RuntimeError("private_key thiếu header/footer BEGIN/END PRIVATE KEY.")

    # 2) Lấy phần thân base64 giữa header/footer và làm sạch
    lines = [ln.strip() for ln in pk.split("\n")]
    try:
        h_idx = lines.index(header)
        f_idx = lines.index(footer)
    except ValueError:
        raise RuntimeError("Định dạng private_key không hợp lệ (không tìm thấy header/footer).")

    body_lines = [ln for ln in lines[h_idx + 1 : f_idx] if ln]
    # Chỉ giữ Base64-char để tránh ký tự lạ
    body_raw = re.sub(r"[^A-Za-z0-9+/=]", "", "".join(body_lines))

    # 3) Bỏ padding cũ rồi thêm padding mới theo mod 4
    body_str = body_raw.replace("=", "")
    if not body_str:
        raise RuntimeError("private_key base64 rỗng sau khi làm sạch.")
    rem = len(body_str) % 4
    if rem != 0:
        body_str += "=" * (4 - rem)

    # 4) Thử decode để bắt mọi lỗi base64 (Short substrate..., Incorrect padding,...)
    try:
        base64.b64decode(body_str, validate=True)
    except Exception as e:
        svc = cred.get("client_email", "(không lấy được)")
        raise RuntimeError(
            "❌ private_key trong secrets bị hỏng hoặc thiếu ký tự.\n"
            "Hãy tạo key JSON mới và copy nguyên văn (không thêm ...).\n"
            f"Service Account: {svc}\nLỗi gốc: {e}"
        )

    # 5) Reflow lại PEM: 64 ký tự mỗi dòng
    pk_clean = header + "\n"
    for i in range(0, len(body_str), 64):
        pk_clean += body_str[i : i + 64] + "\n"
    pk_clean += footer + "\n"

    cred["private_key"] = pk_clean

    # 6) Tạo credentials và trả về client
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
        # st.query_params đã là dict-like
        return dict(st.query_params)
    else:
        raw = st.experimental_get_query_params()
        return {k: (v[0] if isinstance(v, list) and v else v) for k, v in raw.items()}

def normalize_name(name: str):
    """Viết hoa chữ cái đầu mỗi từ (dùng cho so khớp chính xác)"""
    return " ".join(w.capitalize() for w in name.strip().split())

def strip_accents(s: str) -> str:
    """Bỏ dấu tiếng Việt để tìm kiếm gần đúng (AI-ish fuzzy)"""
    s = unicodedata.normalize("NFD", s)
    s = "".join(ch for ch in s if unicodedata.category(ch) != "Mn")
    return unicodedata.normalize("NFC", s)

def norm_search(s: str) -> str:
    """Chuẩn hóa cho tìm kiếm: bỏ dấu, lower, bỏ khoảng thừa"""
    return " ".join(strip_accents(s).lower().split())

def load_records(sheet):
    """
    Tải toàn bộ dữ liệu dưới dạng list[dict].
    Cần các cột: 'MSSV', 'Họ và Tên', 'Tổ', 'Buổi 1'... (tên cột 'Họ và Tên' đã dùng trong mã gốc)
    """
    return sheet.get_all_records(expected_headers=None, default_blank="")

def find_header_col(sheet, header_name):
    """Tìm vị trí cột theo tiêu đề (dùng khi update/check nhanh)"""
    return sheet.find(header_name).col

def find_student_candidates(records, query: str):
    """
    Tìm SV theo:
    - 4 số cuối MSSV (nếu query là 4 chữ số)
    - hoặc họ tên (gần đúng: contains + difflib.get_close_matches)
    Trả về list[dict] (các dòng khớp).
    """
    q = query.strip()
    if not q:
        return []

    # 1) MSSV 4 số cuối
    if q.isdigit() and len(q) == 4:
        return [r for r in records if str(r.get("MSSV", "")).strip().endswith(q)]

    # 2) Họ tên gần đúng
    qn = norm_search(q)
    # Ưu tiên contains (không dấu)
    contains = [r for r in records if qn in norm_search(r.get("Họ và Tên", ""))]
    if contains:
        return contains

    # Sau đó dùng gần đúng theo difflib
    names = [r.get("Họ và Tên", "") for r in records]
    name_map = {n: r for n, r in zip(names, records)}
    # lấy 5 kết quả gần nhất
    close = get_close_matches(q, names, n=5, cutoff=0.6)
    if not close:
        # thử không dấu
        names_no = [norm_search(n) for n in names]
        name_map_no = {norm_search(n): n for n in names}
        close_no = get_close_matches(qn, names_no, n=5, cutoff=0.6)
        close = [name_map_no[c] for c in close_no]
    return [name_map[n] for n in close]

def attendance_flag(val):
    """Xác định đã điểm danh hay chưa (coi mọi giá trị khác rỗng là có mặt)"""
    return str(val).strip() != ""

# ===================== GIAO DIỆN STREAMLIT =====================
st.set_page_config(page_title="QR Lecturer", layout="wide")
qp = get_query_params()

# Nếu URL có sv=1 hoặc buoi=... thì chỉ hiển thị form SV
student_only = (qp.get("sv") == "1") or ("buoi" in qp)

# ===================== MÀN HÌNH SINH VIÊN =====================
if student_only:
    buoi_sv = qp.get("buoi", "Buổi 1")
    st.title("🎓 Điểm danh sinh viên")
    st.info(f"Bạn đang điểm danh cho **{buoi_sv}**")

    st.write("Mã số sinh viên: 51125", unsafe_allow_html=True)
    mssv_tail = st.text_input("Nhập 4 số cuối MSSV")
    mssv = "51125" + (mssv_tail or "").strip()
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
                hoten_sheet = sheet.cell(cell_mssv.row, find_header_col(sheet, "Họ và Tên")).value
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

tab_gv, tab_search, tab_stats, tab_sv = st.tabs(
    ["👨‍🏫 Giảng viên", "🔎 Tìm kiếm", "📊 Thống kê", "🎓 Sinh viên"]
)

# ---------- TAB GIẢNG VIÊN ----------
with tab_gv:
    st.subheader("📸 Tạo mã QR điểm danh")
    buoi = st.selectbox(
        "Chọn buổi học",
        ["Buổi 1", "Buổi 2", "Buổi 3", "Buổi 4", "Buổi 5", "Buổi 6"],
        index=0,
        key="buoi_gv_select",
    )

    if st.button("Tạo mã QR", use_container_width=True):
        st.session_state["buoi"] = buoi
        qr_data = f"https://qrlecturer.streamlit.app/?sv=1&buoi={urllib.parse.quote(buoi)}"

        qr = qrcode.make(qr_data)
        buf = io.BytesIO()
        qr.save(buf, format="PNG")
        buf.seek(0)
        img = Image.open(buf)

        st.image(img, caption="📱 Quét mã để điểm danh", width=260)
        st.write(f"🔗 Link: {qr_data}")

        countdown = st.empty()
        for i in range(60, 0, -1):  # hiệu lực 1 phút
            countdown.markdown(f"⏳ Thời gian còn lại: **{i} giây**")
            time.sleep(1)
        countdown.markdown("✅ Hết thời gian điểm danh")

# ---------- TAB TÌM KIẾM (AI trợ giúp gần đúng) ----------
with tab_search:
    st.subheader("🔎 Tìm kiếm sinh viên (4 số cuối MSSV hoặc họ và tên)")
    q = st.text_input("Nhập từ khóa tìm kiếm", placeholder="VD: 1234 hoặc 'Nguyen Van A'")
    run = st.button("Tìm", type="primary", use_container_width=True)

    if run and q.strip():
        try:
            sheet = get_sheet()
            records = load_records(sheet)
            results = find_student_candidates(records, q)

            if not results:
                st.warning("🙁 Không tìm thấy kết quả phù hợp.")
            else:
                st.success(f"Tìm thấy {len(results)} kết quả:")
                # Hiển thị gọn: MSSV, Họ và Tên, Tổ, và trạng thái các buổi (✅/trống)
                show_cols = []
                if records:
                    # Lấy tất cả header từ bản ghi đầu
                    show_cols = list(records[0].keys())

                preferred = ["MSSV", "Họ và Tên", "Tổ"]
                # Ưu tiên 3 cột chính + các cột 'Buổi ...'
                buoi_cols = [c for c in show_cols if c.lower().startswith("buổi ")]
                cols = [c for c in preferred if c in show_cols] + buoi_cols

                # Rút gọn mỗi kết quả theo các cột trên
                tidy = []
                for r in results:
                    row = {c: r.get(c, "") for c in cols}
                    # Chuẩn hóa hiển thị tick
                    for bc in buoi_cols:
                        row[bc] = "✅" if attendance_flag(r.get(bc, "")) else ""
                    tidy.append(row)

                st.dataframe(tidy, use_container_width=True)
        except Exception as e:
            st.error(f"❌ Lỗi khi tìm kiếm: {e}")

# ---------- TAB THỐNG KÊ (chia theo Tổ) ----------
with tab_stats:
    st.subheader("📊 Thống kê điểm danh theo buổi & theo Tổ")
    try:
        sheet = get_sheet()
        # Chọn buổi để thống kê (độc lập với tab GV)
        # Tự động dò các cột có dạng "Buổi ..."
        headers = sheet.row_values(1)
        buoi_list = [h for h in headers if h.lower().startswith("buổi ")]
        buoi_chon = st.selectbox("Chọn buổi", buoi_list or ["Buổi 1"], index=0, key="buoi_stats_select")

        # Tải dữ liệu
        records = load_records(sheet)

        # Tổng hợp
        present_count = 0
        absent_count = 0
        by_group = {}  # {Tổ: {"present": x, "absent": y}}

        for r in records:
            flag = attendance_flag(r.get(buoi_chon, ""))
            if flag:
                present_count += 1
            else:
                absent_count += 1
            group = str(r.get("Tổ", "")).strip() or "Chưa rõ"
            if group not in by_group:
                by_group[group] = {"present": 0, "absent": 0}
            by_group[group]["present" if flag else "absent"] += 1

        c1, c2 = st.columns(2)
        with c1:
            st.metric("✅ Đã điểm danh", present_count)
        with c2:
            st.metric("❌ Vắng mặt", absent_count)

        st.markdown("#### 📌 Phân bố theo Tổ")
        # Bảng theo tổ
        table = []
        for g, v in sorted(by_group.items(), key=lambda x: str(x[0])):
            total = v["present"] + v["absent"]
            rate = f"{(v['present'] / total * 100):.1f}%" if total else "-"
            table.append(
                {"Tổ": g, "Có mặt": v["present"], "Vắng": v["absent"], "Tỷ lệ có mặt": rate}
            )
        st.dataframe(table, use_container_width=True)

    except Exception as e:
        st.error(f"❌ Lỗi khi lấy thống kê: {e}")

# ---------- TAB SINH VIÊN (DỰ PHÒNG) ----------
with tab_sv:
    st.subheader("📲 Nhập thông tin điểm danh (dành cho SV)")
    mssv = st.text_input("Nhập MSSV")
    hoten = st.text_input("Nhập họ và tên")
    buoi_sv = st.session_state.get("buoi", "Buổi 1")

    if st.button("Điểm danh", use_container_width=True):
        try:
            sheet = get_sheet()
            col_buoi = find_header_col(sheet, buoi_sv)
            cell_mssv = sheet.find(str(mssv).strip())
            hoten_sheet = sheet.cell(cell_mssv.row, find_header_col(sheet, "Họ và Tên")).value
            if normalize_name(hoten_sheet or "") != normalize_name(hoten):
                st.error("❌ Họ tên không khớp với MSSV trong danh sách.")
            else:
                sheet.update_cell(cell_mssv.row, col_buoi, "✅")
                st.success("🎉 Điểm danh thành công!")
        except Exception as e:
            st.error(f"❌ Lỗi khi điểm danh: {e}")
