"""Pull the sprint retro figures straight from Jira Cloud (Sprint Retro page of Abacus).

A COPY of jira_pull.py from the local Sprint Retro Dashboard
(ISP Project Management Documents\\01 Standardisation\\Process - Sprint Retro Dashboard\\03 Live).
The counting rules must stay identical in both, or the live page and the local dashboard will
disagree: change one, change the other.

On Abacus, app.py calls build() from the page's Sync button and stores the result in the
database (Render's disk does not survive a restart). From the command line:

    python retro_pull.py              # pull and write sprint_data.js (local testing only)
    python retro_pull.py --check      # test the credentials, report data quality
    python retro_pull.py --reconcile  # compare this tool's numbers against Abacus's rule

EVERYTHING COMES FROM JIRA
    Project names, sprint names and people's names are all read from the Jira issues
    themselves - fields.project.name, the sprint field, and fields.assignee.displayName.
    Nothing is copied from SprintRetro_New.xlsx, so a new project or a new joiner appears
    on the dashboard the moment they appear in Jira.

    The single exception is the sector split (Customer / Marketing / Process and Ops),
    which does not exist as a Jira field at all. It is derived from the project key using
    the same rule Abacus uses (see SECTOR_BY_PROJECT).

WHAT "COMPLETED" MEANS HERE
    A point counts towards a week when Jira's resolutiondate falls in that Monday-Friday
    week. That is a real completion timestamp, so past weeks never change.

    Abacus does it differently: it reads each issue's CURRENT status and attributes it to
    the issue's sprint. That rewrites history on every sync. The two will disagree; run
    --reconcile to see by how much and why.

    Some workflows close an issue without setting a resolution, which leaves resolutiondate
    empty. Rather than drop that work, we use statuscategorychangedate (when its status moved
    to Done), and only if that is missing too, the issue's sprint week. How often it happened
    is counted in the payload.

    A point therefore only ever counts in the week the task was actually finished. Work that
    was completed earlier and is still attached to a later sprint is never counted again.

CREDENTIALS
    On Render: the JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN environment variables Abacus
    already uses. Locally: this folder's .env (app.py loads it into the environment first).
"""

import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent

# The Jira token lives here. One file, one place to rotate it.
ABACUS_ENV = HERE / ".." / ".." / "Process - Abacus" / "02 Working" / "abacus on render" / ".env"

TIMEOUT = 30    # seconds per request
_PAGE = 100     # Jira's maximum page size
HISTORY_DAYS = 190

# Jira names the story-point field differently in company- and team-managed projects.
_SP_FIELD_NAMES = ("story points", "story point estimate")

# The sprint field. Hardcoded in Abacus too (jira_sync.py) - it is stable for this site.
SPRINT_FIELD = "customfield_10020"

THE_SECTORS = ("Customer", "Marketing", "Process and Ops")

# Sector is not a Jira field, so it has to be derived from the project key. Same rule as
# Abacus (jira_sync.CATEGORY_BY_PROJECT). Everything that is not internal is client work.
SECTOR_BY_PROJECT = {
    "ISPMKTG": "Marketing",
    "ISPOPS2": "Process and Ops",
    "ODM": "Process and Ops",
}

# Jira projects kept off the retro entirely - internal admin rather than sprint delivery.
# Filtered at the pull, so they stay out of the chart, the totals and the per-person table,
# and never reappear after a sync. Remove a code from here to bring it back.
EXCLUDE_PROJECTS = {"ODM"}

HIGH_PRIORITIES = {"blocker", "highest", "high"}

# The order project cards appear in on the dashboard.
SECTOR_RANK = {"Customer": 0, "Process and Ops": 1, "Marketing": 2}


class JiraError(Exception):
    """Any Jira call that failed, with a message fit to show on screen."""


# --------------------------------------------------------------------------- config

def _read_env_file(path):
    """Tiny .env reader. python-dotenv is not installed and is not worth a dependency."""
    out = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip().strip('"').strip("'")
        if val:
            out[key.strip()] = val
    return out


def load_config():
    """Environment wins, then a local .env, then the Abacus .env."""
    cfg = {}
    cfg.update(_read_env_file(ABACUS_ENV))
    cfg.update(_read_env_file(HERE / ".env"))
    for key in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN"):
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    return {
        "base_url": (cfg.get("JIRA_BASE_URL") or "https://insiderpro.atlassian.net").rstrip("/"),
        "email": cfg.get("JIRA_EMAIL") or "team@insiderpro.co.uk",
        "token": cfg.get("JIRA_API_TOKEN") or "",
    }


# --------------------------------------------------------------------------- Jira client

class Jira:
    """Read-only Jira Cloud client.

    The site URL rejects the modern scoped token, so every authenticated call goes through
    the api.atlassian.com gateway with a cloudId resolved from the site's public tenant_info
    endpoint. This mirrors abacus on render/jira_client.py, where the approach is proven.
    """

    def __init__(self, cfg=None):
        cfg = cfg or load_config()
        self.base_url = cfg["base_url"]
        self.email = cfg["email"]
        self.token = cfg["token"]
        self._cloud_id = None
        self._sp_fields = None
        self.session = requests.Session()

    @property
    def configured(self):
        return bool(self.token)

    def cloud_id(self):
        if self._cloud_id:
            return self._cloud_id
        try:
            r = self.session.get(f"{self.base_url}/_edge/tenant_info", timeout=TIMEOUT)
            r.raise_for_status()
            self._cloud_id = r.json()["cloudId"]
        except (requests.RequestException, KeyError, ValueError) as e:
            raise JiraError(f"Could not resolve the Jira cloudId from {self.base_url}: {e}") from e
        return self._cloud_id

    def get(self, path, params=None):
        if not self.configured:
            raise JiraError("Jira is not configured - JIRA_API_TOKEN is not set.")
        url = f"https://api.atlassian.com/ex/jira/{self.cloud_id()}{path}"
        try:
            r = self.session.get(url, params=params, auth=(self.email, self.token),
                                 headers={"Accept": "application/json"}, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise JiraError(f"Could not reach Jira: {e}") from e
        if r.status_code == 401:
            raise JiraError("Jira rejected the credentials (401). Check JIRA_EMAIL and JIRA_API_TOKEN.")
        if r.status_code == 403:
            raise JiraError("Jira denied access (403). The token may lack the read:jira-work scope.")
        if r.status_code == 429:
            raise JiraError("Jira is rate limiting this token (429). Wait a minute and sync again.")
        if r.status_code >= 400:
            raise JiraError(f"Jira returned {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError as e:
            raise JiraError("Jira returned a non-JSON response.") from e

    def story_point_fields(self):
        if self._sp_fields is not None:
            return self._sp_fields
        self._sp_fields = [
            f["id"] for f in self.get("/rest/api/3/field")
            if (f.get("name") or "").strip().lower() in _SP_FIELD_NAMES and f.get("id")
        ]
        return self._sp_fields

    def search(self, jql, fields):
        """Paginated JQL search against the platform endpoint."""
        issues, token = [], None
        while True:
            params = {"jql": jql, "maxResults": _PAGE, "fields": fields}
            if token:
                params["nextPageToken"] = token
            data = self.get("/rest/api/3/search/jql", params=params)
            issues += data.get("issues", [])
            token = data.get("nextPageToken")
            if data.get("isLast") or not token:
                break
        return issues


# --------------------------------------------------------------------------- field helpers

def monday_of(value):
    """Any 'YYYY-MM-DD...' string or date -> the Monday of that week, or None."""
    if not value:
        return None
    if isinstance(value, date):
        return value - timedelta(days=value.weekday())
    try:
        d = datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    return d - timedelta(days=d.weekday())


def weeks_in_year(year):
    """ISO weeks in a year: 52 or 53. Abacus hardcodes '/52', which is wrong some years."""
    return date(year, 12, 28).isocalendar()[1]


def week_label(monday):
    friday = monday + timedelta(days=4)
    if monday.month == friday.month:
        return f"{monday.day}-{friday.day} {friday.strftime('%b %y')}"
    return f"{monday.day} {monday.strftime('%b')}-{friday.day} {friday.strftime('%b %y')}"


def points_of(fields, sp_fields):
    """First populated story-point field wins; missing or unparseable means zero."""
    for fid in sp_fields:
        val = fields.get(fid)
        if val is not None:
            try:
                return int(round(float(val)))
            except (TypeError, ValueError):
                return 0
    return 0


def is_done(fields):
    cat = (((fields.get("status") or {}).get("statusCategory") or {}).get("key") or "").lower()
    return cat == "done"


def project_code(issue_key):
    return (issue_key or "").split("-")[0].upper()


def is_excluded(issue_key, fields=None):
    """True for projects the retro deliberately ignores (see EXCLUDE_PROJECTS)."""
    proj = (fields or {}).get("project") or {}
    code = (proj.get("key") or project_code(issue_key) or "").upper()
    return code in EXCLUDE_PROJECTS


def project_of(issue_key, fields):
    """(code, name, sector) - the name straight from Jira, never from a lookup table."""
    proj = fields.get("project") or {}
    code = (proj.get("key") or project_code(issue_key) or "").upper()
    name = (proj.get("name") or "").strip() or code or "Unknown"
    return code, name, SECTOR_BY_PROJECT.get(code, "Customer")


def epic_of(fields):
    """(key, name) of the issue's epic, off the Jira parent field.

    Only a parent that is itself an epic counts. A subtask's parent is a story, so a
    subtask lands under "No epic" rather than costing an extra lookup per issue.
    """
    parent = fields.get("parent") or {}
    pf = parent.get("fields") or {}
    it = pf.get("issuetype") or {}
    if parent.get("key") and ((it.get("name") or "").lower() == "epic" or it.get("hierarchyLevel") == 1):
        return parent["key"], (pf.get("summary") or "").strip() or parent["key"]
    return None, "No epic"


def _tidy_name(raw):
    """Jira's own displayName, just cased sensibly: 'will.buggey' -> 'Will Buggey'.

    Purely mechanical - it only reshapes the string Jira returned. No name list.
    """
    name = (raw or "").strip()
    if not name:
        return "Unassigned"
    if name != name.lower():
        return name          # Jira already has it properly cased, leave it alone
    return " ".join(part.capitalize() for part in name.replace(".", " ").replace("_", " ").split())


def assignee_of(fields):
    """(key, display name). accountId is the join key so a rename does not split history."""
    a = fields.get("assignee") or {}
    account = a.get("accountId") or ""
    name = _tidy_name(a.get("displayName") or "")
    if not account:
        return "unassigned", "Unassigned"
    return account, name


def home_sprint(fields):
    """The issue's most recent closed or active sprint, straight off the Jira field."""
    best = None
    for sp in (fields.get(SPRINT_FIELD) or []):
        if not isinstance(sp, dict):
            continue
        state = (sp.get("state") or "").lower()
        if state not in ("closed", "active"):
            continue
        mon = monday_of(sp.get("startDate") or sp.get("completeDate") or sp.get("endDate"))
        if not mon:
            continue
        if best is None or mon > best["monday"]:
            best = {
                "monday": mon,
                "id": sp.get("id"),
                "name": (sp.get("name") or "").strip(),
                "state": state,
                "start": str(sp.get("startDate") or "")[:10],
                "end": str(sp.get("endDate") or sp.get("completeDate") or "")[:10],
            }
    return best


def sprint_monday(fields):
    h = home_sprint(fields)
    return h["monday"] if h else None


# --------------------------------------------------------------------------- aggregation

def _blank_week(monday):
    friday = monday + timedelta(days=4)
    iso_year, iso_week, _ = monday.isocalendar()
    return {
        "start": monday.isoformat(),
        "end": friday.isoformat(),
        "label": week_label(monday),
        "iso_week": iso_week,
        "iso_weeks_in_year": weeks_in_year(iso_year),
        "sprint_names": [],
        "by_category": {c: {"done": 0, "committed": 0, "committed_done": 0} for c in THE_SECTORS},
        "people": {},
        "projects": {},
        "done": 0,
        "committed": 0,
        "committed_done": 0,
        "done_high": 0,
        "issues_done": 0,
        "issues_committed": 0,
        "blockers": 0,
        "bugs": 0,
        "no_resolution_date": 0,
    }


def _person(week, key, name):
    p = week["people"].get(key)
    if not p:
        p = week["people"][key] = {
            "key": key, "name": name,
            "by_category": {c: {"done": 0, "committed": 0} for c in THE_SECTORS},
            "done": 0, "committed": 0, "committed_done": 0,
            "done_high": 0, "issues_done": 0, "blockers": 0, "bugs": 0,
        }
    p["name"] = name          # keep the freshest name Jira gave us
    return p


def _project(week, code, name, sector):
    p = week["projects"].get(code)
    if not p:
        p = week["projects"][code] = {
            "code": code, "name": name, "sector": sector,
            "done": 0, "committed": 0, "committed_done": 0,
            "issues_done": 0, "issues_committed": 0,
            "blockers": 0, "bugs": 0, "done_items": [], "epics": {},
        }
    p["name"] = name
    return p


def _epic(proj, key, name):
    """The epic's slice of a project's week. Same counters as the project it sits in."""
    slot = key or (proj["code"] + ":none")
    e = proj["epics"].get(slot)
    if not e:
        e = proj["epics"][slot] = {
            "key": slot, "epic": key, "name": name,
            "done": 0, "committed": 0, "committed_done": 0,
            "issues_done": 0, "issues_committed": 0,
            "blockers": 0, "bugs": 0, "done_items": [],
        }
    e["name"] = name
    return e


def epic_totals(jira, keys, sp_fields):
    """{epic key: story points across every child}, whatever the child's status or sprint.

    Read at sync time, so a past week shows the epic's total as it stands today.
    """
    totals = {k: 0 for k in keys}
    keys = sorted(keys)
    fields = ",".join(["parent"] + sp_fields)
    for i in range(0, len(keys), 40):
        chunk = keys[i:i + 40]
        for it in jira.search("parent in (" + ",".join(chunk) + ")", fields):
            f = it.get("fields") or {}
            pk = (f.get("parent") or {}).get("key")
            if pk in totals:
                totals[pk] += points_of(f, sp_fields)
    return totals


def build(jira=None, history_days=HISTORY_DAYS):
    """Pull from Jira and return the full dashboard payload."""
    jira = jira or Jira()
    started = datetime.now(timezone.utc)
    sp_fields = jira.story_point_fields()

    field_list = ",".join(
        ["summary", "status", "assignee", "priority", "issuetype", "project",
         "parent", "resolutiondate", "statuscategorychangedate", SPRINT_FIELD] + sp_fields
    )

    # 1. What was genuinely finished, by resolution date.
    completed = jira.search(
        f"resolutiondate >= -{history_days}d ORDER BY resolutiondate DESC", field_list)

    # 2. What was committed to each sprint. The proven query from Abacus's jira_sync.py.
    committed = jira.search(
        f"(sprint in openSprints() OR (sprint in closedSprints() AND updated >= -{history_days}d))",
        field_list)

    dropped = len(completed) + len(committed)
    completed = [i for i in completed if not is_excluded(i.get("key", ""), i.get("fields") or {})]
    committed = [i for i in committed if not is_excluded(i.get("key", ""), i.get("fields") or {})]
    dropped -= len(completed) + len(committed)

    weeks = {}
    seen_people = {}
    sprints = {}          # monday iso -> {name: state} as Jira reports them

    def week_for(monday):
        if monday.isoformat() not in weeks:
            weeks[monday.isoformat()] = _blank_week(monday)
        return weeks[monday.isoformat()]

    def note_sprint(f):
        h = home_sprint(f)
        if h and h["name"]:
            sprints.setdefault(h["monday"].isoformat(), {})[h["name"]] = h["state"]

    # --- completed work: the bars, and each person's total for the week
    counted = set()
    for it in completed:
        key = it.get("key", "")
        f = it.get("fields", {}) or {}
        note_sprint(f)
        mon = monday_of(f.get("resolutiondate"))
        if not mon:
            continue
        counted.add(key)
        _tally_done(week_for(mon), key, f, sp_fields, seen_people)

    # --- done but with no resolution date: use the date its status moved to Done, which is
    # just as real a completion timestamp. The sprint week is only a last resort: it was wrong
    # for about a third of these, putting work in the week before it was actually finished.
    for it in committed:
        key = it.get("key", "")
        f = it.get("fields", {}) or {}
        note_sprint(f)
        if key in counted or not is_done(f) or f.get("resolutiondate"):
            continue
        mon = monday_of(f.get("statuscategorychangedate")) or sprint_monday(f)
        if not mon:
            continue
        counted.add(key)
        wk = week_for(mon)
        wk["no_resolution_date"] += 1
        _tally_done(wk, key, f, sp_fields, seen_people)

    # --- committed work: the denominator, plus open blockers and bugs
    for it in committed:
        key = it.get("key", "")
        f = it.get("fields", {}) or {}
        mon = sprint_monday(f)
        if not mon:
            continue
        _tally_committed(week_for(mon), key, f, sp_fields, seen_people)

    ordered = [weeks[k] for k in sorted(weeks)]
    for wk in ordered:
        names = sprints.get(wk["start"], {})
        wk["sprint_names"] = sorted(names)
        wk["sprint_active"] = any(s == "active" for s in names.values())
        wk["people"] = sorted(wk["people"].values(),
                              key=lambda p: (-p["done"], p["name"].lower()))
        # Client work first, then Process and Ops, then Marketing.
        wk["projects"] = sorted(wk["projects"].values(),
                                key=lambda p: (SECTOR_RANK.get(p["sector"], 9),
                                               -p["done"], p["name"].lower()))
        for proj in wk["projects"]:
            proj["done_items"] = proj["done_items"][:40]
            proj["epics"] = sorted(proj["epics"].values(),
                                   key=lambda e: (-e["done"], -e["committed"], e["name"].lower()))
            for e in proj["epics"]:
                e["done_items"] = e["done_items"][:40]

    # Every card reads "13 of 100": points completed that week of the epic's total. So every
    # epic with any work in a week needs a total. A customer card sums the epics its work
    # that week sat under; an Operations or Marketing card is one epic.
    active = lambda e: e["done"] > 0 or e["committed"] > 0
    epic_info = {}
    for wk in ordered:
        for proj in wk["projects"]:
            for e in proj["epics"]:
                if e["epic"] and active(e):
                    epic_info[e["epic"]] = {"name": e["name"], "project": proj["code"]}
    totals = epic_totals(jira, set(epic_info), sp_fields) if epic_info else {}
    for k, info in epic_info.items():
        info["total"] = totals.get(k, 0)
    for wk in ordered:
        for proj in wk["projects"]:
            for e in proj["epics"]:
                e["total"] = totals.get(e["epic"], 0) if e["epic"] else 0
            proj["epic_total"] = sum(e["total"] for e in proj["epics"] if active(e))

    return {
        "generated_at": started.astimezone().isoformat(timespec="seconds"),
        "basis": "resolutiondate",
        "history_days": history_days,
        "site": jira.base_url,
        "weeks": ordered,
        "latest_sprint": _latest_sprint_week(ordered),
        "people": sorted(seen_people.values(), key=lambda n: n.lower()),
        "sectors": list(THE_SECTORS),
        "epics": epic_info,
        "excluded_projects": sorted(EXCLUDE_PROJECTS),
        "counts": {
            "excluded_issues": dropped,
            "completed_issues": len(completed),
            "committed_issues": len(committed),
            "weeks": len(ordered),
            "no_resolution_date": sum(w["no_resolution_date"] for w in ordered),
        },
        "elapsed_seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 1),
    }


def _latest_sprint_week(weeks):
    """The most recent finished sprint week - what Monday's retro is about."""
    today = date.today()
    this_monday = today - timedelta(days=today.weekday())
    best = None
    for wk in weeks:
        if wk["start"] < this_monday.isoformat():
            best = wk
    if not best:
        return None
    return {"week_start": best["start"], "names": best["sprint_names"]}


def _tally_done(wk, key, f, sp_fields, seen_people):
    pts = points_of(f, sp_fields)
    code, name, sector = project_of(key, f)
    acct, person_name = assignee_of(f)
    seen_people[acct] = person_name
    priority = ((f.get("priority") or {}).get("name") or "").strip().lower()
    high = priority in HIGH_PRIORITIES

    wk["done"] += pts
    wk["issues_done"] += 1
    wk["by_category"][sector]["done"] += pts
    if high:
        wk["done_high"] += pts

    p = _person(wk, acct, person_name)
    p["done"] += pts
    p["issues_done"] += 1
    p["by_category"][sector]["done"] += pts
    if high:
        p["done_high"] += pts

    item = {
        "key": key,
        "summary": (f.get("summary") or "")[:160],
        "points": pts,
        "assignee": person_name,
    }
    proj = _project(wk, code, name, sector)
    epic = _epic(proj, *epic_of(f))
    for slot in (proj, epic):
        slot["done"] += pts
        slot["issues_done"] += 1
        slot["done_items"].append(item)


def _tally_committed(wk, key, f, sp_fields, seen_people):
    pts = points_of(f, sp_fields)
    code, name, sector = project_of(key, f)
    acct, person_name = assignee_of(f)
    seen_people[acct] = person_name
    priority = ((f.get("priority") or {}).get("name") or "").strip().lower()
    issue_type = ((f.get("issuetype") or {}).get("name") or "").strip().lower()
    done = is_done(f)

    wk["committed"] += pts
    wk["issues_committed"] += 1
    wk["by_category"][sector]["committed"] += pts
    if done:
        wk["committed_done"] += pts
        wk["by_category"][sector]["committed_done"] += pts

    p = _person(wk, acct, person_name)
    p["committed"] += pts
    p["by_category"][sector]["committed"] += pts
    if done:
        p["committed_done"] += pts

    proj = _project(wk, code, name, sector)
    epic = _epic(proj, *epic_of(f))
    for slot in (proj, epic):
        slot["committed"] += pts
        slot["issues_committed"] += 1
        if done:
            slot["committed_done"] += pts

    # Blockers are only interesting while they are still open. Bugs we count either way.
    if priority == "blocker" and not done:
        wk["blockers"] += 1
        p["blockers"] += 1
        proj["blockers"] += 1
        epic["blockers"] += 1
    if issue_type == "bug":
        wk["bugs"] += 1
        p["bugs"] += 1          # per person - the spreadsheet's SUMIFS omitted this
        proj["bugs"] += 1
        epic["bugs"] += 1


# --------------------------------------------------------------------------- output

def write_data_file(payload, path=None):
    """Write sprint_data.js. A <script src> works from file:// as well as the server."""
    path = Path(path or (HERE / "sprint_data.js"))
    body = ("// Generated by jira_pull.py - do not edit by hand.\n"
            "// Pulled from Jira on " + payload["generated_at"] + "\n"
            "window.RETRO_DATA = " + json.dumps(payload, indent=1) + ";\n")
    tmp = path.with_suffix(".js.tmp")
    tmp.write_text(body, encoding="utf-8")
    try:
        os.replace(tmp, path)
    except OSError:
        # Dropbox intermittently denies a replace over an existing file on Windows.
        path.write_text(body, encoding="utf-8")
        try:
            tmp.unlink()
        except OSError:
            pass
    return path


# --------------------------------------------------------------------------- CLI

def _last_complete_monday(today=None):
    today = today or date.today()
    this_monday = today - timedelta(days=today.weekday())
    return this_monday - timedelta(days=7)


def cmd_check():
    cfg = load_config()
    print("Jira site : " + cfg["base_url"])
    print("Jira user : " + cfg["email"])
    print("Token     : " + ("set (" + str(len(cfg["token"])) + " chars)" if cfg["token"] else "MISSING"))
    if not cfg["token"]:
        print("\nNo token. Set JIRA_API_TOKEN, or check " + str(ABACUS_ENV))
        return 1
    jira = Jira(cfg)
    print("cloudId   : " + jira.cloud_id())
    sp = jira.story_point_fields()
    print("Story pts : " + (", ".join(sp) if sp else "NONE FOUND"))

    fields = ",".join(["status", "assignee", "project", "resolutiondate", SPRINT_FIELD] + sp)
    issues = jira.search("resolutiondate >= -30d ORDER BY resolutiondate DESC", fields)
    print("\nResolved in the last 30 days: " + str(len(issues)) + " issues")

    with_points = sum(1 for i in issues if points_of(i.get("fields", {}) or {}, sp) > 0)
    with_assignee = sum(1 for i in issues if (i.get("fields", {}) or {}).get("assignee"))
    print("  with story points : " + str(with_points))
    print("  with an assignee  : " + str(with_assignee))

    committed = jira.search(
        "(sprint in openSprints() OR (sprint in closedSprints() AND updated >= -30d))", fields)

    names = {}
    for i in committed:
        h = home_sprint(i.get("fields", {}) or {})
        if h and h["name"]:
            names[h["name"]] = h["state"]
    print("\nSprints seen (name and state, straight from Jira):")
    for n in sorted(names)[-8:]:
        print("    " + n + "  [" + names[n] + "]")

    projects = {}
    for i in committed + issues:
        code, name, sector = project_of(i.get("key", ""), i.get("fields", {}) or {})
        projects[code] = (name, sector)
    print("\nProjects seen (name straight from Jira, sector derived from the key):")
    for code in sorted(projects):
        name, sector = projects[code]
        print("    " + code.ljust(9) + name.ljust(34) + sector)

    done_no_date = [i for i in committed
                    if is_done(i.get("fields", {}) or {})
                    and not (i.get("fields", {}) or {}).get("resolutiondate")]
    print("\nDone but with no resolution date: " + str(len(done_no_date))
          + " (these use the sprint-week fallback)")
    return 0


def cmd_reconcile(weeks_back=6):
    """Show this tool's numbers next to Abacus's rule, so any gap is explainable."""
    jira = Jira()
    sp = jira.story_point_fields()
    fields = ",".join(["summary", "status", "assignee", "priority", "issuetype", "project",
                       "resolutiondate", SPRINT_FIELD] + sp)

    completed = jira.search(f"resolutiondate >= -{HISTORY_DAYS}d ORDER BY resolutiondate DESC", fields)
    committed = jira.search(
        f"(sprint in openSprints() OR (sprint in closedSprints() AND updated >= -{HISTORY_DAYS}d))",
        fields)

    by_resolution, by_abacus = {}, {}
    for it in completed:
        f = it.get("fields", {}) or {}
        mon = monday_of(f.get("resolutiondate"))
        if mon:
            by_resolution[mon] = by_resolution.get(mon, 0) + points_of(f, sp)
    for it in committed:
        f = it.get("fields", {}) or {}
        if not is_done(f):
            continue
        mon = sprint_monday(f)
        if mon:
            by_abacus[mon] = by_abacus.get(mon, 0) + points_of(f, sp)

    last = _last_complete_monday()
    mondays = [last - timedelta(days=7 * i) for i in range(weeks_back)][::-1]

    print("\nPoints completed per week, two ways of counting")
    print("  A = this dashboard   (Jira resolution date - when it was actually finished)")
    print("  B = Abacus's rule    (current status, attributed to the issue's sprint week)\n")
    print("  Week commencing        A      B    diff")
    print("  " + "-" * 38)
    for mon in mondays:
        a = by_resolution.get(mon, 0)
        b = by_abacus.get(mon, 0)
        print(f"  {mon.isoformat()}  {a:5d}  {b:5d}  {b - a:+5d}")
    print("\n  A never changes once a week has passed. B moves every time somebody")
    print("  reopens or closes an old ticket, because it reads today's status.")
    return 0


def main(argv):
    args = set(a.lower() for a in argv[1:])
    try:
        if "--check" in args:
            return cmd_check()
        if "--reconcile" in args:
            return cmd_reconcile()
        print("Pulling from Jira ...")
        payload = build()
        path = write_data_file(payload)
        c = payload["counts"]
        print(f"  {c['completed_issues']} completed issues, {c['committed_issues']} in sprints")
        print(f"  {c['weeks']} weeks, {len(payload['people'])} people")
        latest = payload.get("latest_sprint")
        if latest:
            print("  latest sprint: " + (", ".join(latest["names"]) or "(unnamed)")
                  + "  w/c " + latest["week_start"])
        if c["excluded_issues"]:
            print(f"  {c['excluded_issues']} issues excluded ("
                  + ", ".join(sorted(EXCLUDE_PROJECTS)) + ")")
        if c["no_resolution_date"]:
            print(f"  {c['no_resolution_date']} done with no resolution date (dated by their move to Done)")
        print(f"Wrote {path.name} in {payload['elapsed_seconds']}s")
        return 0
    except JiraError as e:
        print("Jira error: " + str(e))
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
