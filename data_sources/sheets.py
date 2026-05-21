"""
Google Sheets data source.
Reads the "kaithi annotations tracker" sheet via gspread service-account auth.
Returns a list of dicts, one per data row (header rows skipped).
"""

from __future__ import annotations

import time
import logging
from typing import Any
import json
import os

import gspread
from google.oauth2.service_account import Credentials

from config import (
    SERVICE_ACCOUNT_FILE,
    GOOGLE_SCOPES,
    SPREADSHEET_NAME,
    WORKSHEET_NAME,
    SHEET_HEADER_ROWS,
    COL_TASK_ID,
    COL_FILE_URL,
    COL_LS_URL,
    COL_IS_LABELED,
    COL_ANNOTATED_BY,
    COL_ANNOTATION_DATE,
    COL_ANNOTATION_APPROVED,
    COL_ANNOTATION_APPROVED_BY,
    COL_EXCEL_READY,
    COL_EXCEL_ASSIGNED_TO,
    COL_EXCEL_SUBMITTED,
    COL_EXCEL_APPROVED,
    COL_EXCEL_APPROVED_BY,
    COL_EXCEL_LINK,
    CACHE_TTL_SECONDS,
)

logger = logging.getLogger(__name__)

# ── Singleton gspread client ───────────────────────────────────────────────────
_gc: gspread.Client | None = None


def _get_gc() -> gspread.Client:
    global _gc
    if _gc is None:
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if creds_json:
            creds_dict = json.loads(creds_json)
            creds = Credentials.from_service_account_info(creds_dict, scopes=GOOGLE_SCOPES)
        else:
            creds = Credentials.from_service_account_file(
                str(SERVICE_ACCOUNT_FILE), scopes=GOOGLE_SCOPES
            )
        _gc = gspread.authorize(creds)
    return _gc


# ── TTL cache ──────────────────────────────────────────────────────────────────
_cache: list[dict] | None = None
_cache_ts: float = 0.0


def _is_cache_valid() -> bool:
    return _cache is not None and (time.monotonic() - _cache_ts) < CACHE_TTL_SECONDS


def invalidate_cache() -> None:
    global _cache, _cache_ts
    _cache = None
    _cache_ts = 0.0


# ── Helpers ────────────────────────────────────────────────────────────────────

def _safe(row: list, idx: int) -> str:
    try:
        return str(row[idx]).strip() if idx < len(row) else ""
    except Exception:
        return ""


def _is_true(val: str) -> bool:
    return val.upper() in {"TRUE", "YES", "1"}


def _row_to_dict(row: list) -> dict:
    task_id = _safe(row, COL_TASK_ID)
    if not task_id:
        return {}

    return {
        "task_id":               task_id,
        "file_url":              _safe(row, COL_FILE_URL),
        "ls_url":                _safe(row, COL_LS_URL),
        "is_labeled":            _is_true(_safe(row, COL_IS_LABELED)),
        "annotated_by":          _safe(row, COL_ANNOTATED_BY),
        "annotation_date":       _safe(row, COL_ANNOTATION_DATE),
        "annotation_approved":   _is_true(_safe(row, COL_ANNOTATION_APPROVED)),
        "annotation_approved_by":_safe(row, COL_ANNOTATION_APPROVED_BY),
        "excel_ready":           _is_true(_safe(row, COL_EXCEL_READY)),
        "excel_assigned_to":     _safe(row, COL_EXCEL_ASSIGNED_TO),
        "excel_submitted":       _is_true(_safe(row, COL_EXCEL_SUBMITTED)),
        "excel_approved":        _is_true(_safe(row, COL_EXCEL_APPROVED)),
        "excel_approved_by":     _safe(row, COL_EXCEL_APPROVED_BY),
        "excel_link":            _safe(row, COL_EXCEL_LINK),
    }


# ── Public API ─────────────────────────────────────────────────────────────────

def fetch_sheet_rows_sync() -> list[dict]:
    """
    Blocking read of all data rows from the tracker sheet.
    Safe to call via asyncio.to_thread().
    Uses in-memory TTL cache.
    """
    global _cache, _cache_ts

    if _is_cache_valid():
        logger.debug("Sheet cache hit (%d rows)", len(_cache))
        return _cache

    logger.info("Fetching Google Sheet…")
    try:
        gc = _get_gc()
        ws = gc.open(SPREADSHEET_NAME).worksheet(WORKSHEET_NAME)
        all_values: list[list] = ws.get_all_values()
    except Exception as exc:
        logger.error("Sheet fetch failed: %s", exc)
        if _cache is not None:
            logger.warning("Returning stale Sheet cache")
            return _cache
        return []

    rows = []
    for raw_row in all_values[SHEET_HEADER_ROWS:]:
        d = _row_to_dict(raw_row)
        if d:
            rows.append(d)

    _cache = rows
    _cache_ts = time.monotonic()
    logger.info("Sheet fetch complete: %d rows", len(rows))
    return rows
