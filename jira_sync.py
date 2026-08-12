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
from models import (SessionLocal, WorkPackage, WpTask, Sprint, SprintHistory,
                   WpJiraLink, WpStatus, Subprocess)

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


# --------------------------------------------------------------------------- #
# Two-way status sync of PUSHED steps (wp_jira_link).
#
# The app never applies anything blindly: build_sync_plan() produces a review of
# what changed on each side since the last accepted sync; apply_sync_plan() then
# writes only the changes the user accepted. On the test build Abacus->Jira
# transitions are simulated (allow_jira_writes=False) so no real issue is moved.
# --------------------------------------------------------------------------- #

# Abacus RAG value <-> Jira statusCategory key. '' / 'N/R' have no Jira category.
_CAT_FOR = {"3": "done", "2": "indeterminate", "1": "new"}
_VAL_FOR = {"done": "3", "indeterminate": "2", "new": "1"}
VALUE_LABEL = {"": "Not started", "1": "Outstanding", "2": "In progress",
               "3": "Complete", "N/R": "Not required"}
CAT_LABEL = {"new": "To Do", "indeterminate": "In Progress", "done": "Done", "": "Unknown"}


def _set_point(s, wp_id, code, value):
    """Upsert/delete a single wp_status row (mirrors app._set_point). The sync only
    ever writes '1'/'2'/'3' from Jira, never '' - but the delete branch is kept safe."""
    row = s.get(WpStatus, {"wp_id": int(wp_id), "code": code})
    if value == "":
        if row:
            s.delete(row)
    elif row:
        row.value = value
    else:
        s.add(WpStatus(wp_id=int(wp_id), code=code, value=value))


def _linked_wps(s, wp_id=None):
    q = s.query(WorkPackage).filter(WorkPackage.jira_project_key.isnot(None),
                                    WorkPackage.jira_project_key != "")
    if wp_id is not None:
        q = q.filter(WorkPackage.id == int(wp_id))
    return [wp for wp in q.all() if (wp.jira_project_key or "").strip()]


def _classify(link, jira_cur, abacus_cur):
    """Which side changed since the last accepted sync, and what that means.
    Returns one of: 'none', 'silent', 'from_jira', 'from_abacus', 'conflict'."""
    jc = jira_cur != (link.last_jira_status or "new")
    ac = abacus_cur != (link.last_abacus_value or "")
    acat = _CAT_FOR.get(abacus_cur)          # None for '' / 'N/R'
    tval = _VAL_FOR.get(jira_cur)            # None for an unknown Jira category
    if not jc and not ac:
        return "none"
    if jc and not ac:
        # Jira moved; if the local value already equals the mapped value, nothing to do.
        return "silent" if (tval is None or tval == abacus_cur) else "from_jira"
    if ac and not jc:
        # Abacus moved; if it has no Jira category, or already matches Jira, nothing to send.
        return "silent" if (acat is None or acat == jira_cur) else "from_abacus"
    # both moved
    if acat is not None and acat == jira_cur:
        return "silent"
    return "conflict"


def _item(link, wp_label, step_label, jira_cur, abacus_cur, kind):
    tval = _VAL_FOR.get(jira_cur, "")
    acat = _CAT_FOR.get(abacus_cur, "")
    return {
        "wp_id": link.wp_id, "code": link.code, "jira_issue_key": link.jira_issue_key,
        "wp_label": wp_label, "step_label": step_label, "kind": kind,
        # the Abacus side (what a from_jira / keep-Jira decision would set locally)
        "abacus_cur": abacus_cur, "abacus_cur_label": VALUE_LABEL.get(abacus_cur, abacus_cur),
        "abacus_target": tval, "abacus_target_label": VALUE_LABEL.get(tval, tval),
        # the Jira side (what a from_abacus / keep-site decision would transition to)
        "jira_cur": jira_cur, "jira_cur_label": CAT_LABEL.get(jira_cur, jira_cur or "Unknown"),
        "jira_target": acat, "jira_target_label": CAT_LABEL.get(acat, acat or "Unknown"),
    }


def _sub_labels(s):
    return {sp.code: ((sp.outcomes or "").strip() or (sp.question or "").strip())
            for sp in s.query(Subprocess).all()}


def build_sync_plan(wp_id=None):
    """Read-only: compare each pushed step's live Jira status and live Abacus value to
    the baselines on its wp_jira_link, and group the pending changes into
    from_jira / from_abacus / conflicts. Writes nothing."""
    if not jira_client.is_configured():
        return {"configured": False, "from_jira": [], "from_abacus": [], "conflicts": []}
    from_jira, from_abacus, conflicts = [], [], []
    errors = []
    with SessionLocal() as s:
        sub_label = _sub_labels(s)
        for wp in _linked_wps(s, wp_id):
            links = s.query(WpJiraLink).filter_by(wp_id=wp.id).all()
            if not links:
                continue
            epic = (wp.jira_project_key or "").strip()
            try:
                children = jira_client.epic_children(epic)
            except jira_client.JiraError as e:
                errors.append({"epic": epic, "error": str(e)})
                continue
            status_by_key = {c["key"]: (c.get("status") or "") for c in children}
            vals = {st.code: st.value for st in s.query(WpStatus).filter_by(wp_id=wp.id).all()}
            wp_label = f"{wp.client} - {wp.name}".strip(" -")
            for link in links:
                jira_cur = status_by_key.get(link.jira_issue_key)
                if jira_cur is None:            # issue deleted / not found - leave the stale link
                    continue
                abacus_cur = vals.get(link.code, "")
                kind = _classify(link, jira_cur, abacus_cur)
                if kind in ("none", "silent"):
                    continue
                step_label = f"{link.code} {sub_label.get(link.code, '')}".strip()
                item = _item(link, wp_label, step_label, jira_cur, abacus_cur, kind)
                (from_jira if kind == "from_jira"
                 else from_abacus if kind == "from_abacus"
                 else conflicts).append(item)
    return {"configured": True, "from_jira": from_jira, "from_abacus": from_abacus,
            "conflicts": conflicts, "errors": errors}


def apply_sync_plan(decisions, allow_jira_writes=False):
    """Apply the user's accepted decisions. Re-derives the current diff (so a stale or
    tampered decision can't apply an unexpected change), writes accepted Jira->Abacus
    changes locally, and either performs or SIMULATES accepted Abacus->Jira transitions
    depending on allow_jira_writes. Silently advances baselines for no-op reconciles."""
    if not jira_client.is_configured():
        return {"error": "Jira is not configured on the server."}
    dmap = {}
    for d in (decisions or []):
        try:
            dmap[(int(d.get("wp_id")), str(d.get("code")))] = str(d.get("action") or "skip")
        except (TypeError, ValueError):
            continue

    applied, simulated, failed, skipped = [], [], [], []
    to_transition = []   # real Jira writes, done after the DB commit (network off the lock)

    with SessionLocal() as s:
        sub_label = _sub_labels(s)
        for wp in _linked_wps(s):
            links = s.query(WpJiraLink).filter_by(wp_id=wp.id).all()
            if not links:
                continue
            epic = (wp.jira_project_key or "").strip()
            try:
                children = jira_client.epic_children(epic)
            except jira_client.JiraError:
                continue
            status_by_key = {c["key"]: (c.get("status") or "") for c in children}
            vals = {st.code: st.value for st in s.query(WpStatus).filter_by(wp_id=wp.id).all()}
            wp_label = f"{wp.client} - {wp.name}".strip(" -")
            for link in links:
                jira_cur = status_by_key.get(link.jira_issue_key)
                if jira_cur is None:
                    continue
                abacus_cur = vals.get(link.code, "")
                kind = _classify(link, jira_cur, abacus_cur)
                step_label = f"{link.code} {sub_label.get(link.code, '')}".strip()

                # no-op reconciles: advance baselines so they stop being detected
                if kind == "silent":
                    link.last_jira_status, link.last_abacus_value = jira_cur, abacus_cur
                    link.updated_at = datetime.utcnow()
                    continue
                if kind == "none":
                    continue

                action = dmap.get((wp.id, link.code))
                if not action or action == "skip":
                    continue   # untouched -> stays pending for next time

                do_local = (kind == "from_jira" and action == "accept") or \
                           (kind == "conflict" and action == "accept_jira")
                do_jira = (kind == "from_abacus" and action == "accept") or \
                          (kind == "conflict" and action == "accept_site")
                rec = {"wp_id": wp.id, "code": link.code, "wp_label": wp_label,
                       "step_label": step_label, "jira_issue_key": link.jira_issue_key}

                if do_local:
                    val = _VAL_FOR.get(jira_cur, "")
                    if val:
                        _set_point(s, wp.id, link.code, val)
                        link.last_jira_status, link.last_abacus_value = jira_cur, val
                        link.updated_at = datetime.utcnow()
                        applied.append({**rec, "direction": "jira->abacus",
                                        "detail": f"set to {VALUE_LABEL.get(val, val)}"})
                elif do_jira:
                    tcat = _CAT_FOR.get(abacus_cur)
                    if tcat is None:
                        # abacus went to '' / 'N/R' - nothing to move in Jira; accept the divergence
                        link.last_jira_status, link.last_abacus_value = jira_cur, abacus_cur
                        link.updated_at = datetime.utcnow()
                        applied.append({**rec, "direction": "abacus->jira",
                                        "detail": "no Jira status to move to; kept as-is"})
                    elif allow_jira_writes:
                        to_transition.append({**rec, "target_cat": tcat, "abacus_cur": abacus_cur})
                    else:
                        # simulated on the test build: advance baselines, don't touch Jira
                        link.last_jira_status, link.last_abacus_value = tcat, abacus_cur
                        link.updated_at = datetime.utcnow()
                        simulated.append({**rec, "direction": "abacus->jira",
                                          "detail": f"would set Jira to {CAT_LABEL.get(tcat, tcat)}"})
        s.commit()

    # real Jira transitions (network) - each success advances its own baseline
    for t in to_transition:
        try:
            jira_client.transition_issue_to_category(t["jira_issue_key"], t["target_cat"])
        except jira_client.JiraError as e:
            failed.append({**{k: t[k] for k in ("wp_id", "code", "wp_label", "step_label",
                                                "jira_issue_key")},
                           "direction": "abacus->jira", "error": str(e)})
            continue
        with SessionLocal.begin() as s:
            link = s.get(WpJiraLink, {"wp_id": int(t["wp_id"]), "code": t["code"]})
            if link:
                link.last_jira_status = t["target_cat"]
                link.last_abacus_value = t["abacus_cur"]
                link.updated_at = datetime.utcnow()
        applied.append({k: t[k] for k in ("wp_id", "code", "wp_label", "step_label",
                                          "jira_issue_key")}
                       | {"direction": "abacus->jira",
                          "detail": f"moved Jira to {CAT_LABEL.get(t['target_cat'], t['target_cat'])}"})

    return {"ok": True, "applied": applied, "simulated": simulated,
            "failed": failed, "skipped": skipped}
