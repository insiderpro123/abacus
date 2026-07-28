"""
Manual "Sync from Jira" - the app is the source of truth; this pulls the latest from
Jira on demand (read-only). Two jobs:

  1. sync_active_sprint(): refresh the current-sprint task cards from Jira's open sprint,
     matched to work packages by their linked epic. Tasks are matched by Jira issue key so
     re-syncing updates status / points / priority / assignee in place and never clobbers
     app-side planning (priority tweaks, sprint moves, hand-made tasks).
  2. backfill_history(): read recently-closed sprints and snapshot per-week, per-category
     points into SprintHistory so the history view keeps working even after Jira is dropped.

All Jira access is read-only and goes through jira_client (platform search API).
"""
from datetime import datetime, timedelta

import jira_client
from models import SessionLocal, WorkPackage, WpTask, Sprint, SprintHistory

# Category of an issue, by its Jira project (the key prefix). Anything not listed is a Customer.
CATEGORY_BY_PROJECT = {"ISPMKTG": "Marketing", "ISPOPS2": "Process and Ops", "ODM": "Process and Ops"}

# Jira priority name -> app scale (1 = highest … 5 = lowest)
PRIORITY_MAP = {"blocker": 1, "highest": 1, "high": 2, "medium": 3, "low": 4, "lowest": 5}
_STATUS_MAP = {"new": "todo", "indeterminate": "progress", "done": "done"}
_HISTORY_WINDOW_DAYS = 180


def category_for_key(issue_key):
    proj = (issue_key or "").split("-")[0]
    return CATEGORY_BY_PROJECT.get(proj, "Customer")


def _priority_from(fields):
    name = ((fields.get("priority") or {}).get("name") or "").strip().lower()
    return PRIORITY_MAP.get(name, 3)


def _points_from(fields, sp_fields):
    for fid in sp_fields:
        if fields.get(fid) is not None:
            try:
                return int(round(float(fields[fid])))
            except (TypeError, ValueError):
                return 0
    return 0


def _status_from(fields):
    cat = (((fields.get("status") or {}).get("statusCategory") or {}).get("key") or "").lower()
    return _STATUS_MAP.get(cat, "todo")


def _monday(date_str):
    """ISO date string (any 'YYYY-MM-DD...') -> the Monday of that week, or None."""
    if not date_str:
        return None
    try:
        d = datetime.strptime(str(date_str)[:10], "%Y-%m-%d").date()
    except ValueError:
        return None
    return d - timedelta(days=d.weekday())


def _search(jql, fields):
    """Paginated platform JQL search (read-only)."""
    out, tok = [], None
    while True:
        params = {"jql": jql, "maxResults": 100, "fields": fields}
        if tok:
            params["nextPageToken"] = tok
        data = jira_client._get("/rest/api/3/search/jql", params=params)
        out += data.get("issues", [])
        tok = data.get("nextPageToken")
        if data.get("isLast") or not tok:
            break
    return out


# --------------------------------------------------------------------------- #
# 1. Refresh each linked epic's story-point totals (for the Jira badge on work packages)
# --------------------------------------------------------------------------- #
def refresh_epic_points(s):
    epics = [(wp, wp.jira_project_key.strip())
             for wp in s.query(WorkPackage).filter(WorkPackage.parent_id.is_(None)).all()
             if (wp.jira_project_key or "").strip()]
    done = 0
    for wp, epic in epics:
        try:
            pts = jira_client.epic_points(epic)
            wp.jira_done, wp.jira_total, wp.jira_synced_at = pts["done"], pts["total"], datetime.utcnow()
            done += 1
        except jira_client.JiraError:
            pass
    return {"epics": done}


# --------------------------------------------------------------------------- #
# 2. Closed + active sprints -> weekly per-category points history (the dashboard)
# --------------------------------------------------------------------------- #
def backfill_history(s):
    sp_fields = jira_client.story_points_field_ids()
    fields = "summary,status,parent,customfield_10020," + ",".join(sp_fields)
    # include the OPEN sprint too, so the current week shows in the dashboard
    issues = _search(
        f"(sprint in openSprints() OR (sprint in closedSprints() AND updated >= -{_HISTORY_WINDOW_DAYS}d)) "
        f"ORDER BY updated DESC", fields)

    # accumulate per (week_start, category)
    weeks = {}   # (week_start_iso, category) -> dict of tallies
    labels = {}  # week_start_iso -> (week_end_iso, label)
    for it in issues:
        f = it.get("fields", {}) or {}
        # pick the issue's most-recent closed/active sprint (avoids double counting across weeks)
        home = None
        for sp in (f.get("customfield_10020") or []):
            if (sp.get("state") or "") not in ("closed", "active"):
                continue
            mon = _monday(sp.get("startDate") or sp.get("completeDate") or sp.get("endDate"))
            if not mon:
                continue
            if home is None or mon > home:
                home = mon
        if home is None:
            continue
        category = category_for_key(it.get("key", ""))
        points = _points_from(f, sp_fields)
        status = _status_from(f)
        wk = home.isoformat()
        fri = (home + timedelta(days=4))
        labels[wk] = (fri.isoformat(), f"{home.day}-{fri.day} {fri.strftime('%b %y')}")
        agg = weeks.setdefault((wk, category), {
            "points_planned": 0, "points_done": 0,
            "tasks_total": 0, "tasks_todo": 0, "tasks_progress": 0, "tasks_done": 0})
        agg["points_planned"] += points
        agg["tasks_total"] += 1
        agg["tasks_" + status] += 1
        if status == "done":
            agg["points_done"] += points

    # replace the Jira-sourced rows for the weeks we just recomputed
    weeks_seen = {wk for (wk, _cat) in weeks}
    if weeks_seen:
        (s.query(SprintHistory)
         .filter(SprintHistory.source == "jira", SprintHistory.week_start.in_(weeks_seen))
         .delete(synchronize_session=False))
    for (wk, category), agg in weeks.items():
        we, label = labels[wk]
        s.add(SprintHistory(week_start=wk, week_end=we, label=label, category=category,
                            source="jira", captured_at=datetime.utcnow(), **agg))

    return {"weeks": len(weeks_seen), "rows": len(weeks)}


def active_sprint_status():
    """Read-only: what does Jira currently call its active sprint(s), and how many tasks
    fall under our linked work-package epics? Used to check the app is aligned with Jira."""
    if not jira_client.is_configured():
        return {"configured": False}
    issues = _search("sprint in openSprints()", "parent,customfield_10020")
    with SessionLocal() as s:
        epics = {(wp.jira_project_key or "").strip()
                 for wp in s.query(WorkPackage).filter(WorkPackage.parent_id.is_(None)).all()
                 if (wp.jira_project_key or "").strip()}
    sprints, count = {}, 0
    for it in issues:
        f = it.get("fields", {}) or {}
        if (f.get("parent") or {}).get("key") in epics:
            count += 1
        for sp in (f.get("customfield_10020") or []):
            if (sp.get("state") or "") == "active":
                sprints[sp.get("name")] = {"start": str(sp.get("startDate"))[:10],
                                           "end": str(sp.get("endDate"))[:10]}
    return {"configured": True,
            "sprints": [{"name": n, **v} for n, v in sprints.items()],
            "task_count": count}


def run_sync():
    """Full manual sync: refresh work-package epic badges + rebuild the points dashboard."""
    if not jira_client.is_configured():
        return {"error": "Jira is not configured (JIRA_API_TOKEN is not set)."}
    with SessionLocal.begin() as s:
        epics = refresh_epic_points(s)
        history = backfill_history(s)
    return {"ok": True, "epics": epics, "history": history}
