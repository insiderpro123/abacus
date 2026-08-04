"""
Abacus Work Package Tracker - FULL SYSTEM (test copy).

A standalone copy of the Abacus tracker that adds, on top of the RAG matrix:
  * a simple two-box task board per work package (To Do / In Progress -> Complete);
  * per-work-package Confluence and Dropbox links;
  * a global "Jamie meetings" link in the header.

Reads/writes a SQL database (SQLite locally, PostgreSQL on Render - see models.py)
behind a single shared password.

Local:  python app.py            (opens a browser at http://127.0.0.1:5070)
Prod:   gunicorn app:app         (Render sets DATABASE_URL, SECRET_KEY, APP_PASSWORD)
"""

import hmac
import os
import re
import secrets
from datetime import datetime

from flask import (
    Flask, jsonify, render_template, request, redirect, url_for, session,
)
from werkzeug.security import generate_password_hash, check_password_hash


def _load_local_env():
    """Load KEY=VALUE lines from a local .env file (if present) into the
    environment, without overriding anything already set. Keeps secrets
    (Jira / Jamie tokens, APP_PASSWORD, SECRET_KEY) out of source and the
    launch scripts. Must run before the modules below read their config."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key, val = key.strip(), val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
    except FileNotFoundError:
        pass


_load_local_env()

from models import (
    init_db, SessionLocal,
    Process, Subprocess, WorkPackage, WpStatus, WpFinished, WpTask, Customer, StaffUser,
    Sprint, SprintHistory,
)
import jira_client
import jamie_client
import jira_sync

app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

# Shared team password (set APP_PASSWORD in production; default is for local dev only)
APP_PASSWORD = os.environ.get("APP_PASSWORD", "abacus")

# Local Dropbox "Clients" root. The app lives at <Clients>/ISP Project Management
# Documents/full system test server, so the Clients root is two levels up - derived
# automatically so it resolves on any colleague's machine (override with DROPBOX_ROOT).
_APP_DIR = os.path.dirname(os.path.abspath(__file__))
DROPBOX_ROOT = os.environ.get("DROPBOX_ROOT") or os.path.dirname(os.path.dirname(_APP_DIR))

# Link to the Jamie meeting dashboard (a separate local Flask app, default port 5057)
JAMIE_URL = os.environ.get("JAMIE_URL", "http://127.0.0.1:5057")


@app.template_global()
def static_v(filename):
    """Static URL with a cache-busting ?v=<mtime> so browsers always fetch the
    current file (busts on every change/deploy)."""
    try:
        ver = int(os.path.getmtime(os.path.join(app.static_folder, filename)))
    except OSError:
        ver = 0
    return url_for("static", filename=filename) + f"?v={ver}"


init_db()  # create tables if missing (safe to call every start)


def _next_sub_num(s, parent_id):
    """Lowest positive number not already used by a sibling sub-work-package."""
    used = {wp.sub_num for wp in s.query(WorkPackage)
            .filter(WorkPackage.parent_id == parent_id).all() if wp.sub_num}
    n = 1
    while n in used:
        n += 1
    return n


def _backfill_sub_nums():
    """Give any existing sub-work-package that predates numbering a number."""
    with SessionLocal.begin() as s:
        kids = (s.query(WorkPackage).filter(WorkPackage.parent_id.isnot(None))
                .order_by(WorkPackage.id).all())
        by_parent = {}
        for k in kids:
            by_parent.setdefault(k.parent_id, []).append(k)
        for group in by_parent.values():
            used = {k.sub_num for k in group if k.sub_num}
            n = 1
            for k in group:
                if not k.sub_num:
                    while n in used:
                        n += 1
                    k.sub_num = n
                    used.add(n)


_backfill_sub_nums()


def _backfill_task_points():
    """Ensure every task has at least 1 point (no blank/'-' tasks)."""
    from models import WpTask as _WpTask
    with SessionLocal.begin() as s:
        for t in s.query(_WpTask).filter((_WpTask.points == 0) | (_WpTask.points.is_(None))).all():
            t.points = 1


_backfill_task_points()
# (Outcome wording is now edited via /admin/settings; the one-off patch_outcomes.py
#  remains as a manual tool. No startup force here - it would overwrite edits.)

# --------------------------------------------------------------------------- #
# Status helpers (unchanged meaning from the Excel version)
# --------------------------------------------------------------------------- #
SCORE = {"done": 1.0, "progress": 0.5, "todo": 0.0}
ALLOWED_STATUSES = ("Active", "Inactive", "Removed", "Complete", "On hold")
# Top-level project category. 'Customer' keeps the full 12-step Abacus tracker;
# 'Marketing'/'Process and Ops' have no Abacus link (sub-WPs + tasks only).
ALLOWED_CATEGORIES = ("Customer", "Marketing", "Process and Ops")
GUIDANCE_FIELDS = ("operational", "top_level", "assets", "comment")

_last_saved = {"when": None, "ok": None, "message": ""}


def _clean(v):
    return "" if v is None else str(v).strip()


def status_of(value):
    s = _clean(value)
    if s == "3":
        return "done"
    if s == "2":
        return "progress"
    if s == "1":
        return "todo"
    if s.upper() in ("N/R", "NR", "N/A", "NOT REQUIRED"):
        return "nr"
    return "na"


def _norm_value(value):
    """Front-end write value -> stored value, or '' meaning clear."""
    v = _clean(value)
    if v.upper() in ("N/R", "NR"):
        return "N/R"
    return v  # '', '1', '2', '3'


def _mark_saved():
    _last_saved.update(when=datetime.now().strftime("%H:%M:%S"), ok=True, message="Saved")


# Project field rules
MAX_NAME_LEN = 40          # short project title
MAX_DESC_WORDS = 60        # detailed description word cap


def _valid_icon(icon):
    """Require a real emoji: non-empty, short, and containing a non-ASCII char
    (so plain text like 'abc' is rejected)."""
    icon = _clean(icon)
    return bool(icon) and len(icon) <= 8 and re.search(r"[^\x00-\x7F]", icon) is not None


def _word_count(text):
    return len(_clean(text).split())


def _norm_url(value):
    """Keep only sane http(s) links; anything else becomes '' (cleared)."""
    v = _clean(value)
    if not v:
        return ""
    return v if re.match(r"^https?://", v, re.I) else ""


def _project_errors(client, name, icon, description, require_icon=True):
    """Return an error string for the add/edit project form, or '' if valid.
    Sub-work-packages don't carry an icon, so require_icon is False for them."""
    if not client or not name:
        return "Customer and project name are required."
    if len(name) > MAX_NAME_LEN:
        return f"Project name is too long (max {MAX_NAME_LEN} characters)."
    if require_icon and not _valid_icon(icon):
        return "Please choose an emoji icon (a picture, not text)."
    if _word_count(description) > MAX_DESC_WORDS:
        return f"Description is too long (max {MAX_DESC_WORDS} words)."
    return ""


def _dup_exists(s, client, name, exclude_id=None, parent_id=None):
    """True if another work package under the same parent already has this
    client + name (case-insensitive). Sub-work-packages are only checked against
    their siblings, so the same name can repeat under different parents."""
    c, n = client.strip().lower(), name.strip().lower()
    pid = parent_id or None
    for wp in s.query(WorkPackage).all():
        if exclude_id is not None and wp.id == exclude_id:
            continue
        if (wp.parent_id or None) != pid:
            continue
        if ((wp.client or "").strip().lower() == c
                and (wp.name or "").strip().lower() == n):
            return True
    return False


# --------------------------------------------------------------------------- #
# Authentication - three roles behind one login page:
#   * admin    - blank email + shared APP_PASSWORD; full access incl. "full settings"
#                (editing the 12 steps and managing ISP/customer logins)
#   * staff    - ISP staff email + password; everything operational EXCEPT the
#                admin-only settings below
#   * customer - email + per-customer password; read-only Jamie portal only
# Both admin and ISP staff have session["authed"]; an admin is an authed session
# WITHOUT a staff_id (shared password), so old admin sessions stay admin.
# --------------------------------------------------------------------------- #
def _is_staff():
    return bool(session.get("authed"))


def _is_admin():
    return bool(session.get("authed")) and not session.get("staff_id")


def _is_customer():
    return session.get("role") == "customer"


# Endpoints only a full admin may call (ISP staff are blocked with 403)
ADMIN_ONLY_PREFIXES = ("/api/admin/process/", "/api/admin/subprocess/", "/api/admin/staff/")


@app.before_request
def _require_login():
    p = request.path
    if p == "/login" or p == "/logout" or p.startswith("/static/"):
        return
    if _is_staff():
        if not _is_admin() and any(p.startswith(pref) for pref in ADMIN_ONLY_PREFIXES):
            return jsonify({"error": "Admin only."}), 403
        return                                  # staff: full operational access
    if _is_customer():
        # customers may reach only their portal and its read-only API
        if p == "/portal" or p.startswith("/api/portal/"):
            return
        if p.startswith("/api/"):
            return jsonify({"error": "Not allowed"}), 403
        return redirect(url_for("portal"))
    # not logged in
    if p.startswith("/api/"):
        return jsonify({"error": "Not authenticated"}), 401
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = _clean(request.form.get("email"))
        password = request.form.get("password", "")
        if email:
            with SessionLocal() as s:
                # ISP staff login (email + password) - operational access, not admin
                staff = s.query(StaffUser).filter(StaffUser.email.ilike(email)).first()
                if staff and check_password_hash(staff.password_hash, password):
                    session.clear()
                    session["authed"] = True
                    session["role"] = "staff"
                    session["staff_id"] = staff.id
                    session.permanent = True
                    return redirect(url_for("index"))
                # customer login (email + password) - read-only portal
                cust = s.query(Customer).filter(Customer.email.ilike(email)).first()
                if cust and check_password_hash(cust.password_hash, password):
                    session.clear()
                    session["role"] = "customer"
                    session["customer_id"] = cust.id
                    session.permanent = True
                    return redirect(url_for("portal"))
            return render_template("login.html", error="Incorrect email or password"), 401
        # admin login (shared team password, no email) - full access
        if hmac.compare_digest(password, APP_PASSWORD):
            session.clear()
            session["authed"] = True
            session["role"] = "admin"
            session.permanent = True
            return redirect(url_for("index"))
        return render_template("login.html", error="Incorrect password"), 401
    if _is_staff():
        return redirect(url_for("index"))
    if _is_customer():
        return redirect(url_for("portal"))
    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


def _current_customer(s):
    """Return the logged-in Customer row (within session s), or None."""
    cid = session.get("customer_id")
    return s.get(Customer, cid) if cid else None


# --------------------------------------------------------------------------- #
# Data assembly
# --------------------------------------------------------------------------- #
def _is_locked(s, wp_id):
    wp = s.get(WorkPackage, int(wp_id)) if str(wp_id).isdigit() else None
    return (wp.status if wp else "Active").strip().lower() != "active"


def get_data():
    with SessionLocal() as s:
        processes = s.query(Process).order_by(Process.num).all()
        subs = s.query(Subprocess).order_by(Subprocess.process_num, Subprocess.seq).all()
        wps = s.query(WorkPackage).order_by(WorkPackage.id).all()
        statuses = s.query(WpStatus).all()
        finished = s.query(WpFinished).all()
        tasks = s.query(WpTask).all()

        # per-process ordered sub list, and the shared reference map
        subs_by_process, reference = {}, {}
        for sp in subs:
            label = sp.outcomes.strip() if (sp.outcomes or "").strip() else (sp.question or "")
            subs_by_process.setdefault(sp.process_num, []).append(
                {"code": sp.code, "label": label}
            )
            reference[sp.code] = {
                "outcomes": sp.outcomes or "", "operational": sp.operational or "",
                "top_level": sp.top_level or "", "assets": sp.assets or "",
                "comment": sp.comment or "",
            }

        vals = {}          # wp_id -> {code: value}
        for st in statuses:
            vals.setdefault(st.wp_id, {})[st.code] = st.value
        fin = {}           # wp_id -> set(process_num)
        for f in finished:
            fin.setdefault(f.wp_id, set()).add(f.process_num)
        task_counts = {}   # wp_id -> {"total": n, "done": n}
        task_assignees = {}   # wp_id -> set(assignee names)
        for t in tasks:
            tc = task_counts.setdefault(t.wp_id, {"total": 0, "done": 0})
            tc["total"] += 1
            if t.status == "done":
                tc["done"] += 1
            if (t.assignee or "").strip():
                task_assignees.setdefault(t.wp_id, set()).add(t.assignee.strip())

        # live counters for the ACTIVE sprint, per category (for the summary strip)
        active_sprint = s.query(Sprint).filter_by(status="active").first()
        active_sprint_id = active_sprint.id if active_sprint else None
        cat_by_wp = {wp.id: (wp.category or "Customer") for wp in wps}
        sprint_summary = {c: {"todo": 0, "progress": 0, "done": 0, "planned": 0, "done_pts": 0}
                          for c in ("Customer", "Marketing", "Process and Ops")}
        for t in tasks:
            if t.sprint_id not in (active_sprint_id, None):
                continue                       # only tasks in the active sprint
            b = sprint_summary.get(cat_by_wp.get(t.wp_id, "Customer"))
            if not b:
                continue
            if t.status in ("todo", "progress", "done"):
                b[t.status] += 1
            b["planned"] += (t.points or 0)
            if t.status == "done":
                b["done_pts"] += (t.points or 0)

        process_headers = [{"num": p.num, "title": p.title} for p in processes]

        work_packages = []
        for wp in wps:
            wpvals = vals.get(wp.id, {})
            wpfin = fin.get(wp.id, set())
            phases, group_pcts = [], []
            for p in processes:
                subs_out, answered, nr, score_sum, todo = [], 0, 0, 0.0, 0
                for sub in subs_by_process.get(p.num, []):
                    raw = wpvals.get(sub["code"], "")
                    st = status_of(raw)
                    if st in SCORE:
                        answered += 1
                        score_sum += SCORE[st]
                        if st == "todo":
                            todo += 1
                    elif st == "nr":
                        nr += 1
                    subs_out.append({"code": sub["code"], "label": sub["label"],
                                     "status": st, "raw": raw})
                is_fin = p.num in wpfin
                total = len(subs_out)
                effective = total - nr
                # a phase whose every sub-point is "Not required" is crossed out
                # (shown as N/R), not 100%, and does not count toward the overall.
                all_nr = (not is_fin) and nr > 0 and effective <= 0
                if is_fin:
                    pct = 100.0
                elif effective <= 0:
                    pct = 0.0
                else:
                    pct = round(score_sum / effective * 100)
                if all_nr:
                    pstatus = "nr"
                elif pct >= 100:
                    pstatus = "done"
                elif todo > 0:
                    # any outstanding sub-point flags the whole phase red
                    pstatus = "todo"
                elif answered == 0 and nr == 0 and not is_fin:
                    pstatus = "na"
                else:
                    pstatus = "progress"
                if not all_nr:
                    group_pcts.append(pct)
                phases.append({"num": p.num, "title": p.title, "pct": pct,
                               "status": pstatus, "finished": is_fin, "subs": subs_out})

            tc = task_counts.get(wp.id, {"total": 0, "done": 0})
            overall = round(sum(group_pcts) / len(group_pcts)) if group_pcts else 0
            # Non-Customer work packages have no 12-step data; base their overall on
            # task completion instead. (Top-level non-Customer projects hold no tasks
            # of their own - their overall is rolled up from children below.)
            if (wp.category or "Customer") != "Customer":
                overall = round(tc["done"] / tc["total"] * 100) if tc["total"] else 0
            work_packages.append({
                "wp_id": str(wp.id), "client": wp.client, "name": wp.name,
                "description": wp.description or "",
                "category": wp.category or "Customer",
                "status": wp.status or "Unknown", "points": wp.points or "",
                "icon": wp.icon or "", "overall": overall, "phases": phases,
                "jira_project_key": wp.jira_project_key or "",
                "jira_done": wp.jira_done or 0, "jira_total": wp.jira_total or 0,
                "jira_synced_at": wp.jira_synced_at.strftime("%d %b %H:%M") if wp.jira_synced_at else "",
                "confluence_url": wp.confluence_url or "", "dropbox_url": wp.dropbox_url or "",
                "jamie_tag": wp.jamie_tag or "",
                "parent_id": (str(wp.parent_id) if wp.parent_id else None),
                "sub_num": wp.sub_num or None,
                "children": [],
                "task_total": tc["total"], "task_done": tc["done"],
                "assignees": sorted(task_assignees.get(wp.id, set())),
            })

    # Nest sub-work-packages under their parent; return only top-level projects.
    by_id = {w["wp_id"]: w for w in work_packages}
    top_level = []
    for w in work_packages:
        parent = by_id.get(w["parent_id"]) if w["parent_id"] else None
        (parent["children"] if parent else top_level).append(w)

    # Non-Customer projects hold their tasks on the sub-work-packages, not on the
    # project itself. Roll those child task counts up so the project's task chip
    # and overall % are meaningful.
    for w in top_level:
        if (w.get("category") or "Customer") != "Customer" and w["children"]:
            total = sum(c["task_total"] for c in w["children"])
            done = sum(c["task_done"] for c in w["children"])
            w["task_total"], w["task_done"] = total, done
            w["overall"] = round(done / total * 100) if total else 0
        # a project's assignees = its own tasks' + all its sub-work-packages' (any category),
        # so the top-level "who's on this" chips and the name filter see everyone involved
        if w["children"]:
            merged = set(w["assignees"])
            for c in w["children"]:
                merged.update(c["assignees"])
            w["assignees"] = sorted(merged)

    assignees_all = sorted({a for w in work_packages for a in w["assignees"]})

    return {
        "generated": datetime.now().strftime("%d %b %Y, %H:%M"),
        "processes": process_headers,
        "work_packages": top_level,
        "assignees_all": assignees_all,
        "sprint_summary": sprint_summary,
        "active_sprint_id": (str(active_sprint_id) if active_sprint_id else None),
        "reference": reference,
        "pending": 0,
        "last_flush": dict(_last_saved),
        "jira_configured": jira_client.is_configured(),
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template("index.html", jamie_url=JAMIE_URL)


# --------------------------------------------------------------------------- #
# Customer portal (read-only, Jamie meeting notes only)
# --------------------------------------------------------------------------- #
@app.route("/portal")
def portal():
    with SessionLocal() as s:
        cust = _current_customer(s)
        name = (cust.client or cust.email) if cust else "your project"
        tag = (cust.jamie_tag if cust else "") or ""
    return render_template("portal.html", customer_name=name, jamie_tag=tag)


@app.route("/api/portal/meetings")
def api_portal_meetings():
    with SessionLocal() as s:
        cust = _current_customer(s)
        tag = cust.jamie_tag if cust else ""
    if not tag:
        return jsonify({"meetings": [], "note": "No meetings are linked to your account yet."})
    try:
        meetings = jamie_client.fetch_meetings(tag)
    except jamie_client.JamieError as e:
        return jsonify({"error": str(e)}), 502
    out = [{"id": m.get("id"), "title": m.get("title") or "(untitled meeting)",
            "startTime": m.get("startTime")} for m in meetings]
    return jsonify({"meetings": out})


@app.route("/api/portal/meeting/<meeting_id>")
def api_portal_meeting(meeting_id):
    with SessionLocal() as s:
        cust = _current_customer(s)
        tag = cust.jamie_tag if cust else ""
    if not tag:
        return jsonify({"error": "No meetings are linked to your account."}), 403
    try:
        detail = jamie_client.fetch_meeting_detail(meeting_id)
    except jamie_client.JamieError as e:
        return jsonify({"error": str(e)}), 502
    # security: only let a customer open a meeting that carries their tag
    tag_names = {(t.get("name") or "").lower() for t in (detail.get("tags") or [])}
    if tag.lower() not in tag_names:
        return jsonify({"error": "Not allowed"}), 403
    return jsonify(_shape_meeting_detail(detail))


# --------------------------------------------------------------------------- #
# Staff: Jamie meeting notes per work package (same data customers see; staff
# see every project, each scoped to that project's own Jamie tag).
# --------------------------------------------------------------------------- #
def _shape_meeting_detail(detail):
    summary = detail.get("summary") or {}
    # meetings.get embeds action items with a "content" field (tasks.list uses "text")
    tasks = [{"text": t.get("content") or t.get("text") or "",
              "completed": bool(t.get("completed")),
              "assignee": (t.get("assignee") or {}).get("name")}
             for t in (detail.get("tasks") or [])]
    return {
        "id": detail.get("id"), "title": detail.get("title") or "(untitled meeting)",
        "startTime": detail.get("startTime"),
        "summaryHtml": summary.get("html") or "", "summaryMarkdown": summary.get("markdown") or "",
        "hasSummary": bool(summary.get("html") or summary.get("markdown")), "tasks": tasks,
    }


@app.route("/api/jamie/tags")
def api_jamie_tags():
    """Jamie tag names, for the Edit-project dropdown (staff)."""
    try:
        tags = [t.get("name", "") for t in jamie_client.fetch_tags() if t.get("name")]
    except jamie_client.JamieError as e:
        return jsonify({"tags": [], "error": str(e)})
    return jsonify({"tags": sorted(tags, key=str.lower)})


def _wp_tag(wp_id):
    with SessionLocal() as s:
        wp = s.get(WorkPackage, int(wp_id)) if str(wp_id).isdigit() else None
        return (wp.jamie_tag or "") if wp else None


@app.route("/api/wp/<int:wp_id>/meetings")
def api_wp_meetings(wp_id):
    tag = _wp_tag(wp_id)
    if tag is None:
        return jsonify({"error": "work package not found"}), 404
    if not tag:
        return jsonify({"meetings": [], "note": "No Jamie tag set for this project - add one via Edit."})
    try:
        meetings = jamie_client.fetch_meetings(tag)
    except jamie_client.JamieError as e:
        return jsonify({"error": str(e)}), 502
    out = [{"id": m.get("id"), "title": m.get("title") or "(untitled meeting)",
            "startTime": m.get("startTime")} for m in meetings]
    return jsonify({"meetings": out})


@app.route("/api/wp/<int:wp_id>/meeting/<meeting_id>")
def api_wp_meeting(wp_id, meeting_id):
    tag = _wp_tag(wp_id)
    if not tag:
        return jsonify({"error": "No Jamie tag set for this project."}), 400
    try:
        detail = jamie_client.fetch_meeting_detail(meeting_id)
    except jamie_client.JamieError as e:
        return jsonify({"error": str(e)}), 502
    tag_names = {(t.get("name") or "").lower() for t in (detail.get("tags") or [])}
    if tag.lower() not in tag_names:
        return jsonify({"error": "This meeting is not tagged for this project."}), 403
    return jsonify(_shape_meeting_detail(detail))


# --------------------------------------------------------------------------- #
# Staff admin: Settings - edit the 12 process steps + sub-processes
# --------------------------------------------------------------------------- #
@app.route("/admin/settings")
def admin_settings():
    with SessionLocal() as s:
        procs = s.query(Process).order_by(Process.num).all()
        subs = s.query(Subprocess).order_by(Subprocess.process_num, Subprocess.seq).all()
        by_proc = {}
        for sp in subs:
            by_proc.setdefault(sp.process_num, []).append({
                "code": sp.code, "question": sp.question or "", "outcomes": sp.outcomes or "",
                "operational": sp.operational or "", "top_level": sp.top_level or "",
                "assets": sp.assets or "", "comment": sp.comment or "",
            })
        processes = [{"num": p.num, "title": p.title or "", "subs": by_proc.get(p.num, [])}
                     for p in procs]
        staff = [{"id": u.id, "email": u.email, "name": u.name or ""}
                 for u in s.query(StaffUser).order_by(StaffUser.email).all()]
    return render_template("admin_settings.html", processes=processes, staff=staff,
                           is_admin=_is_admin(), jira_configured=jira_client.is_configured())


@app.route("/api/admin/process/update", methods=["POST"])
def api_admin_process_update():
    body = request.get_json(force=True, silent=True) or {}
    num = _clean(body.get("num"))
    title = _clean(body.get("title"))
    if not num.isdigit():
        return jsonify({"error": "invalid step number"}), 400
    if not title:
        return jsonify({"error": "Step title is required."}), 400
    if len(title) > 255:
        return jsonify({"error": "Step title is too long (max 255 characters)."}), 400
    with SessionLocal.begin() as s:
        p = s.get(Process, int(num))
        if not p:
            return jsonify({"error": "step not found"}), 404
        p.title = title
    _mark_saved()
    return jsonify({"ok": True})


SUBPROC_FIELDS = ("outcomes", "question", "operational", "top_level", "assets", "comment")


@app.route("/api/admin/subprocess/update", methods=["POST"])
def api_admin_subprocess_update():
    body = request.get_json(force=True, silent=True) or {}
    code = _clean(body.get("code"))
    if not re.fullmatch(r"\d+\.\d+", code):
        return jsonify({"error": "invalid code"}), 400
    with SessionLocal.begin() as s:
        sp = s.get(Subprocess, code)
        if not sp:
            return jsonify({"error": "sub-process not found"}), 404
        for f in SUBPROC_FIELDS:
            if f in body:
                setattr(sp, f, "" if body[f] is None else str(body[f]).strip())
    _mark_saved()
    return jsonify({"ok": True})


def _next_sub_code(s, process_num):
    """Next free sub-process code + seq for a step, e.g. '6.5'. Uses max existing
    number after the dot + 1 so codes stay unique even with gaps."""
    subs = s.query(Subprocess).filter_by(process_num=process_num).all()
    maxn = maxseq = 0
    for sp in subs:
        try:
            maxn = max(maxn, int(sp.code.split(".")[1]))
        except (IndexError, ValueError):
            pass
        maxseq = max(maxseq, sp.seq or 0)
    return f"{process_num}.{maxn + 1}", maxseq + 1


@app.route("/api/admin/subprocess/add", methods=["POST"])
def api_admin_subprocess_add():
    body = request.get_json(force=True, silent=True) or {}
    num = _clean(body.get("process_num"))
    if not num.isdigit():
        return jsonify({"error": "invalid step number"}), 400
    with SessionLocal.begin() as s:
        if not s.get(Process, int(num)):
            return jsonify({"error": "step not found"}), 404
        code, seq = _next_sub_code(s, int(num))
        s.add(Subprocess(code=code, process_num=int(num), seq=seq, question="",
                         outcomes="", operational="", top_level="", assets="", comment=""))
    _mark_saved()
    return jsonify({"ok": True, "code": code})


@app.route("/api/admin/subprocess/duplicate", methods=["POST"])
def api_admin_subprocess_duplicate():
    body = request.get_json(force=True, silent=True) or {}
    code = _clean(body.get("code"))
    if not re.fullmatch(r"\d+\.\d+", code):
        return jsonify({"error": "invalid code"}), 400
    with SessionLocal.begin() as s:
        src = s.get(Subprocess, code)
        if not src:
            return jsonify({"error": "sub-process not found"}), 404
        newcode, seq = _next_sub_code(s, src.process_num)
        s.add(Subprocess(code=newcode, process_num=src.process_num, seq=seq,
                         question=src.question or "", outcomes=src.outcomes or "",
                         operational=src.operational or "", top_level=src.top_level or "",
                         assets=src.assets or "", comment=src.comment or ""))
    _mark_saved()
    return jsonify({"ok": True, "code": newcode})


@app.route("/api/admin/subprocess/delete", methods=["POST"])
def api_admin_subprocess_delete():
    body = request.get_json(force=True, silent=True) or {}
    code = _clean(body.get("code"))
    if not re.fullmatch(r"\d+\.\d+", code):
        return jsonify({"error": "invalid code"}), 400
    with SessionLocal.begin() as s:
        sp = s.get(Subprocess, code)
        if not sp:
            return jsonify({"error": "sub-process not found"}), 404
        # remove this point's RAG value on every work package first (FK on wp_status.code)
        s.query(WpStatus).filter_by(code=code).delete()
        s.delete(sp)
    _mark_saved()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Admin: manage ISP staff logins (admin-only; gated by ADMIN_ONLY_PREFIXES)
# --------------------------------------------------------------------------- #
@app.route("/api/admin/staff/add", methods=["POST"])
def api_admin_staff_add():
    body = request.get_json(force=True, silent=True) or {}
    email = _clean(body.get("email")).lower()
    password = body.get("password", "")
    name = _clean(body.get("name"))
    if "@" not in email or "." not in email:
        return jsonify({"error": "A valid email is required."}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters."}), 400
    with SessionLocal.begin() as s:
        if s.query(StaffUser).filter(StaffUser.email.ilike(email)).first():
            return jsonify({"error": f"A staff login for {email} already exists."}), 409
        s.add(StaffUser(email=email, password_hash=generate_password_hash(password), name=name))
    return jsonify({"ok": True})


@app.route("/api/admin/staff/reset_password", methods=["POST"])
def api_admin_staff_reset():
    body = request.get_json(force=True, silent=True) or {}
    sid = _clean(body.get("id"))
    password = body.get("password", "")
    if not sid.isdigit() or len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters."}), 400
    with SessionLocal.begin() as s:
        u = s.get(StaffUser, int(sid))
        if not u:
            return jsonify({"error": "staff login not found"}), 404
        u.password_hash = generate_password_hash(password)
    return jsonify({"ok": True})


@app.route("/api/admin/staff/delete", methods=["POST"])
def api_admin_staff_delete():
    body = request.get_json(force=True, silent=True) or {}
    sid = _clean(body.get("id"))
    if not sid.isdigit():
        return jsonify({"error": "invalid id"}), 400
    with SessionLocal.begin() as s:
        u = s.get(StaffUser, int(sid))
        if u:
            s.delete(u)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# Staff admin: manage customer logins
# --------------------------------------------------------------------------- #
@app.route("/admin/customers")
def admin_customers():
    with SessionLocal() as s:
        customers = [{"id": c.id, "email": c.email, "client": c.client or "",
                      "jamie_tag": c.jamie_tag or ""}
                     for c in s.query(Customer).order_by(Customer.email).all()]
        clients = sorted({(wp.client or "").strip() for wp in s.query(WorkPackage).all()
                          if (wp.client or "").strip()}, key=str.lower)
    try:
        tags = sorted([t.get("name", "") for t in jamie_client.fetch_tags() if t.get("name")],
                      key=str.lower)
    except jamie_client.JamieError:
        tags = []
    return render_template("admin_customers.html",
                           customers=customers, clients=clients, jamie_tags=tags)


@app.route("/api/admin/customers/add", methods=["POST"])
def api_admin_customers_add():
    body = request.get_json(force=True, silent=True) or {}
    email = _clean(body.get("email")).lower()
    password = body.get("password", "")
    client = _clean(body.get("client"))
    jamie_tag = _clean(body.get("jamie_tag"))
    if "@" not in email or "." not in email:
        return jsonify({"error": "A valid email is required."}), 400
    if len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters."}), 400
    with SessionLocal.begin() as s:
        if s.query(Customer).filter(Customer.email.ilike(email)).first():
            return jsonify({"error": f"A login for {email} already exists."}), 409
        s.add(Customer(email=email, password_hash=generate_password_hash(password),
                       client=client, jamie_tag=jamie_tag))
    return jsonify({"ok": True})


@app.route("/api/admin/customers/reset_password", methods=["POST"])
def api_admin_customers_reset():
    body = request.get_json(force=True, silent=True) or {}
    cid = _clean(body.get("id"))
    password = body.get("password", "")
    if not cid.isdigit() or len(password) < 6:
        return jsonify({"error": "Password must be at least 6 characters."}), 400
    with SessionLocal.begin() as s:
        c = s.get(Customer, int(cid))
        if not c:
            return jsonify({"error": "Customer not found."}), 404
        c.password_hash = generate_password_hash(password)
    return jsonify({"ok": True})


@app.route("/api/admin/customers/delete", methods=["POST"])
def api_admin_customers_delete():
    body = request.get_json(force=True, silent=True) or {}
    cid = _clean(body.get("id"))
    if not cid.isdigit():
        return jsonify({"error": "invalid id"}), 400
    with SessionLocal.begin() as s:
        c = s.get(Customer, int(cid))
        if c:
            s.delete(c)
    return jsonify({"ok": True})


@app.route("/api/data")
def api_data():
    try:
        return jsonify(get_data())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sync_status")
def api_sync_status():
    return jsonify({"pending": 0, "last_flush": dict(_last_saved)})


# --------------------------------------------------------------------------- #
# Task board (simple two-box Kanban per work package)
#   status: 'todo' (red) | 'progress' (amber) | 'done' (green, right-hand box)
# --------------------------------------------------------------------------- #
TASK_STATUSES = ("todo", "progress", "done")
MAX_TASK_LEN = 200
# Modified Fibonacci story points, capped at 21 (minimum + default is 1)
TASK_POINTS = (1, 2, 3, 5, 8, 13, 21)


def _task_dict(t):
    return {"id": t.id, "wp_id": t.wp_id, "title": t.title, "status": t.status,
            "points": t.points or 0, "assignee": t.assignee or "",
            "priority": t.priority or 3, "blocker": bool(t.blocker),
            "jira_key": t.jira_key or "",
            "sub_wp_id": t.sub_wp_id or None, "sprint_id": t.sprint_id or None, "seq": t.seq}


@app.route("/api/tasks/<int:wp_id>")
def api_tasks_list(wp_id):
    with SessionLocal() as s:
        rows = (s.query(WpTask).filter_by(wp_id=wp_id)
                .order_by(WpTask.seq, WpTask.id).all())
        return jsonify({"tasks": [_task_dict(t) for t in rows]})


@app.route("/api/tasks/add", methods=["POST"])
def api_tasks_add():
    body = request.get_json(force=True, silent=True) or {}
    wp_id = _clean(body.get("wp_id"))
    title = _clean(body.get("title"))[:MAX_TASK_LEN]
    assignee = _clean(body.get("assignee"))[:128]
    try:
        points = int(body.get("points", 1))
    except (TypeError, ValueError):
        points = 1
    if points not in TASK_POINTS:
        points = 1
    if not wp_id.isdigit():
        return jsonify({"error": "invalid wp_id"}), 400
    if not title:
        return jsonify({"error": "Task title is required."}), 400
    with SessionLocal.begin() as s:
        wp = s.get(WorkPackage, int(wp_id))
        if not wp:
            return jsonify({"error": "work package not found"}), 404
        max_seq = (s.query(WpTask.seq).filter_by(wp_id=int(wp_id))
                   .order_by(WpTask.seq.desc()).first())
        seq = ((max_seq[0] or 0) + 1) if max_seq else 1
        t = WpTask(wp_id=int(wp_id), title=title, status="todo", points=points,
                   assignee=assignee, seq=seq)
        s.add(t)
        s.flush()
        row = _task_dict(t)
    _mark_saved()
    return jsonify({"ok": True, "task": row})


@app.route("/api/tasks/set_status", methods=["POST"])
def api_tasks_set_status():
    body = request.get_json(force=True, silent=True) or {}
    task_id = _clean(body.get("task_id"))
    status = _clean(body.get("status"))
    if not task_id.isdigit() or status not in TASK_STATUSES:
        return jsonify({"error": "invalid task_id or status"}), 400
    with SessionLocal.begin() as s:
        t = s.get(WpTask, int(task_id))
        if not t:
            return jsonify({"error": "task not found"}), 404
        t.status = status
    _mark_saved()
    return jsonify({"ok": True})


@app.route("/api/tasks/set_title", methods=["POST"])
def api_tasks_set_title():
    body = request.get_json(force=True, silent=True) or {}
    task_id = _clean(body.get("task_id"))
    title = _clean(body.get("title"))[:MAX_TASK_LEN]
    if not task_id.isdigit():
        return jsonify({"error": "invalid task_id"}), 400
    if not title:
        return jsonify({"error": "Task title is required."}), 400
    with SessionLocal.begin() as s:
        t = s.get(WpTask, int(task_id))
        if not t:
            return jsonify({"error": "task not found"}), 404
        t.title = title
    _mark_saved()
    return jsonify({"ok": True})


@app.route("/api/tasks/set_sub_wp", methods=["POST"])
def api_tasks_set_sub_wp():
    """Link a task to one of its work package's sub-work-packages (or clear it)."""
    body = request.get_json(force=True, silent=True) or {}
    task_id = _clean(body.get("task_id"))
    raw = _clean(body.get("sub_wp_id"))
    sub_wp_id = int(raw) if raw.isdigit() else None
    if not task_id.isdigit():
        return jsonify({"error": "invalid task_id"}), 400
    with SessionLocal.begin() as s:
        t = s.get(WpTask, int(task_id))
        if not t:
            return jsonify({"error": "task not found"}), 404
        if sub_wp_id is not None:
            child = s.get(WorkPackage, sub_wp_id)
            if not child or child.parent_id != t.wp_id:
                return jsonify({"error": "not a sub-work-package of this work package"}), 400
        t.sub_wp_id = sub_wp_id
    _mark_saved()
    return jsonify({"ok": True})


@app.route("/api/tasks/set_points", methods=["POST"])
def api_tasks_set_points():
    body = request.get_json(force=True, silent=True) or {}
    task_id = _clean(body.get("task_id"))
    try:
        points = int(body.get("points"))
    except (TypeError, ValueError):
        points = -1
    if not task_id.isdigit() or points not in TASK_POINTS:
        return jsonify({"error": "invalid task_id or points"}), 400
    with SessionLocal.begin() as s:
        t = s.get(WpTask, int(task_id))
        if not t:
            return jsonify({"error": "task not found"}), 404
        t.points = points
    _mark_saved()
    return jsonify({"ok": True})


@app.route("/api/tasks/set_assignee", methods=["POST"])
def api_tasks_set_assignee():
    body = request.get_json(force=True, silent=True) or {}
    task_id = _clean(body.get("task_id"))
    assignee = _clean(body.get("assignee"))[:128]
    if not task_id.isdigit():
        return jsonify({"error": "invalid task_id"}), 400
    with SessionLocal.begin() as s:
        t = s.get(WpTask, int(task_id))
        if not t:
            return jsonify({"error": "task not found"}), 404
        t.assignee = assignee
    _mark_saved()
    return jsonify({"ok": True})


@app.route("/api/tasks/set_priority", methods=["POST"])
def api_tasks_set_priority():
    body = request.get_json(force=True, silent=True) or {}
    task_id = _clean(body.get("task_id"))
    try:
        priority = int(body.get("priority"))
    except (TypeError, ValueError):
        priority = 0
    if not task_id.isdigit() or priority not in (1, 2, 3, 4, 5):
        return jsonify({"error": "invalid task_id or priority"}), 400
    with SessionLocal.begin() as s:
        t = s.get(WpTask, int(task_id))
        if not t:
            return jsonify({"error": "task not found"}), 404
        t.priority = priority
    _mark_saved()
    return jsonify({"ok": True})


@app.route("/api/tasks/defer", methods=["POST"])
def api_tasks_defer():
    """Move a task to the upcoming sprint (app-local), or pull it back to the active one."""
    body = request.get_json(force=True, silent=True) or {}
    task_id = _clean(body.get("task_id"))
    to_upcoming = bool(body.get("upcoming", True))
    if not task_id.isdigit():
        return jsonify({"error": "invalid task_id"}), 400
    with SessionLocal.begin() as s:
        t = s.get(WpTask, int(task_id))
        if not t:
            return jsonify({"error": "task not found"}), 404
        active = s.query(Sprint).filter_by(status="active").first()
        if to_upcoming:
            upcoming = s.query(Sprint).filter_by(status="upcoming").first()
            if not upcoming:
                upcoming = Sprint(name="Upcoming sprint", status="upcoming")
                s.add(upcoming)
                s.flush()
            t.sprint_id = upcoming.id
        else:
            t.sprint_id = active.id if active else None
    _mark_saved()
    return jsonify({"ok": True})


@app.route("/api/tasks/set_blocker", methods=["POST"])
def api_tasks_set_blocker():
    body = request.get_json(force=True, silent=True) or {}
    task_id = _clean(body.get("task_id"))
    blocker = 1 if body.get("blocker") else 0
    if not task_id.isdigit():
        return jsonify({"error": "invalid task_id"}), 400
    with SessionLocal.begin() as s:
        t = s.get(WpTask, int(task_id))
        if not t:
            return jsonify({"error": "task not found"}), 404
        t.blocker = blocker
    _mark_saved()
    return jsonify({"ok": True})


@app.route("/api/tasks/delete", methods=["POST"])
def api_tasks_delete():
    body = request.get_json(force=True, silent=True) or {}
    task_id = _clean(body.get("task_id"))
    if not task_id.isdigit():
        return jsonify({"error": "invalid task_id"}), 400
    with SessionLocal.begin() as s:
        t = s.get(WpTask, int(task_id))
        if t:
            s.delete(t)
    _mark_saved()
    return jsonify({"ok": True})


def _set_point(s, wp_id, code, value):
    """Upsert or delete a single wp_status row. value '' -> delete (not started)."""
    row = s.get(WpStatus, {"wp_id": int(wp_id), "code": code})
    if value == "":
        if row:
            s.delete(row)
    elif row:
        row.value = value
    else:
        s.add(WpStatus(wp_id=int(wp_id), code=code, value=value))


@app.route("/api/update", methods=["POST"])
def api_update():
    body = request.get_json(force=True, silent=True) or {}
    wp_id = _clean(body.get("wp_id"))
    code = _clean(body.get("code"))
    value = _norm_value(body.get("value"))
    if not wp_id or not re.fullmatch(r"\d+\.\d+", code) or value not in ("", "1", "2", "3", "N/R"):
        return jsonify({"error": "invalid wp_id, code or value"}), 400
    with SessionLocal.begin() as s:
        if _is_locked(s, wp_id):
            return jsonify({"error": "This work package is inactive (locked). Make it active to edit."}), 403
        _set_point(s, wp_id, code, value)
    _mark_saved()
    return jsonify({"ok": True, "pending": 0})


@app.route("/api/update_bulk", methods=["POST"])
def api_update_bulk():
    body = request.get_json(force=True, silent=True) or {}
    wp_id = _clean(body.get("wp_id"))
    value = _norm_value(body.get("value"))
    if not wp_id or value not in ("", "1", "2", "3", "N/R"):
        return jsonify({"error": "invalid wp_id or value"}), 400
    codes = [c for c in (_clean(x) for x in (body.get("codes") or [])) if re.fullmatch(r"\d+\.\d+", c)]
    if not codes:
        return jsonify({"error": "no valid codes"}), 400
    with SessionLocal.begin() as s:
        if _is_locked(s, wp_id):
            return jsonify({"error": "This work package is inactive (locked). Make it active to edit."}), 403
        for code in codes:
            _set_point(s, wp_id, code, value)
    _mark_saved()
    return jsonify({"ok": True, "count": len(codes), "pending": 0})


@app.route("/api/set_status", methods=["POST"])
def api_set_status():
    body = request.get_json(force=True, silent=True) or {}
    wp_id = _clean(body.get("wp_id"))
    status = _clean(body.get("status"))
    if not wp_id or status not in ALLOWED_STATUSES:
        return jsonify({"error": "invalid wp_id or status"}), 400
    with SessionLocal.begin() as s:
        wp = s.get(WorkPackage, int(wp_id)) if wp_id.isdigit() else None
        if not wp:
            return jsonify({"error": "work package not found"}), 404
        wp.status = status
    _mark_saved()
    return jsonify({"ok": True, "pending": 0})


@app.route("/api/delete_project", methods=["POST"])
def api_delete_project():
    """Permanently delete a work package. Only allowed once it is inactive
    (locked), so an active project can't be removed by accident. Its status
    and finished rows are cleaned up by the cascade on the relationships."""
    body = request.get_json(force=True, silent=True) or {}
    wp_id = _clean(body.get("wp_id"))
    if not wp_id:
        return jsonify({"error": "invalid wp_id"}), 400
    with SessionLocal.begin() as s:
        wp = s.get(WorkPackage, int(wp_id)) if wp_id.isdigit() else None
        if not wp:
            return jsonify({"error": "work package not found"}), 404
        # sub-work-packages can be deleted directly; top-level projects must be inactive first
        if wp.parent_id is None and (wp.status or "").strip().lower() == "active":
            return jsonify({"error": "Make the project inactive before deleting it."}), 403
        s.delete(wp)
    _mark_saved()
    return jsonify({"ok": True})


@app.route("/api/add_project", methods=["POST"])
def api_add_project():
    body = request.get_json(force=True, silent=True) or {}
    client = _clean(body.get("client"))
    name = _clean(body.get("name"))
    icon = _clean(body.get("icon"))
    description = _clean(body.get("description"))
    jira_key = _clean(body.get("jira_project_key")).upper()
    confluence_url = _norm_url(body.get("confluence_url"))
    dropbox_url = _clean(body.get("dropbox_url"))   # a local folder path (relative to Clients), not a URL
    jamie_tag = _clean(body.get("jamie_tag"))
    parent_raw = _clean(body.get("parent_id"))
    parent_id = int(parent_raw) if parent_raw.isdigit() else None
    nr_codes = [c for c in (_clean(x) for x in (body.get("nr_codes") or [])) if re.fullmatch(r"\d+\.\d+", c)]
    category = _clean(body.get("category")) or "Customer"
    if category not in ALLOWED_CATEGORIES:
        category = "Customer"
    # a sub-work-package inherits its parent's client (when none is given) and category
    if parent_id is not None:
        with SessionLocal() as s0:
            p = s0.get(WorkPackage, parent_id)
            if p:
                if not client:
                    client = p.client or ""
                category = p.category or "Customer"
    err = _project_errors(client, name, icon, description, require_icon=(parent_id is None))
    if err:
        return jsonify({"error": err}), 400
    with SessionLocal.begin() as s:
        if parent_id is not None and not s.get(WorkPackage, parent_id):
            return jsonify({"error": "parent work package not found"}), 404
        if _dup_exists(s, client, name, parent_id=parent_id):
            return jsonify({"error": f"A project '{client} - {name}' already exists."}), 409
        max_id = s.query(WorkPackage.id).order_by(WorkPackage.id.desc()).first()
        new_id = (max_id[0] if max_id else 0) + 1
        sub_num = _next_sub_num(s, parent_id) if parent_id is not None else None
        s.add(WorkPackage(id=new_id, client=client, name=name, description=description,
                          status="Active", category=category, icon=icon,
                          jira_project_key=jira_key,
                          confluence_url=confluence_url, dropbox_url=dropbox_url,
                          jamie_tag=jamie_tag, parent_id=parent_id, sub_num=sub_num))
        # only Customer work packages carry the 12-step (Not-required) configuration
        if category == "Customer":
            for code in nr_codes:
                s.add(WpStatus(wp_id=new_id, code=code, value="N/R"))
    _mark_saved()
    return jsonify({"ok": True, "wp_id": str(new_id)})


# --------------------------------------------------------------------------- #
# Jira (read-only): epic picker + on-demand story-point refresh.
# Each work package is linked to a Jira epic (chosen from a dropdown); points
# are summed over the issues under that epic.
# --------------------------------------------------------------------------- #
@app.route("/api/jira/epics")
def api_jira_epics():
    """List of Jira epics for the Edit-form dropdown. Token stays server-side."""
    if not jira_client.is_configured():
        return jsonify({"configured": False, "epics": []})
    try:
        return jsonify({"configured": True, "epics": jira_client.list_epics()})
    except jira_client.JiraError as e:
        return jsonify({"configured": True, "error": str(e), "epics": []}), 502


@app.route("/api/jira/refresh", methods=["POST"])
def api_jira_refresh():
    """Re-pull done/total story points for the epic linked to each work package."""
    if not jira_client.is_configured():
        return jsonify({"error": "Jira is not configured on the server."}), 400
    # read the linked epics with a short-lived session (no network under a write lock)
    with SessionLocal() as s:
        linked = [(wp.id, (wp.jira_project_key or "").strip())
                  for wp in s.query(WorkPackage).all() if (wp.jira_project_key or "").strip()]
    results, errors = {}, []
    for wp_id, key in linked:
        try:
            results[wp_id] = jira_client.epic_points(key)
        except jira_client.JiraError as e:
            errors.append({"epic": key, "error": str(e)})
    now = datetime.utcnow()
    with SessionLocal.begin() as s:
        for wp_id, pts in results.items():
            wp = s.get(WorkPackage, wp_id)
            if wp:
                wp.jira_done, wp.jira_total, wp.jira_synced_at = pts["done"], pts["total"], now
    _mark_saved()
    return jsonify({"ok": True, "updated": len(results), "errors": errors})


def _jira_push_plan(wp_id):
    """Work out what a push would do, WITHOUT writing anything. Shared by the preview
    and the real push. Returns (plan, error) where exactly one is set. plan is a dict
    with epic/project/wp/to_create[]/skipped[]/browse_url; error is (message, status)."""
    if not jira_client.is_configured():
        return None, ("Jira is not configured on the server.", 400)

    with SessionLocal() as s:
        wp = s.get(WorkPackage, wp_id)
        if not wp:
            return None, ("work package not found", 404)
        if (wp.category or "Customer") != "Customer":
            return None, ("Push to Jira is only for Customer work packages "
                          "(the ones that use the 12-step Abacus checklist).", 400)
        epic_key = (wp.jira_project_key or "").strip()
        wp_label = f"{wp.client} - {wp.name}".strip(" -")
        if not epic_key:
            return None, ("This work package isn't linked to a Jira epic. "
                          "Add one via Edit first.", 400)
        # build the sub-point summaries the same way get_data() labels them
        subs = s.query(Subprocess).order_by(Subprocess.process_num, Subprocess.seq).all()
        vals = {st.code: st.value
                for st in s.query(WpStatus).filter_by(wp_id=wp_id).all()}
    project_key = epic_key.split("-")[0]

    summaries = []
    for sp in subs:
        if status_of(vals.get(sp.code, "")) == "nr":
            continue                       # skip 'Not required' points
        label = (sp.outcomes or "").strip() or (sp.question or "").strip()
        summaries.append(f"{sp.code} {label}".strip())

    # split into 'new' vs 'already under the epic' (idempotent re-push)
    try:
        existing = jira_client.epic_child_summaries(epic_key)
    except jira_client.JiraError as e:
        return None, (str(e), 502)

    to_create = [sm for sm in summaries if sm not in existing]
    skipped = [sm for sm in summaries if sm in existing]
    # where the button should land the user afterwards (the Backlog board)
    browse_url = (jira_client.project_backlog_url(project_key)
                  or f"{jira_client.SITE_URL}/browse/{epic_key}")
    plan = {"epic": epic_key, "project": project_key, "wp": wp_label,
            "to_create": to_create, "skipped": skipped, "browse_url": browse_url}
    return plan, None


@app.route("/api/jira/push/<int:wp_id>/preview")
def api_jira_push_preview(wp_id):
    """Read-only: the list of sub-points a push would create (and how many it would
    skip). Powers the confirm dialog - nothing is written to Jira."""
    plan, err = _jira_push_plan(wp_id)
    if err:
        return jsonify({"error": err[0]}), err[1]
    return jsonify({"ok": True, "to_create_count": len(plan["to_create"]),
                    "skipped_count": len(plan["skipped"]), **plan})


@app.route("/api/jira/push/<int:wp_id>", methods=["POST"])
def api_jira_push(wp_id):
    """Push this work package's Abacus sub-points into its linked Jira epic's backlog,
    one issue per sub-point. Skips the 'Not required' points and any whose summary is
    already under the epic (so it's safe to re-run). The app never reads these back."""
    plan, err = _jira_push_plan(wp_id)
    if err:
        return jsonify({"error": err[0]}), err[1]
    epic_key, project_key = plan["epic"], plan["project"]

    # optional subset: only push the sub-points the user left ticked. We intersect
    # with the freshly-computed plan so a stale/tampered list can't create anything
    # unexpected or duplicate what's already in Jira.
    body = request.get_json(force=True, silent=True) or {}
    requested = body.get("summaries")
    if isinstance(requested, list):
        chosen = set(str(x) for x in requested)
        targets = [s for s in plan["to_create"] if s in chosen]
    else:
        targets = plan["to_create"]

    try:
        issue_type_id = jira_client.project_creatable_issue_type(project_key)
    except jira_client.JiraError as e:
        return jsonify({"error": str(e)}), 502

    created, errors = [], []
    for summary in targets:
        try:
            key = jira_client.create_backlog_issue(project_key, epic_key, summary, issue_type_id)
            created.append(key)
        except jira_client.JiraError as e:
            errors.append({"summary": summary, "error": str(e)})
            # a 403 (read-only token) will fail every item - stop hammering Jira
            if "403" in str(e):
                break

    _mark_saved()
    return jsonify({"ok": True, "epic": epic_key, "project": project_key, "wp": plan["wp"],
                    "created": len(created), "created_keys": created,
                    "skipped": len(plan["skipped"]), "errors": errors,
                    "browse_url": plan["browse_url"]})


@app.route("/api/jira/sync", methods=["POST"])
def api_jira_sync():
    """Manual 'Sync from Jira': refresh the active sprint's task cards and backfill
    recent per-week history. The app stays the source of truth; this just pulls."""
    try:
        result = jira_sync.run_sync()
    except jira_client.JiraError as e:
        return jsonify({"error": str(e)}), 502
    if result.get("error"):
        return jsonify(result), 400
    _mark_saved()
    return jsonify(result)


@app.route("/api/sprint/jira_status")
def api_sprint_jira_status():
    """What Jira currently considers its active sprint(s) - to check the app is aligned."""
    try:
        return jsonify(jira_sync.active_sprint_status())
    except jira_client.JiraError as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/history")
def api_history():
    """Per-week, per-category points history for the history view (newest week first)."""
    with SessionLocal() as s:
        rows = (s.query(SprintHistory)
                .order_by(SprintHistory.week_start.desc(), SprintHistory.category).all())
    out = [{
        "week_start": r.week_start, "week_end": r.week_end, "label": r.label,
        "category": r.category, "points_planned": r.points_planned, "points_done": r.points_done,
        "tasks_total": r.tasks_total, "tasks_todo": r.tasks_todo,
        "tasks_progress": r.tasks_progress, "tasks_done": r.tasks_done, "source": r.source,
    } for r in rows]
    return jsonify({"history": out, "categories": ["Customer", "Marketing", "Process and Ops"]})


@app.route("/api/sprint/rollover", methods=["POST"])
def api_sprint_rollover():
    """Start a new sprint: snapshot the active sprint's per-category points into history,
    close it, promote the upcoming sprint to active (or create a fresh one), and carry any
    not-done tasks forward. Done tasks stay with the closed sprint (archived)."""
    from datetime import date, timedelta
    with SessionLocal.begin() as s:
        active = s.query(Sprint).filter_by(status="active").first()
        active_id = active.id if active else None
        tasks = s.query(WpTask).all()
        cat_by_wp = {wp.id: (wp.category or "Customer") for wp in s.query(WorkPackage).all()}

        agg = {}
        for t in tasks:
            if t.sprint_id not in (active_id, None):
                continue
            cat = cat_by_wp.get(t.wp_id, "Customer")
            a = agg.setdefault(cat, {"todo": 0, "progress": 0, "done": 0, "total": 0, "planned": 0, "done_pts": 0})
            if t.status in ("todo", "progress", "done"):
                a[t.status] += 1
            a["total"] += 1
            a["planned"] += (t.points or 0)
            if t.status == "done":
                a["done_pts"] += (t.points or 0)

        today = date.today()
        mon = today - timedelta(days=today.weekday())
        fri = mon + timedelta(days=4)
        wk, label = mon.isoformat(), f"{mon.day}-{fri.day} {fri.strftime('%b %y')}"
        # replace this week's app-sourced snapshot (idempotent if rolled over twice)
        s.query(SprintHistory).filter_by(week_start=wk, source="app").delete(synchronize_session=False)
        for cat, a in agg.items():
            s.add(SprintHistory(week_start=wk, week_end=fri.isoformat(), label=label, category=cat,
                                points_planned=a["planned"], points_done=a["done_pts"], tasks_total=a["total"],
                                tasks_todo=a["todo"], tasks_progress=a["progress"], tasks_done=a["done"], source="app"))

        if active:
            active.status = "closed"
            active.week_start, active.week_end = wk, fri.isoformat()
            if not active.name or active.name == "Current sprint":
                active.name = label
        upcoming = s.query(Sprint).filter_by(status="upcoming").first()
        if upcoming:
            upcoming.status = "active"
            new_active = upcoming
        else:
            new_active = Sprint(name="Current sprint", status="active")
            s.add(new_active)
            s.flush()
        # carry not-done tasks into the new sprint; done tasks archive with the closed one
        for t in tasks:
            if t.sprint_id in (active_id, None):
                t.sprint_id = active_id if t.status == "done" else new_active.id
    _mark_saved()
    return jsonify({"ok": True, "closed": label, "snapshot": agg})


def _resolve_dropbox(stored):
    """Turn a work package's stored Dropbox value into a LOCAL folder under DROPBOX_ROOT.
    Accepts a plain relative folder ("Bridgepoint"), a pasted absolute path, or a legacy
    dropbox.com URL - all get re-rooted under this machine's Clients folder, so nothing is
    tied to one person's account. Returns (local_path, web_url, error): exactly one is set."""
    from urllib.parse import unquote
    v = (stored or "").strip()
    if not v:
        return (None, None, "No Dropbox folder set for this project (add one via Edit).")
    raw = v.replace("\\", "/")
    low = raw.lower()
    rel = None
    if "clients/" in low:                       # web URL or full path that includes .../Clients/...
        rel = unquote(raw[low.rindex("clients/") + len("clients/"):]).strip("/")
    elif low.startswith(("http://", "https://")):
        return (None, v, None)                  # some other web link - just open it in the browser
    else:
        rel = unquote(raw).strip("/")           # a plain relative folder path
    full = os.path.normpath(os.path.join(DROPBOX_ROOT, rel))
    # containment guard: never open anything outside the Clients root
    if os.path.normcase(full) != os.path.normcase(DROPBOX_ROOT) and \
       not os.path.normcase(full).startswith(os.path.normcase(DROPBOX_ROOT) + os.sep):
        return (None, None, "That folder is outside the Dropbox Clients area.")
    return (full, None, None)


def _dropbox_web_url(stored):
    """Best-effort dropbox.com web URL for a stored value. Used on the hosted site,
    where there is no local folder to open: a pasted dropbox.com link is used as-is,
    and a plain relative folder is re-rooted under the team Dropbox 'Clients' space
    (matching the /work/Clients/... links users paste)."""
    from urllib.parse import quote, unquote
    v = (stored or "").strip()
    if not v:
        return None
    raw = v.replace("\\", "/")
    if raw.lower().startswith(("http://", "https://")):
        return v
    rel = unquote(raw).strip("/")
    return "https://www.dropbox.com/work/Clients/" + quote(rel)


@app.route("/api/open_dropbox", methods=["POST"])
def api_open_dropbox():
    """Open a project's Dropbox folder in the local File Explorer (works because the app
    runs on the user's own machine). Falls back to returning a web URL to open in a tab."""
    body = request.get_json(force=True, silent=True) or {}
    wp_id = _clean(body.get("wp_id"))
    if not wp_id.isdigit():
        return jsonify({"error": "invalid wp_id"}), 400
    with SessionLocal() as s:
        wp = s.get(WorkPackage, int(wp_id))
        if not wp:
            return jsonify({"error": "work package not found"}), 404
        stored = wp.dropbox_url
    full, url, err = _resolve_dropbox(stored)
    if url:
        return jsonify({"url": url})
    if err:
        return jsonify({"error": err}), 400
    if not hasattr(os, "startfile"):            # hosted (Render/Linux): no desktop folder to open
        web = _dropbox_web_url(stored)          # fall back to opening the Dropbox web folder in a tab
        if web:
            return jsonify({"url": web})
        return jsonify({"error": "No Dropbox folder set for this project (add one via Edit)."}), 400
    if not os.path.isdir(full):
        return jsonify({"error": f"Folder not found on this machine: {full}"}), 404
    try:
        os.startfile(full)                      # noqa: S606 - opens Windows Explorer at the folder
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "path": full})


@app.route("/api/dropbox/folders")
def api_dropbox_folders():
    """List the immediate sub-folders of a folder under the local Dropbox Clients root,
    for the folder picker. `path` is relative to Clients ("" = the Clients root itself)."""
    rel = _clean(request.args.get("path")).replace("\\", "/").strip("/")
    full = os.path.normpath(os.path.join(DROPBOX_ROOT, rel))
    nc = os.path.normcase
    if nc(full) != nc(DROPBOX_ROOT) and not nc(full).startswith(nc(DROPBOX_ROOT) + os.sep):
        return jsonify({"error": "That location is outside the Dropbox Clients area."}), 400
    if not os.path.isdir(full):
        return jsonify({"error": f"Folder not found on this machine: {full}"}), 404
    try:
        folders = sorted(
            (e.name for e in os.scandir(full) if e.is_dir() and not e.name.startswith((".", "~", "$"))),
            key=str.lower)
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    parent = None if not rel else "/".join(rel.split("/")[:-1])
    return jsonify({"path": rel, "parent": parent, "folders": folders})


@app.route("/api/edit_project", methods=["POST"])
def api_edit_project():
    body = request.get_json(force=True, silent=True) or {}
    wp_id = _clean(body.get("wp_id"))
    client = _clean(body.get("client"))
    name = _clean(body.get("name"))
    icon = _clean(body.get("icon"))
    description = _clean(body.get("description"))
    jira_key = _clean(body.get("jira_project_key")).upper()
    confluence_url = _norm_url(body.get("confluence_url"))
    dropbox_url = _clean(body.get("dropbox_url"))   # a local folder path (relative to Clients), not a URL
    jamie_tag = _clean(body.get("jamie_tag"))
    desired = {c for c in (_clean(x) for x in (body.get("nr_codes") or [])) if re.fullmatch(r"\d+\.\d+", c)}
    if not wp_id:
        return jsonify({"error": "wp_id is required"}), 400
    with SessionLocal.begin() as s:
        if _is_locked(s, wp_id):
            return jsonify({"error": "This work package is inactive (locked). Make it active to edit."}), 403
        wp = s.get(WorkPackage, int(wp_id)) if wp_id.isdigit() else None
        if not wp:
            return jsonify({"error": "work package not found"}), 404
        err = _project_errors(client, name, icon, description, require_icon=(wp.parent_id is None))
        if err:
            return jsonify({"error": err}), 400
        if _dup_exists(s, client, name, exclude_id=wp.id, parent_id=wp.parent_id):
            return jsonify({"error": f"A project '{client} - {name}' already exists."}), 409
        wp.client, wp.name, wp.icon = client, name, icon
        wp.description = description
        wp.confluence_url = confluence_url
        wp.dropbox_url = dropbox_url
        wp.jamie_tag = jamie_tag
        if jira_key != (wp.jira_project_key or ""):
            # epic link changed -> clear stale totals until the next refresh
            wp.jira_project_key = jira_key
            wp.jira_done, wp.jira_total, wp.jira_synced_at = 0, 0, None
        # Category is set once at creation and locked on edit. Only Customer work
        # packages have a 12-step (Not-required) configuration to reconcile.
        if (wp.category or "Customer") == "Customer":
            current = {st.code: st.value for st in s.query(WpStatus).filter_by(wp_id=wp.id).all()}
            all_codes = [row[0] for row in s.query(Subprocess.code).all()]
            for code in all_codes:
                is_nr = current.get(code) == "N/R"
                if code in desired and not is_nr:
                    _set_point(s, wp.id, code, "N/R")
                elif code not in desired and is_nr:
                    _set_point(s, wp.id, code, "")
    _mark_saved()
    return jsonify({"ok": True, "pending": 0})


@app.route("/api/duplicate_project", methods=["POST"])
def api_duplicate_project():
    """Clone an existing work package's setup (client, name+' (copy)', description,
    year, icon and its Not-required points) into a new Active project. Progress is
    NOT copied - the copy starts fresh."""
    body = request.get_json(force=True, silent=True) or {}
    wp_id = _clean(body.get("wp_id"))
    if not wp_id.isdigit():
        return jsonify({"error": "invalid wp_id"}), 400
    with SessionLocal.begin() as s:
        src = s.get(WorkPackage, int(wp_id))
        if not src:
            return jsonify({"error": "work package not found"}), 404
        # find a non-clashing name "<name> (copy)", "(copy 2)", ... (scoped to siblings)
        base = f"{src.name} (copy)"
        new_name = base
        n = 2
        while _dup_exists(s, src.client, new_name, parent_id=src.parent_id):
            new_name = f"{src.name} (copy {n})"
            n += 1
        max_id = s.query(WorkPackage.id).order_by(WorkPackage.id.desc()).first()
        new_id = (max_id[0] if max_id else 0) + 1
        # a duplicated sub-work-package keeps its parent and takes the next free number
        sub_num = _next_sub_num(s, src.parent_id) if src.parent_id is not None else None
        s.add(WorkPackage(id=new_id, client=src.client, name=new_name,
                          description=src.description or "",
                          status="Active", category=src.category or "Customer",
                          icon=src.icon or "",
                          parent_id=src.parent_id, sub_num=sub_num))
        # copy only the Not-required configuration (fresh progress); Customer only
        if (src.category or "Customer") == "Customer":
            for st in s.query(WpStatus).filter_by(wp_id=src.id, value="N/R").all():
                s.add(WpStatus(wp_id=new_id, code=st.code, value="N/R"))
    _mark_saved()
    return jsonify({"ok": True, "wp_id": str(new_id)})


@app.route("/api/update_guidance", methods=["POST"])
def api_update_guidance():
    body = request.get_json(force=True, silent=True) or {}
    code = _clean(body.get("code"))
    fields = body.get("fields") or {}
    if not re.fullmatch(r"\d+\.\d+", code):
        return jsonify({"error": "invalid code"}), 400
    changed = []
    with SessionLocal.begin() as s:
        sp = s.get(Subprocess, code)
        if not sp:
            return jsonify({"error": "unknown code"}), 404
        for k in GUIDANCE_FIELDS:
            if k in fields:
                v = "" if fields[k] is None else str(fields[k]).strip()
                if v != _clean(getattr(sp, k)):
                    setattr(sp, k, v)
                    changed.append(k)
    if changed:
        _mark_saved()
    return jsonify({"ok": True, "changed": changed, "pending": 0})


# --------------------------------------------------------------------------- #
# Local dev entry point (production uses: gunicorn app:app)
# --------------------------------------------------------------------------- #
def _find_free_port(preferred):
    import socket
    for cand in [preferred] + list(range(preferred + 1, preferred + 40)):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
            try:
                sk.bind(("127.0.0.1", cand))
                return cand
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sk:
        sk.bind(("127.0.0.1", 0))
        return sk.getsockname()[1]


def _install_terminal_close_handler():
    """Close the terminal window -> stop the server (no orphaned process holding the port).
    On Windows we catch the console CTRL_CLOSE/LOGOFF/SHUTDOWN events; elsewhere SIGHUP
    (sent when the controlling terminal goes away) is enough."""
    if os.environ.get("NO_TERMINAL_WATCH") == "1":
        return                                  # opt-out (e.g. background/automated runs)
    if os.name == "nt":
        import ctypes
        import threading
        k32 = ctypes.windll.kernel32
        # (a) console window close / logoff / shutdown events
        try:
            handler = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)(
                lambda evt: (os._exit(0) if evt in (2, 5, 6) else False))
            k32.SetConsoleCtrlHandler(handler, True)
            globals()["_CONSOLE_CTRL_HANDLER"] = handler   # keep a ref so it isn't GC'd
        except Exception:
            pass
        # (b) the parent terminal/shell exiting (covers editor & tabbed terminals that
        #     close the shell without sending a console event) -> stop the server too
        try:
            k32.OpenProcess.restype = ctypes.c_void_p
            k32.OpenProcess.argtypes = [ctypes.c_uint, ctypes.c_bool, ctypes.c_uint]
            k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint]
            parent = k32.OpenProcess(0x00100000, False, os.getppid())   # SYNCHRONIZE
            if parent:
                def _watch_parent():
                    k32.WaitForSingleObject(parent, 0xFFFFFFFF)         # blocks until parent exits
                    os._exit(0)
                threading.Thread(target=_watch_parent, daemon=True).start()
        except Exception:
            pass
    else:
        try:
            import signal
            signal.signal(signal.SIGHUP, lambda *_: os._exit(0))
        except Exception:
            pass


if __name__ == "__main__":
    import threading
    import webbrowser
    port = _find_free_port(int(os.environ.get("PORT", 5070)))
    url = f"http://127.0.0.1:{port}/"
    if os.environ.get("NO_BROWSER") != "1":
        t = threading.Timer(1.2, lambda: webbrowser.open(url))
        t.daemon = True
        t.start()
    _install_terminal_close_handler()
    print(f"Abacus Work Package Tracker running at {url}")
    print(f"(Log in with the shared password. Local default: '{APP_PASSWORD}')")
    print("(Close this terminal window to stop the server.)")
    app.run(host="127.0.0.1", port=port, debug=False)
