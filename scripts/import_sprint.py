"""
One-off importer: turn the current Jira active-sprint work into dashboard work packages.

For each epic in TARGETS it creates a top-level WorkPackage linked to that Jira epic, and copies the
epic's active-sprint activities in as tasks (title + assignee + status + story points) ON THE WORK
PACKAGE ITSELF for every category. Customer WPs keep the 12-step abacus empty. Story points are pulled
per epic so the Jira badge shows on open. The two work packages that were Active before the very first
import are set Inactive.

Read-only against Jira; writes to the local abacus.db. Safe to re-run: an existing target work package
is reused and its tasks are rebuilt from the current sprint (and any leftover sub-work-packages from an
earlier version are removed). Run from anywhere:

    python scripts/import_sprint.py
"""
import os
import sys
from datetime import datetime
from pathlib import Path

APP = Path(__file__).resolve().parent.parent

# Load .env manually (python-dotenv isn't installed, so nothing else does it) for JIRA_API_TOKEN.
env = APP / ".env"
if env.exists():
    for line in env.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

sys.path.insert(0, str(APP))
import jira_client  # noqa: E402
from models import SessionLocal, WorkPackage, WpTask, init_db  # noqa: E402

# category, work-package name (verbatim from the screenshot), customer, icon, jira epic key
TARGETS = [
    ("Customer", "HIJ DRUGS v2- WSB", "HIJ", "💊", "PP-2575"),
    ("Customer", "PPG P4- Pathology", "PPG", "🧪", "PP-2158"),
    ("Customer", "Monica Vinader - Jewellery", "Monica Vinader", "💎", "MON-202"),
    ("Customer", "Monica Vinader - Tranche 1", "Monica Vinader", "💎", "MON-749"),
    ("Customer", "DMU - Catering DWY", "DMU", "🍽️", "DMU-37"),
    ("Customer", "DMU - Project Support", "DMU", "🎓", "DMU-261"),
    ("Customer", "Bridgepoint Cost Review", "Bridgepoint", "🏢", "BPT-328"),
    ("Customer", "PPG - TSA Support", "PPG", "📦", "PP-2806"),
    ("Marketing", "Lead Generation", "Insider Pro", "📣", "ISPMKTG-1134"),
    ("Marketing", "Crafting Our Offer", "Insider Pro", "✍️", "ISPMKTG-1588"),
    ("Marketing", "Sales Meeting With potential clients", "Insider Pro", "🤝", "ISPMKTG-1180"),
    ("Process and Ops", "Business Development and Networking", "Insider Pro", "🌐", "ISPOPS2-2171"),
    ("Process and Ops", "Operational Effiency", "Insider Pro", "⚙️", "ISPOPS2-1"),
]

_STATUS_MAP = {"new": "todo", "indeterminate": "progress", "done": "done"}
_TARGET_KEYS = {(cat, name, client) for cat, name, client, _icon, _epic in TARGETS}


def read_sprint_by_epic():
    """{epic_key: [ {title, assignee, status, points}, ... ]} for the current active sprint."""
    sp_fields = jira_client.story_points_field_ids()   # e.g. customfield_10026 / _10016
    fields = "summary,assignee,status,parent," + ",".join(sp_fields)
    by_epic, tok = {}, None
    while True:
        params = {"jql": "sprint in openSprints() ORDER BY key ASC", "maxResults": 100, "fields": fields}
        if tok:
            params["nextPageToken"] = tok
        data = jira_client._get("/rest/api/3/search/jql", params=params)
        for it in data.get("issues", []):
            f = it.get("fields", {}) or {}
            parent = (f.get("parent") or {}).get("key")
            if not parent:
                continue
            cat = (((f.get("status") or {}).get("statusCategory") or {}).get("key") or "").lower()
            pts = None
            for fid in sp_fields:                 # first populated story-point field wins
                if f.get(fid) is not None:
                    pts = f.get(fid)
                    break
            by_epic.setdefault(parent, []).append({
                "title": (f.get("summary") or "(no title)")[:200],
                "assignee": ((f.get("assignee") or {}).get("displayName") or "").strip()[:128],
                "status": _STATUS_MAP.get(cat, "todo"),
                "points": int(round(pts)) if pts is not None else 1,
            })
        tok = data.get("nextPageToken")
        if data.get("isLast") or not tok:
            break
    return by_epic


def main():
    init_db()  # make sure the schema (incl. wp_task.assignee) is present
    print("Reading active sprint from Jira…")
    by_epic = read_sprint_by_epic()
    total_acts = sum(len(v) for v in by_epic.values())
    print(f"  {len(by_epic)} epics with {total_acts} active-sprint activities\n")

    created = reused = removed_subs = 0
    with SessionLocal.begin() as s:
        # work packages that are Active *and not one of our targets* get deactivated (only ever
        # bites on the first run - the original 2; re-runs won't touch the 13 imported ones)
        prior_active = [wp for wp in s.query(WorkPackage)
                        .filter(WorkPackage.parent_id.is_(None), WorkPackage.status == "Active").all()
                        if (wp.category or "Customer", wp.name, wp.client) not in _TARGET_KEYS]

        next_id = (s.query(WorkPackage.id).order_by(WorkPackage.id.desc()).first() or [0])[0] + 1

        for category, name, client, icon, epic in TARGETS:
            wp = (s.query(WorkPackage)
                  .filter(WorkPackage.parent_id.is_(None), WorkPackage.category == category,
                          WorkPackage.client == client, WorkPackage.name == name).first())
            if wp:
                # rebuild: drop any leftover sub-work-packages (cascades their tasks) + own tasks
                for child in list(wp.children):
                    s.delete(child)
                    removed_subs += 1
                s.query(WpTask).filter_by(wp_id=wp.id).delete()
                wp.jira_project_key = epic
                reused += 1
            else:
                wp = WorkPackage(id=next_id, client=client, name=name, description="", status="Active",
                                 category=category, icon=icon, jira_project_key=epic)
                s.add(wp)
                next_id += 1
                created += 1

            # tasks live directly on the work package for every category
            acts = by_epic.get(epic, [])
            for i, a in enumerate(acts, start=1):
                s.add(WpTask(wp_id=wp.id, title=a["title"], status=a["status"],
                             points=a["points"], assignee=a["assignee"], seq=i))

            # pull done/total story points for the epic so the Jira badge is populated
            try:
                pts = jira_client.epic_points(epic)
                wp.jira_done, wp.jira_total, wp.jira_synced_at = pts["done"], pts["total"], datetime.utcnow()
                pts_str = f"{pts['done']}/{pts['total']} pts"
            except jira_client.JiraError as e:
                pts_str = f"(points failed: {e})"

            spread = ",".join(str(a["points"]) for a in acts) or "-"
            print(f"  + [{category}] {client} - {name}  ({len(acts)} tasks [{spread}], epic {pts_str})")

        for wp in prior_active:
            wp.status = "Inactive"
        if prior_active:
            print("\n  deactivated: " + ", ".join(f"{w.client} - {w.name}" for w in prior_active))

    print(f"\nDone. Created {created}, reused {reused}, removed {removed_subs} old sub-work-package(s).")


if __name__ == "__main__":
    main()
