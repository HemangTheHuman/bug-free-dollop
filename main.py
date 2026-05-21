"""
FastAPI application entry point.
Serves:
  GET /api/dashboard?view=today|week|all  → JSON dashboard payload
  GET /                                   → annotation_dashboard.html (static)
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from data_sources import label_studio as ls_source
from data_sources import sheets as sheets_source
from data_sources import drive as drive_source
from aggregator import compute_dashboard
from config import CACHE_DIR
import json

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Annotation Pipeline Dashboard API", version="1.0.0")

# Allow the HTML to call the API from the same origin or any dev port
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# Root of the project (parent of backend/)
PROJECT_ROOT = Path(__file__).parent.parent

# Ensure cache dir exists
CACHE_DIR.mkdir(parents=True, exist_ok=True)


# ─── Routes ──────────────────────────────────────────────────────────────────

@app.get("/api/dashboard")
async def get_dashboard(
    view: str = Query(default="all", pattern="^(today|week|all)$"),
):
    """
    Returns the full dashboard data payload.
    All three sources are fetched concurrently in thread-pool workers
    (the underlying SDK/gspread/Drive calls are all synchronous).
    """
    try:
        ls_res, sheet_rows, drive_data = await asyncio.gather(
            asyncio.to_thread(ls_source.fetch_all_tasks_sync),
            asyncio.to_thread(sheets_source.fetch_sheet_rows_sync),
            asyncio.to_thread(drive_source.fetch_drive_data_sync),
        )
        ls_tasks, ls_views_tasks = ls_res
    except Exception as exc:
        logger.exception("Data fetch failed")
        raise HTTPException(status_code=502, detail=f"Data source error: {exc}") from exc

    payload = compute_dashboard(ls_tasks, ls_views_tasks, sheet_rows, drive_data, view=view)

    # Persist the generated payload to disk so it can be served instantly on reload
    cache_file = CACHE_DIR / f"dashboard_cache_{view}.json"
    try:
        cache_file.write_text(json.dumps(payload))
    except Exception as exc:
        logger.warning(f"Failed to write dashboard cache file: {exc}")

    return JSONResponse(content=payload)


@app.get("/api/dashboard/cached")
async def get_dashboard_cached(
    view: str = Query(default="all", pattern="^(today|week|all)$"),
):
    """
    Returns the physically cached dashboard data payload instantly (if it exists).
    This allows the frontend to render statically while live data fetches in background.
    """
    cache_file = CACHE_DIR / f"dashboard_cache_{view}.json"
    if cache_file.exists():
        return FileResponse(str(cache_file), media_type="application/json")
    return JSONResponse(content={})


@app.post("/api/refresh")
async def refresh_cache():
    """Force-invalidate all caches so next /api/dashboard call fetches fresh data."""
    ls_source.invalidate_cache()
    sheets_source.invalidate_cache()
    drive_source.invalidate_cache()
    return {"status": "caches cleared"}


@app.get("/api/health")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=FileResponse)
async def serve_dashboard():
    """Serve the annotation dashboard HTML."""
    html_path = PROJECT_ROOT / "annotation_dashboard.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="Dashboard HTML not found")
    return FileResponse(str(html_path), media_type="text/html")
