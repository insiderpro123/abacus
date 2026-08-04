"""
Read-only Jira Cloud client for the Abacus tracker.

Pulls completed / total story points for a Jira project so the dashboard can
show delivery progress. Read-only: nothing here writes to Jira.

Configuration comes entirely from environment variables (never hardcode the
token - it is a secret):
  JIRA_BASE_URL    site URL, e.g. https://insiderpro.atlassian.net   (default below)
  JIRA_EMAIL       the account the API token belongs to              (default below)
  JIRA_API_TOKEN   an Atlassian API token (READ-ONLY, scoped)        (required; no default)

Auth: HTTP Basic (email:token). NOTE: modern *scoped* API tokens only work
against the platform gateway https://api.atlassian.com/ex/jira/{cloudId}/... -
the site URL (…atlassian.net/rest/…) returns 401 for them. So we discover the
site's cloudId from the public {site}/_edge/tenant_info endpoint and send all
authenticated calls through the gateway.

If JIRA_API_TOKEN is unset the module reports itself as "not configured" and the
app degrades gracefully (no Jira features shown).
"""

import os
import time

import requests

SITE_URL = os.environ.get("JIRA_BASE_URL", "https://insiderpro.atlassian.net").rstrip("/")
EMAIL = os.environ.get("JIRA_EMAIL", "team@insiderpro.co.uk")
API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")

TIMEOUT = 30  # seconds per request
_PAGE = 100   # Jira max page size

# names Jira uses for the story-point field (company- vs team-managed projects)
_SP_FIELD_NAMES = ("story points", "story point estimate")

# discovered once and cached
_cloud_id = None
_sp_field_ids = None

# epic list is cached for a few minutes so the Edit-form dropdown opens instantly
_epics_cache = {"at": 0.0, "data": None}
_EPICS_TTL = 300  # seconds


class JiraError(Exception):
    """Raised for any Jira call that fails, with a human-readable message."""


def is_configured():
    return bool(API_TOKEN)


def _auth():
    return (EMAIL, API_TOKEN)


def _cloudid():
    """Resolve the site's cloudId (public, unauthenticated); cached."""
    global _cloud_id
    if _cloud_id:
        return _cloud_id
    try:
        r = requests.get(f"{SITE_URL}/_edge/tenant_info", timeout=TIMEOUT)
        r.raise_for_status()
        _cloud_id = r.json()["cloudId"]
    except (requests.RequestException, KeyError, ValueError) as e:
        raise JiraError(f"Could not resolve the Jira cloudId from {SITE_URL}: {e}") from e
    return _cloud_id


def _get(path, params=None):
    """Authenticated GET through the api.atlassian.com gateway."""
    if not is_configured():
        raise JiraError("Jira is not configured (JIRA_API_TOKEN is not set).")
    url = f"https://api.atlassian.com/ex/jira/{_cloudid()}{path}"
    try:
        r = requests.get(url, params=params, auth=_auth(),
                         headers={"Accept": "application/json"}, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise JiraError(f"Could not reach Jira: {e}") from e
    if r.status_code == 401:
        raise JiraError("Jira rejected the credentials (401). Check JIRA_EMAIL + JIRA_API_TOKEN.")
    if r.status_code == 403:
        raise JiraError("Jira denied access (403). The token may lack the read:jira-work scope.")
    if r.status_code >= 400:
        raise JiraError(f"Jira returned {r.status_code}: {r.text[:200]}")
    try:
        return r.json()
    except ValueError as e:
        raise JiraError("Jira returned a non-JSON response.") from e


def _post(path, payload):
    """Authenticated POST (write) through the api.atlassian.com gateway.

    Writing needs a token with the write:jira-work scope; the read-only token the
    rest of this module uses will get a 403 here, which we surface plainly."""
    if not is_configured():
        raise JiraError("Jira is not configured (JIRA_API_TOKEN is not set).")
    url = f"https://api.atlassian.com/ex/jira/{_cloudid()}{path}"
    try:
        r = requests.post(url, json=payload, auth=_auth(),
                          headers={"Accept": "application/json",
                                   "Content-Type": "application/json"}, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise JiraError(f"Could not reach Jira: {e}") from e
    if r.status_code == 401:
        raise JiraError("Jira rejected the credentials (401). Check JIRA_EMAIL + JIRA_API_TOKEN.")
    if r.status_code == 403:
        raise JiraError("Jira denied the write (403). The API token is read-only - it needs the "
                        "'write:jira-work' scope to create issues.")
    if r.status_code >= 400:
        raise JiraError(f"Jira returned {r.status_code}: {r.text[:300]}")
    try:
        return r.json()
    except ValueError as e:
        raise JiraError("Jira returned a non-JSON response.") from e


def list_epics():
    """Every epic visible to the token, for the picker:
    [{'key':..., 'project':..., 'summary':...}], ordered by project then key.
    Cached for _EPICS_TTL seconds so the dropdown opens instantly on repeat."""
    now = time.time()
    if _epics_cache["data"] is not None and (now - _epics_cache["at"]) < _EPICS_TTL:
        return _epics_cache["data"]
    epics, next_token = [], None
    while True:
        params = {"jql": "issuetype = Epic ORDER BY project ASC, key ASC",
                  "maxResults": _PAGE, "fields": "summary,project"}
        if next_token:
            params["nextPageToken"] = next_token
        data = _get("/rest/api/3/search/jql", params=params)
        for it in data.get("issues", []):
            f = it.get("fields", {}) or {}
            epics.append({
                "key": it.get("key", ""),
                "project": (f.get("project") or {}).get("key", ""),
                "summary": f.get("summary") or "",
            })
        next_token = data.get("nextPageToken")
        if data.get("isLast") or not next_token:
            break
    _epics_cache.update(at=now, data=epics)
    return epics


def story_points_field_ids():
    """Ids of every story-point field on this site (company- and team-managed
    projects use different ones). Cached after first lookup."""
    global _sp_field_ids
    if _sp_field_ids is not None:
        return _sp_field_ids
    fields = _get("/rest/api/3/field")
    ids = []
    for f in fields:
        if (f.get("name") or "").strip().lower() in _SP_FIELD_NAMES and f.get("id"):
            ids.append(f["id"])
    _sp_field_ids = ids
    return ids


def _num(v):
    """Coerce a story-point value to a number; missing/blank -> 0."""
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def epic_points(epic_key):
    """Sum story points across the issues under an epic (its children).
    Returns {'done': int, 'total': int} where 'done' counts issues whose status
    category is Done. Each issue uses whichever story-point field is populated;
    issues without an estimate contribute 0."""
    sp_fields = story_points_field_ids()
    if not sp_fields:
        raise JiraError("Could not find a 'Story Points' field on this Jira site.")
    jql = f'parent = "{epic_key}"'
    fields_param = ",".join(sp_fields + ["status"])
    done_pts = total_pts = 0.0
    next_token = None
    while True:
        params = {"jql": jql, "maxResults": _PAGE, "fields": fields_param}
        if next_token:
            params["nextPageToken"] = next_token
        data = _get("/rest/api/3/search/jql", params=params)
        for it in data.get("issues", []):
            f = it.get("fields", {}) or {}
            pts = 0.0
            for fid in sp_fields:                 # first populated field wins
                if f.get(fid) is not None:
                    pts = _num(f.get(fid))
                    break
            total_pts += pts
            cat = (((f.get("status") or {}).get("statusCategory") or {}).get("key") or "").lower()
            if cat == "done":
                done_pts += pts
        next_token = data.get("nextPageToken")
        if data.get("isLast") or not next_token:
            break
    return {"done": int(round(done_pts)), "total": int(round(total_pts))}


# --------------------------------------------------------------------------- #
# Write: push Abacus sub-points into a Jira epic's backlog as separate issues
# --------------------------------------------------------------------------- #

# creatable issue-type id per project key, discovered once and cached
_issue_type_cache = {}

# issue types we prefer for pushed sub-points, best first. Story first so pushed
# sub-points get the green Story marker on the board (matching the backlog).
_PREFERRED_TYPES = ("story", "task")


def project_creatable_issue_type(project_key):
    """Id of a creatable, non-subtask issue type for the project - preferring Story,
    then Task, then the first available. Cached per project key."""
    key = (project_key or "").strip()
    if not key:
        raise JiraError("No Jira project key given.")
    if key in _issue_type_cache:
        return _issue_type_cache[key]
    data = _get(f"/rest/api/3/issue/createmeta/{key}/issuetypes")
    types = [t for t in data.get("issueTypes", []) if not t.get("subtask")]
    if not types:
        raise JiraError(f"No creatable issue types found for project {key}.")
    chosen = None
    for want in _PREFERRED_TYPES:
        chosen = next((t for t in types if (t.get("name") or "").strip().lower() == want), None)
        if chosen:
            break
    chosen = chosen or types[0]
    _issue_type_cache[key] = chosen["id"]
    return chosen["id"]


def epic_child_summaries(epic_key):
    """Set of the summaries of every issue already under an epic (its children).
    Used to skip sub-points that have already been pushed (idempotent re-push)."""
    summaries = set()
    jql = f'parent = "{epic_key}"'
    next_token = None
    while True:
        params = {"jql": jql, "maxResults": _PAGE, "fields": "summary"}
        if next_token:
            params["nextPageToken"] = next_token
        data = _get("/rest/api/3/search/jql", params=params)
        for it in data.get("issues", []):
            summ = ((it.get("fields") or {}).get("summary") or "").strip()
            if summ:
                summaries.add(summ)
        next_token = data.get("nextPageToken")
        if data.get("isLast") or not next_token:
            break
    return summaries


def resolve_backlog_url(project_key):
    """Work out the deep link to the project's Backlog board (the screen the user
    sees in Jira, e.g. .../projects/MON/boards/121/backlog). Returns (url, error):
    on success (url, None); on failure (None, reason). Finding the board needs a
    token that can READ JIRA SOFTWARE BOARDS - a plain read/write:jira-work token
    can't, which is the usual reason this falls back."""
    key = (project_key or "").strip()
    if not key:
        return (None, "no project key on the work package")
    try:
        data = _get("/rest/agile/1.0/board", params={"projectKeyOrId": key, "maxResults": 50})
    except JiraError as e:
        return (None, f"can't read Jira boards for {key} ({e}). The API token likely "
                      f"lacks Jira Software (board) read access.")
    boards = data.get("values", []) or []
    # a scrum board has the Backlog screen; otherwise take the first board
    board = next((b for b in boards if (b.get("type") or "") == "scrum"), boards[0] if boards else None)
    if not board or not board.get("id"):
        return (None, f"no board is linked to project {key}")
    # classic (company-managed) boards live under /jira/software/c/…,
    # team-managed (next-gen) under /jira/software/… (no 'c/')
    style = ""
    try:
        style = (_get(f"/rest/api/3/project/{key}").get("style") or "").lower()
    except JiraError:
        pass
    seg = "" if style == "next-gen" else "c/"
    return (f"{SITE_URL}/jira/software/{seg}projects/{key}/boards/{board['id']}/backlog", None)


_board_url_cache = {}


def project_backlog_url(project_key):
    """URL-only wrapper around resolve_backlog_url(), cached per project key.
    Returns None when the board can't be resolved."""
    key = (project_key or "").strip()
    if key in _board_url_cache:
        return _board_url_cache[key]
    url, _err = resolve_backlog_url(key)
    _board_url_cache[key] = url
    return url


def create_backlog_issue(project_key, epic_key, summary, issue_type_id):
    """Create a single issue under an epic. With no sprint set it lands in the
    project's backlog. Returns the new issue key (e.g. 'MON-812')."""
    payload = {"fields": {
        "project": {"key": project_key},
        "summary": summary,
        "issuetype": {"id": issue_type_id},
        "parent": {"key": epic_key},
    }}
    data = _post("/rest/api/3/issue", payload)
    return data.get("key", "")
