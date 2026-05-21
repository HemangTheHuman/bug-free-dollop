"""
Google Drive data source.
Lists files in the 3 configured folders (Allocated per-person, Submitted, Approved).
Returns structured metadata including per-file modifiedTime for throughput charting.
"""

from __future__ import annotations

import re
import time
import logging
from datetime import datetime, timezone
from typing import Any

import json
import os
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

from config import (
    SERVICE_ACCOUNT_FILE,
    GOOGLE_SCOPES,
    DRIVE_ALLOCATION_FOLDERS,
    DRIVE_SUBMITTED_FOLDER_ID,
    DRIVE_APPROVED_FOLDER_ID,
    CACHE_TTL_SECONDS,
)

logger = logging.getLogger(__name__)

# ── Singleton Drive service ────────────────────────────────────────────────────
_drive_service: Any = None


def _get_drive():
    global _drive_service
    if _drive_service is None:
        creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
        if creds_json:
            creds_dict = json.loads(creds_json)
            creds = Credentials.from_service_account_info(creds_dict, scopes=GOOGLE_SCOPES)
        else:
            creds = Credentials.from_service_account_file(
                str(SERVICE_ACCOUNT_FILE), scopes=GOOGLE_SCOPES
            )
        _drive_service = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _drive_service


# ── TTL cache ──────────────────────────────────────────────────────────────────
_cache: dict | None = None
_cache_ts: float = 0.0


def _is_cache_valid() -> bool:
    return _cache is not None and (time.monotonic() - _cache_ts) < CACHE_TTL_SECONDS


def invalidate_cache() -> None:
    global _cache, _cache_ts
    _cache = None
    _cache_ts = 0.0


# ── Helpers ────────────────────────────────────────────────────────────────────
_TASK_RE = re.compile(r"^task[\s_\-]*(\d+)(?:[\s_\-].*)?(?:\.xlsx)?$", re.IGNORECASE)


def _extract_task_id(filename: str) -> str | None:
    if not filename:
        return None
    clean = filename.strip().replace("\u00a0", " ")
    m = _TASK_RE.match(clean)
    return m.group(1) if m else None


def _parse_drive_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _list_folder(drive, folder_id: str) -> list[dict]:
    """Paginate through all files in a Drive folder."""
    files: list[dict] = []
    page_token = None
    while True:
        resp = drive.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            spaces="drive",
            fields="nextPageToken, files(id, name, mimeType, modifiedTime, webViewLink)",
            pageToken=page_token,
            pageSize=1000,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()
        files.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return files


def _process_folder(drive, folder_id: str) -> dict:
    """
    Returns:
        {
            "files": [{ task_id, filename, modified_dt, link }],
            "file_count": int,
            "task_count": int,
        }
    """
    raw = _list_folder(drive, folder_id)
    seen_tasks: set[str] = set()
    processed: list[dict] = []

    for f in raw:
        filename = f.get("name", "")
        task_id  = _extract_task_id(filename)
        modified = _parse_drive_dt(f.get("modifiedTime"))
        link     = f.get("webViewLink") or f"https://drive.google.com/open?id={f['id']}"

        entry = {
            "task_id":     task_id,
            "filename":    filename,
            "modified_dt": modified,
            "link":        link,
        }
        processed.append(entry)
        if task_id:
            seen_tasks.add(task_id)

    return {
        "files":      processed,
        "file_count": len(raw),
        "task_count": len(seen_tasks),
    }


# ── Public API ─────────────────────────────────────────────────────────────────

def fetch_drive_data_sync() -> dict:
    """
    Returns:
    {
        "allocated": {
            "<name>": { "files": [...], "file_count": N, "task_count": N }
        },
        "submitted": { "files": [...], "file_count": N, "task_count": N },
        "approved":  { "files": [...], "file_count": N, "task_count": N },
        # Convenience flat lookups built from submitted/approved
        "submitted_by_task": { task_id: { modified_dt, link } },
        "approved_by_task":  { task_id: { modified_dt, link } },
        "allocated_to_task": { task_id: folder_display_name },
    }
    """
    global _cache, _cache_ts

    if _is_cache_valid():
        logger.debug("Drive cache hit")
        return _cache

    logger.info("Fetching Google Drive folders…")
    try:
        drive = _get_drive()

        # Allocation folders (one per assignee)
        allocated: dict[str, dict] = {}
        allocated_to_task: dict[str, str] = {}
        for display_name, folder_id in DRIVE_ALLOCATION_FOLDERS.items():
            info = _process_folder(drive, folder_id)
            allocated[display_name] = info
            for f in info["files"]:
                if f["task_id"] and f["task_id"] not in allocated_to_task:
                    allocated_to_task[f["task_id"]] = display_name

        # Submitted / completed folder
        submitted_info = _process_folder(drive, DRIVE_SUBMITTED_FOLDER_ID)
        submitted_by_task: dict[str, dict] = {}
        for f in submitted_info["files"]:
            if f["task_id"] and f["task_id"] not in submitted_by_task:
                submitted_by_task[f["task_id"]] = {
                    "modified_dt": f["modified_dt"],
                    "link": f["link"],
                }

        # Approved folder (may be same as submitted)
        if DRIVE_APPROVED_FOLDER_ID == DRIVE_SUBMITTED_FOLDER_ID:
            approved_info    = submitted_info
            approved_by_task = submitted_by_task
        else:
            approved_info = _process_folder(drive, DRIVE_APPROVED_FOLDER_ID)
            approved_by_task: dict[str, dict] = {}
            for f in approved_info["files"]:
                if f["task_id"] and f["task_id"] not in approved_by_task:
                    approved_by_task[f["task_id"]] = {
                        "modified_dt": f["modified_dt"],
                        "link": f["link"],
                    }

        result = {
            "allocated":         allocated,
            "submitted":         submitted_info,
            "approved":          approved_info,
            "submitted_by_task": submitted_by_task,
            "approved_by_task":  approved_by_task,
            "allocated_to_task": allocated_to_task,
        }

        _cache    = result
        _cache_ts = time.monotonic()
        logger.info(
            "Drive fetch complete: %d allocated, %d submitted, %d approved",
            sum(v["task_count"] for v in allocated.values()),
            submitted_info["task_count"],
            approved_info["task_count"],
        )
        return result

    except Exception as exc:
        logger.error("Drive fetch failed: %s", exc)
        if _cache is not None:
            logger.warning("Returning stale Drive cache")
            return _cache
        return {
            "allocated": {}, "submitted": {}, "approved": {},
            "submitted_by_task": {}, "approved_by_task": {}, "allocated_to_task": {},
        }
