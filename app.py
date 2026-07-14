"""
Abacus Work Package Tracker - web app (database-backed).

Reads/writes a SQL database (SQLite locally, PostgreSQL on Render - see models.py)
and serves the same dashboard as before. The whole page sits behind a single shared
password. The front-end contract (8 /api endpoints + the /api/data shape) is unchanged,
so static/app.js and templates/index.html are reused as-is.

Local:  python app.py            (opens a browser at http://127.0.0.1:5010)
Prod:   gunicorn app:app         (Render sets DATABASE_URL, SECRET_KEY, APP_PASSWORD)
"""

import hmac
import os
import re
import secrets
import threading
from datetime import datetime, timedelta

from flask import (
    Flask, jsonify, render_template, request, redirect, url_for, session,
)

from models import (
    init_db, SessionLocal,
    Process, Subprocess, WorkPackage, WpStatus, WpFinished,
)

app = Flask(__name__)
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)

# Shared team password (set APP_PASSWORD in production; default is for local dev only)
APP_PASSWORD = os.environ.get("APP_PASSWORD", "abacus")

# --------------------------------------------------------------------------- #
# Single-seat lock + idle timeout
# Only one person may be signed in at a time. The current holder is released
# after IDLE_MINUTES with no requests, freeing the seat for someone else.
# NOTE: this state lives in memory, so the app must run a single worker
# (gunicorn -w 1). With >1 worker each would track its own seat.
# --------------------------------------------------------------------------- #
IDLE_MINUTES = int(os.environ.get("IDLE_MINUTES", 5))
IDLE_LIMIT = timedelta(minutes=IDLE_MINUTES)
_seat_lock = threading.Lock()
_seat = {"token": None, "last": None}  # token = active session id, last = datetime of last activity


def _seat_expire_if_idle(now):
    """Release the seat if the holder has been idle past the limit. Caller holds _seat_lock."""
    if _seat["token"] is not None and _seat["last"] is not None and now - _seat["last"] > IDLE_LIMIT:
        _seat.update(token=None, last=None)


init_db()  # create tables if missing (safe to call every start)

# --------------------------------------------------------------------------- #
# Status helpers (unchanged meaning from the Excel version)
# --------------------------------------------------------------------------- #
SCORE = {"done": 1.0, "progress": 0.5, "todo": 0.0}
ALLOWED_STATUSES = ("Active", "Inactive", "Removed", "Complete", "On hold")
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
MAX_NAME_LEN = 60          # short project title
MAX_DESC_WORDS = 35        # detailed description word cap


def _valid_icon(icon):
    """Require a real emoji: non-empty, short, and containing a non-ASCII char
    (so plain text like 'abc' is rejected)."""
    icon = _clean(icon)
    return bool(icon) and len(icon) <= 8 and re.search(r"[^\x00-\x7F]", icon) is not None


def _word_count(text):
    return len(_clean(text).split())


def _project_errors(client, name, year, icon, description):
    """Return an error string for the add/edit project form, or '' if valid."""
    if not client or not name:
        return "Client and project name are required."
    if not year:
        return "Please enter the year this project relates to."
    if len(name) > MAX_NAME_LEN:
        return f"Project name is too long (max {MAX_NAME_LEN} characters)."
    if not _valid_icon(icon):
        return "Please choose an emoji icon (a picture, not text)."
    if _word_count(description) > MAX_DESC_WORDS:
        return f"Description is too long (max {MAX_DESC_WORDS} words)."
    return ""


def _dup_exists(s, client, name, year, exclude_id=None):
    """True if another work package already has this client + name + year (case-insensitive)."""
    c, n, y = client.strip().lower(), name.strip().lower(), (year or "").strip().lower()
    for wp in s.query(WorkPackage).all():
        if exclude_id is not None and wp.id == exclude_id:
            continue
        if ((wp.client or "").strip().lower() == c
                and (wp.name or "").strip().lower() == n
                and (wp.year or "").strip().lower() == y):
            return True
    return False


# --------------------------------------------------------------------------- #
# Authentication (single shared password)
# --------------------------------------------------------------------------- #
@app.before_request
def _require_login():
    p = request.path
    if p == "/login" or p == "/logout" or p.startswith("/static/"):
        return
    if not session.get("authed"):
        if p.startswith("/api/"):
            return jsonify({"error": "Not authenticated"}), 401
        return redirect(url_for("login"))
    # Authed: enforce the single seat and refresh this holder's activity.
    now = datetime.utcnow()
    with _seat_lock:
        _seat_expire_if_idle(now)
        if _seat["token"] is None or session.get("seat") != _seat["token"]:
            # Seat was taken over by someone else, or expired from inactivity.
            session.clear()
            if p.startswith("/api/"):
                return jsonify({"error": "session_expired"}), 401
            return redirect(url_for("login"))
        _seat["last"] = now


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if not hmac.compare_digest(request.form.get("password", ""), APP_PASSWORD):
            return render_template("login.html", error="Incorrect password"), 401
        now = datetime.utcnow()
        with _seat_lock:
            _seat_expire_if_idle(now)
            if _seat["token"] is not None:
                return render_template(
                    "login.html",
                    error=f"Someone else is signed in right now. Try again after {IDLE_MINUTES} minutes of their inactivity.",
                ), 409
            token = secrets.token_hex(16)
            _seat.update(token=token, last=now)
        session["authed"] = True
        session["seat"] = token
        session.permanent = True
        return redirect(url_for("index"))
    if session.get("authed"):
        return redirect(url_for("index"))
    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    tok = session.get("seat")
    with _seat_lock:
        if tok is not None and _seat["token"] == tok:
            _seat.update(token=None, last=None)
    session.clear()
    return redirect(url_for("login"))


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

        process_headers = [{"num": p.num, "title": p.title} for p in processes]

        work_packages = []
        for wp in wps:
            wpvals = vals.get(wp.id, {})
            wpfin = fin.get(wp.id, set())
            phases, group_pcts = [], []
            for p in processes:
                subs_out, answered, nr, score_sum = [], 0, 0, 0.0
                for sub in subs_by_process.get(p.num, []):
                    raw = wpvals.get(sub["code"], "")
                    st = status_of(raw)
                    if st in SCORE:
                        answered += 1
                        score_sum += SCORE[st]
                    elif st == "nr":
                        nr += 1
                    subs_out.append({"code": sub["code"], "label": sub["label"],
                                     "status": st, "raw": raw})
                is_fin = p.num in wpfin
                total = len(subs_out)
                effective = total - nr
                if is_fin:
                    pct = 100.0
                elif effective <= 0:
                    pct = 100.0 if nr > 0 else 0.0
                else:
                    pct = round(score_sum / effective * 100)
                if pct >= 100:
                    pstatus = "done"
                elif answered == 0 and nr == 0 and not is_fin:
                    pstatus = "na"
                else:
                    pstatus = "progress"
                group_pcts.append(pct)
                phases.append({"num": p.num, "title": p.title, "pct": pct,
                               "status": pstatus, "finished": is_fin, "subs": subs_out})

            overall = round(sum(group_pcts) / len(group_pcts)) if group_pcts else 0
            work_packages.append({
                "wp_id": str(wp.id), "client": wp.client, "name": wp.name,
                "description": wp.description or "", "year": wp.year or "",
                "status": wp.status or "Unknown", "points": wp.points or "",
                "icon": wp.icon or "", "overall": overall, "phases": phases,
            })

    return {
        "generated": datetime.now().strftime("%d %b %Y, %H:%M"),
        "processes": process_headers,
        "work_packages": work_packages,
        "reference": reference,
        "pending": 0,
        "last_flush": dict(_last_saved),
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/data")
def api_data():
    try:
        return jsonify(get_data())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sync_status")
def api_sync_status():
    return jsonify({"pending": 0, "last_flush": dict(_last_saved)})


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


@app.route("/api/add_project", methods=["POST"])
def api_add_project():
    body = request.get_json(force=True, silent=True) or {}
    client = _clean(body.get("client"))
    name = _clean(body.get("name"))
    year = _clean(body.get("year"))
    icon = _clean(body.get("icon"))
    description = _clean(body.get("description"))
    nr_codes = [c for c in (_clean(x) for x in (body.get("nr_codes") or [])) if re.fullmatch(r"\d+\.\d+", c)]
    err = _project_errors(client, name, year, icon, description)
    if err:
        return jsonify({"error": err}), 400
    with SessionLocal.begin() as s:
        if _dup_exists(s, client, name, year):
            return jsonify({"error": f"A project '{client} - {name}' for {year} already exists."}), 409
        max_id = s.query(WorkPackage.id).order_by(WorkPackage.id.desc()).first()
        new_id = (max_id[0] if max_id else 0) + 1
        s.add(WorkPackage(id=new_id, client=client, name=name, description=description,
                          year=year, status="Active", icon=icon))
        for code in nr_codes:
            s.add(WpStatus(wp_id=new_id, code=code, value="N/R"))
    _mark_saved()
    return jsonify({"ok": True, "wp_id": str(new_id)})


@app.route("/api/edit_project", methods=["POST"])
def api_edit_project():
    body = request.get_json(force=True, silent=True) or {}
    wp_id = _clean(body.get("wp_id"))
    client = _clean(body.get("client"))
    name = _clean(body.get("name"))
    year = _clean(body.get("year"))
    icon = _clean(body.get("icon"))
    description = _clean(body.get("description"))
    desired = {c for c in (_clean(x) for x in (body.get("nr_codes") or [])) if re.fullmatch(r"\d+\.\d+", c)}
    if not wp_id:
        return jsonify({"error": "wp_id is required"}), 400
    err = _project_errors(client, name, year, icon, description)
    if err:
        return jsonify({"error": err}), 400
    with SessionLocal.begin() as s:
        if _is_locked(s, wp_id):
            return jsonify({"error": "This work package is inactive (locked). Make it active to edit."}), 403
        wp = s.get(WorkPackage, int(wp_id)) if wp_id.isdigit() else None
        if not wp:
            return jsonify({"error": "work package not found"}), 404
        if _dup_exists(s, client, name, year, exclude_id=wp.id):
            return jsonify({"error": f"A project '{client} - {name}' for {year} already exists."}), 409
        wp.client, wp.name, wp.icon = client, name, icon
        wp.description, wp.year = description, year
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
    NOT copied — the copy starts fresh."""
    body = request.get_json(force=True, silent=True) or {}
    wp_id = _clean(body.get("wp_id"))
    if not wp_id.isdigit():
        return jsonify({"error": "invalid wp_id"}), 400
    with SessionLocal.begin() as s:
        src = s.get(WorkPackage, int(wp_id))
        if not src:
            return jsonify({"error": "work package not found"}), 404
        # find a non-clashing name "<name> (copy)", "(copy 2)", ...
        base = f"{src.name} (copy)"
        new_name = base
        n = 2
        while _dup_exists(s, src.client, new_name, src.year or ""):
            new_name = f"{src.name} (copy {n})"
            n += 1
        max_id = s.query(WorkPackage.id).order_by(WorkPackage.id.desc()).first()
        new_id = (max_id[0] if max_id else 0) + 1
        s.add(WorkPackage(id=new_id, client=src.client, name=new_name,
                          description=src.description or "", year=src.year or "",
                          status="Active", icon=src.icon or ""))
        # copy only the Not-required configuration (fresh progress)
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


if __name__ == "__main__":
    import threading
    import webbrowser
    port = _find_free_port(int(os.environ.get("PORT", 5010)))
    url = f"http://127.0.0.1:{port}/"
    if os.environ.get("NO_BROWSER") != "1":
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    print(f"Abacus Work Package Tracker running at {url}")
    print(f"(Log in with the shared password. Local default: '{APP_PASSWORD}')")
    app.run(host="127.0.0.1", port=port, debug=False)
