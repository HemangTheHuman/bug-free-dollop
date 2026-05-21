"""
Aggregator: joins Label Studio tasks, Sheet rows, and Drive data,
then computes every KPI, pipeline stage count, annotator/reviewer cards,
and throughput series for the dashboard.
"""

from __future__ import annotations

import math
import logging
from collections import defaultdict
from datetime import datetime, timezone, timedelta, date

from config import THROUGHPUT_DAYS

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _today_utc() -> date:
    return _now_utc().date()


def _within(dt: datetime | None, days: int) -> bool:
    """True if dt is within the last `days` days (inclusive of today)."""
    if dt is None:
        return False
    cutoff = _now_utc() - timedelta(days=days)
    return dt >= cutoff


def _on_day(dt: datetime | None, target: date) -> bool:
    if dt is None:
        return False
    return dt.date() == target


def _working_days_since(dt: datetime | None) -> float:
    """Rough working-days count from dt to now (Mon–Fri, ≥ 1)."""
    if dt is None:
        return 1.0
    delta = (_today_utc() - dt.date()).days
    weeks, rem = divmod(max(delta, 0), 7)
    wd = weeks * 5 + min(rem, 5)
    return max(float(wd), 1.0)


def _initials(name: str) -> str:
    parts = name.strip().split()
    if not parts:
        return "??"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def _pct(numerator: int, denominator: int) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator / denominator * 100, 1)


def _est_completion_days(remaining: int, daily_rate: float) -> str:
    if daily_rate <= 0 or remaining <= 0:
        return "N/A"
    days = math.ceil(remaining / daily_rate)
    if days >= 365:
        return f"~{days // 30}mo"
    if days >= 14:
        return f"~{days // 7}wk"
    return f"~{days}d"


# ─────────────────────────────────────────────────────────────────────────────
# View filter helpers
# ─────────────────────────────────────────────────────────────────────────────

def _in_view(dt: datetime | None, view: str) -> bool:
    """True if dt falls within the requested view window."""
    if view == "all" or dt is None:
        return view == "all" or True  # for 'all' we never filter
    if view == "today":
        return _on_day(dt, _today_utc())
    if view == "week":
        return _within(dt, 7)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Main aggregation
# ─────────────────────────────────────────────────────────────────────────────

def compute_dashboard(
    ls_tasks: list[dict],
    ls_views_tasks: dict[str, list[str]],
    sheet_rows: list[dict],
    drive_data: dict,
    view: str = "all",
) -> dict:
    """
    Parameters
    ----------
    ls_tasks   : from label_studio.fetch_all_tasks()
    sheet_rows : from sheets.fetch_sheet_rows()
    drive_data : from drive.fetch_drive_data()
    view       : 'today' | 'week' | 'all'

    Returns
    -------
    Full dashboard payload dict.
    """

    now = _now_utc()

    # ── Build lookup maps ─────────────────────────────────────────────────────
    # Sheet rows keyed by task_id (string)
    sheet_by_id: dict[str, dict] = {r["task_id"]: r for r in sheet_rows if r.get("task_id")}

    # LS tasks keyed by id
    ls_by_id: dict[str, dict] = {t["id"]: t for t in ls_tasks if t.get("id")}

    # Drive lookups
    submitted_by_task: dict[str, dict] = drive_data.get("submitted_by_task", {})
    approved_by_task:  dict[str, dict] = drive_data.get("approved_by_task",  {})
    allocated_to_task: dict[str, str]  = drive_data.get("allocated_to_task", {})

    # ── Per-task classification ────────────────────────────────────────────────
    # We iterate all task IDs from BOTH LS and Sheet (union)
    all_task_ids: set[str] = set(ls_by_id.keys()) | set(sheet_by_id.keys())

    stage_counts = {
        "pending":              0,
        "in_annotation":        0,
        "pending_review":       0,
        "rejected":             0,
        "bbox_approved":        0,
        "excel_pending_review": 0,
        "excel_rejected":       0,
        "excel_complete":       0,  # now means excel approved/submitted without lock? Wait, let's keep "excel_approved" as a stage, but in the dict we can use excel_complete or just change it.
        "locked":               0,
    }
    # To keep payload keys somewhat backward compatible where possible, but add the new ones:
    # Actually, let's just use exact keys:
    stage_counts = {
        "pending":              0,
        "in_annotation":        0,
        "pending_review":       0,  # bbox pending
        "rejected":             0,  # bbox rejected
        "bbox_approved":        0,
        "excel_pending":        0,
        "excel_in_labeling":    0,
        "excel_pending_review": 0,
        "excel_rejected":       0,
        "excel_approved":       0,
    }

    # For view-filtered KPIs
    vf_annotated  = 0  # bbox approved in view window
    vf_labeled    = 0  # excel submitted in view window
    vf_locked     = 0  # excel approved in view window

    # ── Annotator stats ────────────────────────────────────────────────────────
    # ann_stats[name] = { total_done, approved, rejected, pending_review, last24h, first_dt }
    ann_stats: dict[str, dict] = defaultdict(lambda: {
        "total_done": 0, "approved": 0, "rejected": 0, "pending_review": 0, "last24h": 0,
        "first_dt": None,
    })

    # ── Excel labeler stats ────────────────────────────────────────────────────
    # exc_stats[name] = { submitted, approved, pending_review, rejected, last24h, first_dt }
    exc_stats: dict[str, dict] = defaultdict(lambda: {
        "submitted": 0, "approved": 0, "rejected": 0, "pending_review": 0,
        "last24h": 0, "first_dt": None,
    })

    # ── Reviewer stats ─────────────────────────────────────────────────────────
    bbox_rev_stats:  dict[str, dict] = defaultdict(lambda: {"total": 0, "approved": 0, "rejected": 0, "first_dt": None})
    excel_rev_stats: dict[str, dict] = defaultdict(lambda: {"total": 0, "approved": 0, "rejected": 0, "first_dt": None})

    task_labeled_status: dict[str, bool] = {}

    # ── Throughput buckets (last N days) ───────────────────────────────────────
    day_labels: list[str] = []
    bbox_by_day:  dict[str, int] = {}
    excel_by_day: dict[str, int] = {}

    for i in range(THROUGHPUT_DAYS - 1, -1, -1):
        d = (now - timedelta(days=i)).date()
        label = d.strftime("%b %d").lstrip("0").replace(" 0", " ")
        day_labels.append(label)
        bbox_by_day[label]   = 0
        excel_by_day[label]  = 0

    def _day_label(dt: datetime | None) -> str | None:
        if dt is None:
            return None
        d = dt.date()
        cutoff_date = (now - timedelta(days=THROUGHPUT_DAYS - 1)).date()
        if d < cutoff_date:
            return None
        label = d.strftime("%b %d").lstrip("0").replace(" 0", " ")
        return label if label in bbox_by_day else None

    # ── Main iteration ─────────────────────────────────────────────────────────
    for task_id in all_task_ids:
        ls  = ls_by_id.get(task_id)
        sh  = sheet_by_id.get(task_id)

        is_labeled:          bool = (ls["is_labeled"] if ls else False) or (sh["is_labeled"] if sh else False)
        draft_exists:        bool = ls["draft_exists"] if ls else False
        annotation_approved: bool = sh["annotation_approved"] if sh else False
        excel_ready:         bool = sh["excel_ready"] if sh else False
        excel_assigned_to:   str  = sh["excel_assigned_to"] if sh else ""
        excel_submitted:     bool = sh["excel_submitted"] if sh else False
        excel_approved:      bool = sh["excel_approved"] if sh else False

        task_labeled_status[str(task_id)] = is_labeled

        # annotation timestamp (from LS)
        ann_dt: datetime | None = ls["annotation_completed_at"] if ls else None

        # excel submitted timestamp (from Drive submitted folder)
        sub_entry = submitted_by_task.get(task_id)
        sub_dt: datetime | None = sub_entry["modified_dt"] if sub_entry else None

        # excel approved timestamp (Drive approved folder)
        appr_entry = approved_by_task.get(task_id)
        appr_dt: datetime | None = appr_entry["modified_dt"] if appr_entry else None

        # ── Pipeline stage (Independent evaluation) ────────────────────────────
        # Bbox stages
        if annotation_approved:
            stage_counts["bbox_approved"] += 1
        elif is_labeled and not annotation_approved and sh and sh.get("annotation_approved_by"):
            stage_counts["rejected"] += 1
        elif is_labeled and not annotation_approved and not (sh and sh.get("annotation_approved_by")):
            stage_counts["pending_review"] += 1
        elif draft_exists or any(task_id in t_list for t_list in ls_views_tasks.values()):
            stage_counts["in_annotation"] += 1
        elif not is_labeled:
            stage_counts["pending"] += 1

        # Excel stages (Independent of bbox)
        if not excel_ready:
            stage_counts["excel_pending"] += 1
        elif excel_assigned_to and not excel_submitted:
            stage_counts["excel_in_labeling"] += 1
        elif excel_submitted:
            if excel_approved:
                stage_counts["excel_approved"] += 1
            else:
                if sh and sh.get("excel_approved_by"):
                    stage_counts["excel_rejected"] += 1
                else:
                    stage_counts["excel_pending_review"] += 1

        # ── View-filtered KPIs ─────────────────────────────────────────────────
        if view == "all":
            if is_labeled:
                vf_annotated += 1
            if excel_submitted:
                vf_labeled += 1
            if excel_approved:
                vf_locked += 1
        else:
            days_back = 1 if view == "today" else 7
            if is_labeled and _within(ann_dt, days_back):
                vf_annotated += 1
            if excel_submitted and _within(sub_dt, days_back):
                vf_labeled += 1
            if excel_approved and _within(appr_dt, days_back):
                vf_locked += 1

        # ── Annotator stats ────────────────────────────────────────────────────
        annotator = (ls["annotator_name"] if ls else "") or (sh["annotated_by"] if sh else "")
        if is_labeled and annotator:
            st = ann_stats[annotator]
            st["total_done"] += 1
            if annotation_approved:
                st["approved"] += 1
            else:
                if sh and sh.get("annotation_approved_by"):
                    st["rejected"] += 1
                else:
                    st["pending_review"] += 1
            if ann_dt and _within(ann_dt, 1):
                st["last24h"] += 1
            if ann_dt and (st["first_dt"] is None or ann_dt < st["first_dt"]):
                st["first_dt"] = ann_dt

        # ── Excel labeler stats ────────────────────────────────────────────────
        excel_assignee = sh["excel_assigned_to"] if sh else ""
        if excel_assignee:
            es = exc_stats[excel_assignee]
            if excel_submitted:
                es["submitted"] += 1
                if excel_approved:
                    es["approved"] += 1
                else:
                    if sh and sh.get("excel_approved_by"):
                        es["rejected"] += 1
                    else:
                        es["pending_review"] += 1
            elif sh and sh.get("excel_ready") and not excel_submitted:
                pass  # allocated but not yet submitted

            if sub_dt and _within(sub_dt, 1):
                es["last24h"] += 1
            if sub_dt and (es["first_dt"] is None or sub_dt < es["first_dt"]):
                es["first_dt"] = sub_dt

        # ── Reviewer stats ─────────────────────────────────────────────────────
        bbox_reviewer = sh["annotation_approved_by"] if sh else ""
        if bbox_reviewer and is_labeled:
            rs = bbox_rev_stats[bbox_reviewer]
            rs["total"] += 1
            if annotation_approved:
                rs["approved"] += 1
            else:
                rs["rejected"] += 1
            if ann_dt and (rs["first_dt"] is None or ann_dt < rs["first_dt"]):
                rs["first_dt"] = ann_dt

        excel_reviewer = sh["excel_approved_by"] if sh else ""
        if excel_reviewer and excel_submitted:
            rs = excel_rev_stats[excel_reviewer]
            rs["total"] += 1
            if excel_approved:
                rs["approved"] += 1
            else:
                rs["rejected"] += 1
            if sub_dt and (rs["first_dt"] is None or sub_dt < rs["first_dt"]):
                rs["first_dt"] = sub_dt

        # ── Throughput ─────────────────────────────────────────────────────────
        if is_labeled:
            lbl = _day_label(ann_dt)
            if lbl:
                bbox_by_day[lbl] += 1

        if excel_submitted:
            lbl = _day_label(sub_dt)
            if lbl:
                excel_by_day[lbl] += 1

    # ── Aggregate KPIs ────────────────────────────────────────────────────────
    total_tasks     = len(all_task_ids)
    
    # Overlap-free logic as requested by user (Bbox Annotated = Approved + Rejected + Pending Review)
    total_annotated = stage_counts["bbox_approved"] + stage_counts["rejected"] + stage_counts["pending_review"]
    total_labeled   = stage_counts["excel_approved"] + stage_counts["excel_rejected"] + stage_counts["excel_pending_review"]
    total_locked    = stage_counts["excel_approved"]

    # Approval rates
    total_reviewed_bbox  = stage_counts["bbox_approved"] + stage_counts["rejected"]
    approved_bbox        = stage_counts["bbox_approved"]
    approval_rate_bbox   = _pct(approved_bbox, total_reviewed_bbox) if total_reviewed_bbox > 0 else 100

    total_reviewed_excel = stage_counts["excel_approved"] + stage_counts["excel_rejected"]
    approval_rate_excel  = _pct(stage_counts["excel_approved"], total_reviewed_excel) if total_reviewed_excel > 0 else 100

    rejection_backlog = stage_counts["rejected"] + stage_counts["excel_rejected"]

    # Average bbox per day (team level, all-time)
    all_ann_dts = [
        ls["annotation_completed_at"]
        for ls in ls_tasks
        if ls.get("annotation_completed_at") and ls.get("is_labeled")
    ]
    if all_ann_dts:
        earliest = min(all_ann_dts)
        team_working_days = _working_days_since(earliest)
        avg_bbox_per_day  = round(approved_bbox / team_working_days, 1)
    else:
        avg_bbox_per_day = 0.0

    est_completion = _est_completion_days(
        total_tasks - total_locked,
        avg_bbox_per_day if avg_bbox_per_day > 0 else 1.0,
    )

    # ── Annotator cards ────────────────────────────────────────────────────────
    annotator_cards = []
    for name, st in sorted(ann_stats.items(), key=lambda x: -x[1]["total_done"]):
        total  = st["total_done"]
        approved = st["approved"]
        rejected = st["rejected"]
        rate     = _pct(approved, total)
        avg_day  = round(total / _working_days_since(st["first_dt"]), 1)

        # allotted = count from their LS view
        assigned_task_ids = ls_views_tasks.get(name, [])
        allotted = len(assigned_task_ids)
        assigned_pending = sum(1 for tid in assigned_task_ids if not task_labeled_status.get(str(tid), False))

        annotator_cards.append({
            "name":             name,
            "initials":         _initials(name),
            "role":             "Bbox annotator",
            "assigned":         allotted,
            "assigned_pending": assigned_pending,
            "total_done":       total,
            "last24h":          st["last24h"],
            "avg_day":          avg_day,
            "approved":         approved,
            "rejected":         rejected,
            "pending_review":   st["pending_review"],
            "rate":             rate,
        })

    # ── Excel labeler cards ────────────────────────────────────────────────────
    excel_labeler_cards = []
    for name, es in sorted(exc_stats.items(), key=lambda x: -x[1]["submitted"]):
        submitted = es["submitted"]
        approved  = es["approved"]
        pending   = es["pending_review"]
        rejected  = es["rejected"]
        rate      = _pct(approved, submitted)
        avg_day   = round(submitted / _working_days_since(es["first_dt"]), 1)
        # allotted = number of files in their Drive Allocation folder
        allotted = 0
        if name in drive_data.get("allocated", {}):
            allotted = drive_data["allocated"][name].get("file_count", 0)

        assigned_pending = allotted

        excel_labeler_cards.append({
            "name":             name,
            "initials":         _initials(name),
            "role":             "Excel labeler",
            "assigned":         allotted,
            "assigned_pending": assigned_pending,
            "submitted":        submitted,
            "last24h":          es["last24h"],
            "avg_day":          avg_day,
            "approved":         approved,
            "pending":          pending,
            "rejected":         max(rejected, 0),
            "rate":             rate,
        })

    # ── Reviewer cards ─────────────────────────────────────────────────────────
    reviewer_cards = []
    all_reviewer_names: set[str] = set(bbox_rev_stats.keys()) | set(excel_rev_stats.keys())
    for name in all_reviewer_names:
        if not name:
            continue
        b = bbox_rev_stats.get(name, {"total": 0, "approved": 0, "rejected": 0, "first_dt": None})
        e = excel_rev_stats.get(name, {"total": 0, "approved": 0, "rejected": 0, "first_dt": None})
        total    = b["total"] + e["total"]
        approved = b["approved"] + e["approved"]
        rejected = b["rejected"] + e["rejected"]
        rate     = _pct(approved, total)

        first_dt = None
        for dt in (b["first_dt"], e["first_dt"]):
            if dt and (first_dt is None or dt < first_dt):
                first_dt = dt
        avg_day = round(total / _working_days_since(first_dt), 1)

        roles = []
        if b["total"] > 0:
            roles.append("Bbox reviewer")
        if e["total"] > 0:
            roles.append("Excel reviewer")
        role_label = " + ".join(roles) if roles else "Reviewer"

        reviewer_cards.append({
            "name":           name,
            "initials":       _initials(name),
            "role":           role_label,
            "total_reviewed": total,
            "approved":       approved,
            "rejected":       rejected,
            "avg_day":        avg_day,
            "rate":           rate,
        })
    reviewer_cards.sort(key=lambda x: -x["total_reviewed"])

    # ── Drive counts ───────────────────────────────────────────────────────────
    allocated_data = drive_data.get("allocated", {})
    drive_counts = {
        "allocated": sum(v.get("task_count", 0) for v in allocated_data.values()),
        "submitted": drive_data.get("submitted", {}).get("task_count", 0),
        "approved":  drive_data.get("approved", {}).get("task_count", 0),
    }

    # ── Annotator bar chart data ────────────────────────────────────────────────
    annotator_chart = {
        "labels":   [c["name"] for c in annotator_cards[:6]],
        "approved": [c["approved"] for c in annotator_cards[:6]],
        "rejected": [c["rejected"] for c in annotator_cards[:6]],
    }
    excel_labeler_chart = {
        "labels":   [c["name"] for c in excel_labeler_cards[:6]],
        "approved": [c["approved"] for c in excel_labeler_cards[:6]],
        "rejected": [c["rejected"] for c in excel_labeler_cards[:6]],
    }

    return {
        "view": view,
        "kpis": {
            "total":             total_tasks,
            "annotated":         vf_annotated,
            "annotated_pct":     _pct(vf_annotated, total_tasks),
            "labeled":           vf_labeled,
            "labeled_pct":       _pct(vf_labeled, total_tasks),
            "locked":            vf_locked,
            "locked_pct":        _pct(vf_locked, total_tasks),
            "approval_bbox":     approval_rate_bbox,
            "approval_excel":    approval_rate_excel,
            "reviewed_bbox":     total_reviewed_bbox,
            "reviewed_excel":    total_reviewed_excel,
            "approved_bbox":     approved_bbox,
            "approved_excel":    stage_counts["excel_approved"],
            "avg_bbox_per_day":  avg_bbox_per_day,
            "rejection_backlog": rejection_backlog,
            "est_completion":    est_completion,
        },
        "pipeline": {
            "pending":              stage_counts["pending"],
            "in_annotation":        stage_counts["in_annotation"],
            "pending_review":       stage_counts["pending_review"],
            "rejected":             stage_counts["rejected"],
            "bbox_approved":        stage_counts["bbox_approved"],
            "excel_pending":        stage_counts["excel_pending"],
            "excel_in_labeling":    stage_counts["excel_in_labeling"],
            "excel_pending_review": stage_counts["excel_pending_review"],
            "excel_rejected":       stage_counts["excel_rejected"],
            "excel_approved":       stage_counts["excel_approved"],
        },
        "approval_bbox": {
            "approved": stage_counts["bbox_approved"],
            "pending":  stage_counts["pending_review"],
            "rejected": stage_counts["rejected"],
        },
        "approval_excel": {
            "approved": stage_counts["excel_approved"],
            "pending":  stage_counts["excel_pending_review"],
            "rejected": stage_counts["excel_rejected"],
        },
        "throughput": {
            "labels": day_labels,
            "bbox":   [bbox_by_day[lbl] for lbl in day_labels],
            "excel":  [excel_by_day[lbl] for lbl in day_labels],
        },
        "annotator_chart":    annotator_chart,
        "excel_labeler_chart":excel_labeler_chart,
        "annotators":         annotator_cards,
        "excel_labelers":     excel_labeler_cards,
        "reviewers":          reviewer_cards,
        "drive_counts":       drive_counts,
        "last_synced":        now.isoformat(),
    }
