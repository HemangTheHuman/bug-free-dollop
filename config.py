"""
Central configuration for the annotation dashboard 
All values sourced from the existing sync.py / project setup.
"""

from pathlib import Path
import os
from dotenv import load_dotenv

load_dotenv()

# ── Label Studio ──────────────────────────────────────────────────────────────
LS_BASE_URL = os.environ.get("LS_BASE_URL", "https://multilipi-label-studio.centralindia.cloudapp.azure.com")
LS_API_KEY  = os.environ.get("LS_API_KEY", "")
LS_PROJECT_ID = 1

# Views for determining "Allotted" tasks (View ID -> Annotator Name)
LS_VIEW_IDS = {
    10: "Saqib",
    1:  "Saumya",
    9:  "Ali",
    11: "Pritam",
}

# ── Google / Service Account ──────────────────────────────────────────────────
SERVICE_ACCOUNT_FILE = Path(__file__).parent.parent / "google.json"

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

# ── Google Sheet ──────────────────────────────────────────────────────────────
SPREADSHEET_NAME = "kaithi annotations tracker"
WORKSHEET_NAME   = "Sheet1"

# Column indices (0-based) — must match actual sheet layout
# A  Task ID
# B  File URL
# C  Label Studio URL
# D  Is Labeled / Annotated
# E  Updated By / Annotated by
# F  Updated Date / Annotation On
# G  Annotation Review / Annotation Approved
# H  Annotation Approved By      ← manually maintained in sheet
# I  Excel Review / Excel Ready
# J  Excel Assigned To / Excel Allocated To
# K  Excel Submitted / Completed
# L  Excel Approved
# M  Excel Approved By
# N  Excel Link / Completed Excel Link

COL_TASK_ID               = 0   # A
COL_FILE_URL              = 1   # B
COL_LS_URL                = 2   # C
COL_IS_LABELED            = 3   # D
COL_ANNOTATED_BY          = 4   # E
COL_ANNOTATION_DATE       = 5   # F
COL_ANNOTATION_APPROVED   = 6   # G
COL_ANNOTATION_APPROVED_BY= 7   # H
COL_EXCEL_READY           = 8   # I
COL_EXCEL_ASSIGNED_TO     = 9   # J
COL_EXCEL_SUBMITTED       = 10  # K
COL_EXCEL_APPROVED        = 11  # L
COL_EXCEL_APPROVED_BY     = 12  # M
COL_EXCEL_LINK            = 13  # N

SHEET_HEADER_ROWS = 1   # Number of header rows to skip

# ── Google Drive Folders ──────────────────────────────────────────────────────
# Allocation folders: { display_name -> folder_id }
DRIVE_ALLOCATION_FOLDERS: dict[str, str] = {
    "Gajendra": "1apXbxdZ44-oELlI3acbv38sQqKAfKSo9",
    "Ritik":    "1xk3iQZGnye_phBu-kv-bwrvd-q2IOYfv",
}
# Completed / submitted excels folder
DRIVE_SUBMITTED_FOLDER_ID = "1Vefn6kW6JDGpI8_DsNqqVtoKkwJB2rDu"
# Approved excels folder (same as submitted for now — update if separate)
DRIVE_APPROVED_FOLDER_ID  = "1Vefn6kW6JDGpI8_DsNqqVtoKkwJB2rDu"

# ── User map: LS user_id → display name ──────────────────────────────────────
LS_USER_MAP: dict[int, str] = {
    1: "Hemang",
    2: "Saumya",
    4: "Pritam",
    5: "Saqib",
    7: "Ali",
    8: "Saqib",
}

# ── Cache ─────────────────────────────────────────────────────────────────────
CACHE_TTL_SECONDS = 3600   # 1 hour
CACHE_DIR = Path(__file__).parent.parent / "cache"

# ── Throughput history window ─────────────────────────────────────────────────
THROUGHPUT_DAYS = 14
