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
SHEET_KEY = "1P7SOGsmb2KwBX50MU1Y1iVCYtjTiU7F7jLqgp6Bl8Bo"  # <-- ID file Sheet của bạn
WORKSHEET_NAME = "D25A"  # <-- tên sheet con trong file (ví dụ: D25A)

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
        raise RuntimeError(
            f"❌ private_key lỗi base64: {e}\nService Account: {svc}"
        )

    # 4. Ghép lại PEM chuẩn
    pk_clean = header + "\n" + "\n".join(body[i:i+64] for i in range(0, len(body), 64)) + "\n" + footer + "\n"
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


# ===================== GIAO DIỆN STREAMLIT =====================
st.set_page_config(page_title="QR Lecturer", layout="wide")
qp = get_query_params()

# ===================== MÀN HÌNH SINH VIÊN =====================
if qp.get("sv") == "1":
    buoi_sv = qp.get("buoi", "Buổi 1")
    st.title("🎓 Điểm danh sinh viên")
    st.info(f"Bạn đang điểm danh cho **{buoi_sv}**")

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

tab_gv, tab_search, tab_stats = st.tabs(["👨‍🏫 Giảng viên (QR động)", "🔎 Tìm kiếm", "📊 Thống kê"])

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
    go = st.button("Tạo mã QR", use_container_width=True, type="primary")

    if go:
    container = st.empty()
    timer = st.empty()
    try:
        while True:
            now = int(time.time())
            slot = now // 30
            token = f"{slot}"
            base_url = st.secrets["google_service_account"].get(
                "app_base_url", "https://qrlecturer.streamlit.app"
            )
            qr_data = f"{base_url}/?sv=1&buoi={urllib.parse.quote(buoi)}&t={token}"

            # Tạo ảnh QR
            qr = qrcode.make(qr_data)
            buf = io.BytesIO(); qr.save(buf, format="PNG"); buf.seek(0)
            img = Image.open(buf)

            # Giao diện gọn (không spam link), có nút và tùy chọn xem chi tiết
            with container.container():
                st.image(img, caption="📱 Quét mã để điểm danh", width=260)
                cols = st.columns([1,1,2])
                with cols[0]:
                    st.download_button("📎 Tải link", qr_data.encode("utf-8"),
                                       file_name="qr_link.txt", use_container_width=True)
                with cols[1]:
                    st.link_button("🌐 Mở link", qr_data, use_container_width=True)
                with cols[2]:
                    if show_link:
                        # Hiển thị gọn + có thể copy
                        st.text_input("URL hiện tại", value=qr_data, label_visibility="visible")

            remain = 30 - (now % 30)
            timer.markdown(f"⏳ QR đổi sau: **{remain} giây**  •  Buổi: **{buoi}**")

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

        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("✅ Có mặt", present)
        with c2:
            st.metric("❌ Vắng", absent)
        with c3:
            total = present + absent
            st.metric("📈 Tỷ lệ có mặt", f"{(present/total*100):.1f}%" if total else "-")

        table = []
        for g, v in sorted(by_group.items()):
            total_g = v["present"] + v["absent"]
            rate_g = f"{(v['present']/total_g*100):.1f}%" if total_g else "-"
            table.append({"Tổ": g, "Có mặt": v["present"], "Vắng": v["absent"], "Tỷ lệ có mặt": rate_g})
        st.dataframe(table, use_container_width=True)
    except Exception as e:
        st.error(f"❌ Lỗi khi lấy thống kê: {e}")

