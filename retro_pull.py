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
    The same numbers Jira's own sprint view shows (Sam's call, 29 Sep 2026). A week is the
    sprints that ran that week, and for each sprint:

      scheduled = every SCRUM-labelled ticket that was in the sprint when it ended, i.e. at
                  any moment between its end date and the moment somebody pressed Complete
      completed = those of them that were Done by the time it was completed

    Sprints here really run Monday evening to Monday evening, and last week's tickets are
    usually closed on the Monday morning, so a ticket closed then counts in the sprint it
    belonged to, not the new week. Unfinished tickets are often moved to the next sprint by
    hand just before Complete is pressed, which wipes the old sprint off the ticket, so the
    membership is rebuilt from the Sprint changelog rather than read off the sprint field.
    Points are taken as they stood when the sprint was completed, so re-pointing a ticket
    afterwards does not change a finished sprint. Past weeks therefore never change.

    Only tickets labelled SCRUM count (REQUIRE_LABEL), because that is the view the team
    reads its sprint numbers from (the SCRUM CALL board filters on it). A ticket that lost
    its label is left out, exactly as it is in that view.

    A ticket Done in one sprint and then dragged into a later one only counts once, in the
    sprint it was finished in. Tickets resolved outside any sprint are not counted at all.

    Sprints not started yet are included too, from this week up to FUTURE_WEEKS ahead, so
    the page can open this week's sprint before Start is pressed on Monday evening and goals
    can be written into the next ones. Like an active sprint they hold what is in them now.
    Jira only finds their tickets through futureSprints() (openSprints() is the active ones
    only), and the ISP ones carry no dates, so their week is read off the name instead.

    Abacus does it differently again: it reads each issue's CURRENT status and attributes it
    to the issue's latest sprint. Run --reconcile to compare.

CREDENTIALS
    On Render: the JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN environment variables Abacus
    already uses. Locally: this folder's .env (app.py loads it into the environment first).
"""

import json
import os
import re
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

# Sprints not started yet, counted in weeks after this one: this week plus two (Sam, 5 Oct
# 2026). Further ahead they are left out, though Jira holds ISP sprints well into next year.
FUTURE_WEEKS = 2

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

# Only tickets carrying this label count, as on the SCRUM CALL board the team reads its
# sprint numbers from. Set to None to count every ticket in a sprint.
REQUIRE_LABEL = "SCRUM"

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

    def post(self, path, body):
        """A read-only POST (bulk fetches take their arguments in the body)."""
        if not self.configured:
            raise JiraError("Jira is not configured - JIRA_API_TOKEN is not set.")
        url = f"https://api.atlassian.com/ex/jira/{self.cloud_id()}{path}"
        try:
            r = self.session.post(url, json=body, auth=(self.email, self.token),
                                  headers={"Accept": "application/json"}, timeout=TIMEOUT)
        except requests.RequestException as e:
            raise JiraError(f"Could not reach Jira: {e}") from e
        if r.status_code >= 400:
            raise JiraError(f"Jira returned {r.status_code}: {r.text[:200]}")
        try:
            return r.json()
        except ValueError as e:
            raise JiraError("Jira returned a non-JSON response.") from e

    def changelogs(self, issue_ids, field_ids):
        """{issue id: [(when, field id, from, to, fromString, toString)]}, oldest first."""
        out = {}
        for i in range(0, len(issue_ids), 1000):
            token = None
            while True:
                body = {"issueIdsOrKeys": issue_ids[i:i + 1000], "fieldIds": field_ids,
                        "maxResults": 10000}
                if token:
                    body["nextPageToken"] = token
                data = self.post("/rest/api/3/changelog/bulkfetch", body)
                for log in data.get("issueChangeLogs", []):
                    rows = out.setdefault(str(log.get("issueId")), [])
                    for h in log.get("changeHistories", []):
                        when = parse_ts(h.get("created"))
                        if not when:
                            continue
                        for item in h.get("items", []):
                            fid = item.get("fieldId") or (
                                SPRINT_FIELD if (item.get("field") or "").lower() == "sprint" else None)
                            rows.append((when, fid, item.get("from"), item.get("to"),
                                         item.get("fromString"), item.get("toString")))
                token = data.get("nextPageToken")
                if not token:
                    break
        for rows in out.values():
            rows.sort(key=lambda r: r[0])
        return out

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

    def future_sprints(self, board_id):
        """Every not-yet-started sprint on a board, empty ones included (JQL cannot see those)."""
        sprints, start = [], 0
        while True:
            data = self.get(f"/rest/agile/1.0/board/{board_id}/sprint",
                            params={"state": "future", "startAt": start, "maxResults": 50})
            page = data.get("values", [])
            sprints += page
            start += len(page)
            if data.get("isLast", True) or not page:
                break
        return sprints


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


def parse_ts(value):
    """Jira timestamp ('2026-09-28T19:09:27.801Z', '...+0100', or epoch ms) -> aware datetime."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) or str(value).isdigit():
        return datetime.fromtimestamp(int(value) / 1000, timezone.utc)
    s = str(value).replace("Z", "+00:00")
    if len(s) > 5 and s[-5] in "+-" and s[-3] != ":":
        s = s[:-2] + ":" + s[-2:]          # +0100 -> +01:00 for fromisoformat
    try:
        d = datetime.fromisoformat(s)
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def has_required_label(fields):
    return REQUIRE_LABEL is None or REQUIRE_LABEL in (fields.get("labels") or [])


def sprint_week(sp):
    """The Monday of the week a sprint belongs to: the week holding the middle of the sprint.

    Sprints start on a Monday evening, so the start date alone would do today, but a sprint
    started early on a Friday would otherwise land in the week before.
    """
    start, end = parse_ts(sp.get("startDate")), parse_ts(sp.get("endDate"))
    if start and end and end > start:
        return monday_of((start + (end - start) / 2).date())
    return (monday_of(sp.get("startDate") or sp.get("endDate") or sp.get("completeDate"))
            or name_week(sp.get("name")))


_MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
_NAME_DATE = re.compile(r"(\d{1,2})\s*([A-Za-z]{3,9})\.?\s*(\d{4}|\d{2})(?!\d)")


def name_week(name):
    """The Monday of the week a sprint's NAME gives, for sprints Jira holds no dates for.

    Future ISP sprints are created in bulk with no dates, so 'ISP Sprint 28: 5-9 Oct 26'
    only says when it runs in its name. The last 'day month year' in it is the end date,
    which covers every format in use: '05 - 09 Oct26', '05Oct26 - 09Oct26',
    '28 Sept-2 Oct 26', '28 Dec-1 Jan 27'. No date in the name gives None.
    """
    for day, month, year in reversed(_NAME_DATE.findall(name or "")):
        mo = _MONTHS.get(month[:3].lower())
        if not mo:
            continue
        y = int(year) + (2000 if len(year) == 2 else 0)
        try:
            return monday_of(date(y, mo, int(day)))
        except ValueError:
            continue
    return None


def _sprint_ids(raw):
    return {s.strip() for s in str(raw or "").split(",") if s.strip()}


def in_sprint_between(fields, changes, sprint_id, a, b):
    """Was the ticket in the sprint at any moment from a to b?

    The sprint field only says where a ticket is now. Walking the Sprint changelog gives each
    stretch of time and the sprints the ticket sat in during it.
    """
    sid = str(sprint_id)
    moves = [c for c in changes if c[1] == SPRINT_FIELD]
    now = {str(s.get("id")) for s in (fields.get(SPRINT_FIELD) or []) if isinstance(s, dict)}
    since = None                                     # start of the current stretch
    for when, _, frm, _, _, _ in moves:
        if sid in _sprint_ids(frm) and (since is None or since <= b) and when >= a:
            return True
        since = when
    return sid in now and (since is None or since <= b)


def points_at(fields, changes, sp_fields, when):
    """Story points as they stood at `when`: today's value with later edits undone."""
    if when is None:
        return points_of(fields, sp_fields)
    past = dict(fields)
    for at, fid, _, _, frm, _ in reversed(changes):
        if at <= when:
            break
        if fid in sp_fields:
            past[fid] = frm if frm not in ("", None) else None
    return points_of(past, sp_fields)


def finished_at(fields):
    """When the ticket was finished: its resolution date, else its move to Done."""
    return parse_ts(fields.get("resolutiondate") or fields.get("statuscategorychangedate"))


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
        ["summary", "status", "assignee", "priority", "issuetype", "project", "labels",
         "parent", "resolutiondate", "statuscategorychangedate", SPRINT_FIELD] + sp_fields
    )

    # Every ticket that has sat in a recent sprint. The proven query from Abacus's jira_sync.py,
    # plus futureSprints(), which openSprints() does not cover on this site.
    # A ticket moved on to a later sprint is still found, through the sprint it is in now.
    in_sprints = jira.search(
        f"(sprint in openSprints() OR sprint in futureSprints()"
        f" OR (sprint in closedSprints() AND updated >= -{history_days}d))",
        field_list)

    dropped = len(in_sprints)
    in_sprints = [i for i in in_sprints
                  if not is_excluded(i.get("key", ""), i.get("fields") or {})
                  and has_required_label(i.get("fields") or {})]
    dropped -= len(in_sprints)

    # Where each ticket has been, and what it was pointed at, over time.
    history = jira.changelogs([str(i.get("id")) for i in in_sprints if i.get("id")],
                              [SPRINT_FIELD] + sp_fields)

    # Every sprint any of them has been in, straight off the sprint field.
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=history_days)).date()
    this_monday = monday_of(now.date())
    horizon = this_monday + timedelta(weeks=FUTURE_WEEKS)
    all_sprints = {}
    boards = set()
    for it in in_sprints:
        for s in ((it.get("fields") or {}).get(SPRINT_FIELD) or []):
            if isinstance(s, dict) and s.get("id") is not None:
                all_sprints[str(s["id"])] = s
                if (s.get("state") or "").lower() in ("active", "future") and s.get("boardId"):
                    boards.add(s["boardId"])
    # A sprint nobody has put a ticket in yet is on no ticket, so ask the boards in use for
    # their future sprints too, or next week could have no name. Nice to have, not needed:
    # if Jira refuses, the sprints found through tickets still stand.
    for board in sorted(boards):
        try:
            for s in jira.future_sprints(board):
                if s.get("id") is not None:
                    all_sprints.setdefault(str(s["id"]), s)
        except JiraError:
            pass
    runs = []
    for s in all_sprints.values():
        state = (s.get("state") or "").lower()
        mon = sprint_week(s)
        if state not in ("closed", "active", "future") or not mon or mon < cutoff:
            continue
        if state == "future" and not (this_monday <= mon <= horizon):
            continue
        completed_at = parse_ts(s.get("completeDate")) if state == "closed" else None
        if state == "closed" and not completed_at:
            continue
        ended = parse_ts(s.get("endDate")) or completed_at
        # A closed sprint holds what was in it from its end date to Complete; an active or
        # future one holds what is in it now.
        a, b = (min(ended, completed_at), completed_at) if completed_at else (now, now)
        runs.append({"id": str(s["id"]), "name": (s.get("name") or "").strip(), "state": state,
                     "monday": mon, "a": a, "b": b, "completed_at": completed_at})
    runs.sort(key=lambda r: (r["b"], r["monday"], r["name"]))

    weeks = {}
    seen_people = {}
    sprints = {}          # monday iso -> {name: state} as Jira reports them

    def week_for(monday):
        if monday.isoformat() not in weeks:
            weeks[monday.isoformat()] = _blank_week(monday)
        return weeks[monday.isoformat()]

    # Oldest sprint first, so a ticket Done in one sprint and dragged into the next is only
    # ever counted as completed in the sprint it was finished in.
    counted = set()
    for run in runs:
        wk = week_for(run["monday"])
        sprints.setdefault(run["monday"].isoformat(), {})[run["name"]] = run["state"]
        for it in in_sprints:
            key = it.get("key", "")
            f = it.get("fields", {}) or {}
            changes = history.get(str(it.get("id")), [])
            if key in counted or not in_sprint_between(f, changes, run["id"], run["a"], run["b"]):
                continue
            pts = points_at(f, changes, sp_fields, run["completed_at"])
            fin = finished_at(f)
            done = is_done(f) and (run["completed_at"] is None
                                   or (fin is not None and fin <= run["completed_at"]))
            _tally_committed(wk, key, f, pts, done, seen_people)
            if done:
                counted.add(key)
                if not f.get("resolutiondate"):
                    wk["no_resolution_date"] += 1
                _tally_done(wk, key, f, pts, seen_people)

    # This week and the FUTURE_WEEKS after it can always be opened, sprint or not, so there is
    # somewhere to write their goals.
    for n in range(FUTURE_WEEKS + 1):
        week_for(this_monday + timedelta(weeks=n))

    ordered = [weeks[k] for k in sorted(weeks)]
    for wk in ordered:
        names = sprints.get(wk["start"], {})
        wk["sprint_names"] = sorted(names)
        wk["sprint_active"] = any(s == "active" for s in names.values())
        # "future" = nothing that week has been started yet. A week with no sprint at all is
        # future when it lies ahead and closed when it has gone.
        states = set(names.values())
        if "active" in states:
            wk["sprint_state"] = "active"
        elif states == {"future"} or (not states and wk["start"] >= this_monday.isoformat()):
            wk["sprint_state"] = "future"
        else:
            wk["sprint_state"] = "closed"
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
        "basis": "sprint",
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
            "completed_issues": len(counted),
            "committed_issues": len(in_sprints),
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


def _tally_done(wk, key, f, pts, seen_people):
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


def _tally_committed(wk, key, f, pts, done, seen_people):
    code, name, sector = project_of(key, f)
    acct, person_name = assignee_of(f)
    seen_people[acct] = person_name
    priority = ((f.get("priority") or {}).get("name") or "").strip().lower()
    issue_type = ((f.get("issuetype") or {}).get("name") or "").strip().lower()

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
    """This tool's sprint numbers per sector, next to the old finished-that-week count."""
    jira = Jira()
    payload = build(jira)
    sp = jira.story_point_fields()
    fields = ",".join(["project", "labels", "resolutiondate"] + sp)
    by_resolution = {}
    for it in jira.search(f"resolutiondate >= -{HISTORY_DAYS}d", fields):
        f = it.get("fields", {}) or {}
        if is_excluded(it.get("key", ""), f) or not has_required_label(f):
            continue
        mon = monday_of(f.get("resolutiondate"))
        if mon:
            slot = by_resolution.setdefault(mon.isoformat(), {})
            sector = project_of(it.get("key", ""), f)[2]
            slot[sector] = slot.get(sector, 0) + points_of(f, sp)

    print("\nPoints completed of points in the sprint, per sector (as Jira's sprint view)")
    print("  [n] = points resolved inside that Monday-Friday week instead (the old rule)\n")
    for wk in payload["weeks"][-weeks_back:]:
        old = by_resolution.get(wk["start"], {})
        cells = []
        for s in THE_SECTORS:
            c = wk["by_category"][s]
            cells.append(f"{s}: {c['done']} of {c['committed']} [{old.get(s, 0)}]")
        print(f"  {wk['label']:18} " + "   ".join(cells))
        print(f"  {'':18} sprints: " + ", ".join(wk["sprint_names"]))
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
