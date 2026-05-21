"""
Label Studio data source.
Fetches ALL tasks for the project using label-studio-sdk.
Results are cached in-memory with a TTL.
"""

from __future__ import annotations

import time
import logging
from datetime import datetime, timezone
from typing import Any

from label_studio_sdk import LabelStudio

from config import (
    LS_BASE_URL,
    LS_API_KEY,
    LS_PROJECT_ID,
    LS_USER_MAP,
    LS_VIEW_IDS,
    CACHE_TTL_SECONDS,
)

logger = logging.getLogger(__name__)

# ── Singleton SDK client ───────────────────────────────────────────────────────
_client: LabelStudio | None = None

def _get_client() -> LabelStudio:
    global _client
    if _client is None:
        _client = LabelStudio(base_url=LS_BASE_URL, api_key=LS_API_KEY)
    return _client


# ── Simple TTL cache ───────────────────────────────────────────────────────────
_cache: dict[str, Any] = {}
_cache_ts: float = 0.0


def _is_cache_valid() -> bool:
    return bool(_cache) and (time.monotonic() - _cache_ts) < CACHE_TTL_SECONDS


def invalidate_cache() -> None:
    global _cache, _cache_ts
    _cache = {}
    _cache_ts = 0.0


# ── Helpers ────────────────────────────────────────────────────────────────────

def _safe_get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _task_to_dict(task: Any) -> dict:
    """Normalize a raw LS task (SDK object or dict) to a plain dict."""
    if isinstance(task, dict):
        raw = task
    else:
        # SDK objects expose __dict__ or model attributes
        try:
            raw = task.model_dump() if hasattr(task, "model_dump") else task.__dict__
        except Exception:
            raw = {}

    annotations = raw.get("annotations") or []
    drafts       = raw.get("drafts") or []
    is_labeled   = bool(raw.get("is_labeled", False))
    draft_exists = bool(drafts)
    completed_at = _parse_dt(raw.get("completed_at"))

    # Annotator from completed_by of first non-cancelled annotation
    annotator_id   = None
    annotator_name = ""
    annotation_completed_at: datetime | None = None

    for ann in annotations:
        if isinstance(ann, dict):
            cancelled = ann.get("was_cancelled", False)
            cb        = ann.get("completed_by") or {}
        else:
            cancelled = getattr(ann, "was_cancelled", False)
            cb_obj    = getattr(ann, "completed_by", None)
            cb        = cb_obj if isinstance(cb_obj, dict) else (cb_obj.__dict__ if cb_obj else {})

        if not cancelled:
            uid = cb.get("id") if isinstance(cb, dict) else None
            if uid is not None:
                annotator_id   = uid
                annotator_name = LS_USER_MAP.get(uid, cb.get("first_name", str(uid)))
                ca_raw = ann.get("completed_at") if isinstance(ann, dict) else getattr(ann, "completed_at", None)
                annotation_completed_at = _parse_dt(ca_raw)
            break

    # Fallback: updated_by list
    if not annotator_name:
        updated_by = raw.get("updated_by") or []
        if updated_by:
            first = updated_by[0]
            uid = (first.get("user_id") if isinstance(first, dict) else getattr(first, "user_id", None))
            if uid is not None:
                annotator_id   = uid
                annotator_name = LS_USER_MAP.get(uid, str(uid))

    return {
        "id":                     str(raw.get("id", "")),
        "inner_id":               raw.get("inner_id"),
        "is_labeled":             is_labeled,
        "draft_exists":           draft_exists,
        "completed_at":           completed_at,
        "annotation_completed_at": annotation_completed_at or completed_at,
        "updated_at":             _parse_dt(raw.get("updated_at")),
        "created_at":             _parse_dt(raw.get("created_at")),
        "annotator_id":           annotator_id,
        "annotator_name":         annotator_name,
        "total_annotations":      int(raw.get("total_annotations") or 0),
        "storage_filename":       raw.get("storage_filename", ""),
        "data":                   raw.get("data") or {},
    }


# ── Public API ─────────────────────────────────────────────────────────────────

def fetch_all_tasks_sync() -> tuple[list[dict], dict[str, list[str]]]:
    """
    Blocking fetch of all LS tasks for the configured project, plus allotted views.
    Safe to call via asyncio.to_thread().
    Uses in-memory TTL cache.
    Returns (tasks, {annotator_name: [task_id, ...]})
    """
    global _cache, _cache_ts

    if _is_cache_valid():
        logger.debug("LS cache hit (%d tasks)", len(_cache["tasks"]))
        return _cache["tasks"], _cache.get("views_task_ids", {})

    logger.info("Fetching all tasks from Label Studio…")
    client = _get_client()

    try:
        raw_tasks = list(
            client.tasks.list(project=LS_PROJECT_ID, fields="all")
        )
        
        # Fetch tasks for each specific allotted view
        views_task_ids: dict[str, list[str]] = {}
        for view_id, name in LS_VIEW_IDS.items():
            try:
                view_tasks = list(client.tasks.list(project=LS_PROJECT_ID, view=view_id, fields="id"))
                views_task_ids[name] = [str(t.id) if not isinstance(t, dict) else str(t.get("id")) for t in view_tasks]
            except Exception as e:
                logger.warning("Failed to fetch tasks for view %s (%s): %s", view_id, name, e)
                views_task_ids[name] = []
                
    except Exception as exc:
        logger.error("LS fetch failed: %s", exc)
        if _cache:
            logger.warning("Returning stale LS cache")
            return _cache["tasks"], _cache.get("views_task_ids", {})
        return [], {}

    tasks = [_task_to_dict(t) for t in raw_tasks]
    _cache = {"tasks": tasks, "views_task_ids": views_task_ids}
    _cache_ts = time.monotonic()
    logger.info("LS fetch complete: %d tasks", len(tasks))
    return tasks, views_task_ids
