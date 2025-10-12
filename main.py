import gspread
from google.oauth2.service_account import Credentials  # thay cho oauth2client đã deprecated
import streamlit as st
import json
import os

SHEET_KEY = os.getenv("GSHEET_KEY", "1sWG3jE8lDezfmGcEQgdRCRSBXxNjj9Xz")
WORKSHEET_NAME = os.getenv("GSHEET_TAB", "D25A")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

@st.cache_resource
def _get_gspread_client_safe():
    # 1) Lấy credentials từ st.secrets hoặc env
    raw = None
    if "GOOGLE_CREDENTIALS" in st.secrets:
        raw = st.secrets["GOOGLE_CREDENTIALS"]
    elif os.getenv("GOOGLE_CREDENTIALS"):
        raw = os.environ["GOOGLE_CREDENTIALS"]
    else:
        raise RuntimeError("Thiếu GOOGLE_CREDENTIALS (chưa cấu hình trong Secrets).")

    if isinstance(raw, dict):
        cred_dict = raw
    else:
        cred_dict = json.loads(raw)

    # 2) Tạo Credentials
    creds = Credentials.from_service_account_info(cred_dict, scopes=SCOPES)

    # 3) Trả client
    return gspread.authorize(creds), cred_dict.get("client_email", "")

def get_sheet():
    try:
        client, svc_email = _get_gspread_client_safe()
        ss = client.open_by_key(SHEET_KEY)
        try:
            return ss.worksheet(WORKSHEET_NAME)
        except gspread.exceptions.WorksheetNotFound:
            raise RuntimeError(
                f"Không tìm thấy worksheet '{WORKSHEET_NAME}'. Hãy kiểm tra tên tab trong file."
            )
    except gspread.exceptions.APIError as e:
        # Gợi ý cụ thể những việc cần làm
        raise RuntimeError(
            "Không truy cập được Google Sheet. Kiểm tra các điểm sau:\n"
            "• Hãy mở file Google Sheet và share quyền (Editor hoặc Viewer) cho service account email.\n"
            "• Bật Google Sheets API và Google Drive API trong Google Cloud Console của project.\n"
            "• Đảm bảo SHEET_KEY đúng.\n"
            f"Service Account: {(_get_gspread_client_safe()[1] if 'svc_email' not in locals() else svc_email)}\n"
            f"Lỗi gốc: {e}"
        )
    except Exception as e:
        raise RuntimeError(f"Lỗi cấu hình/credentials: {e}")
