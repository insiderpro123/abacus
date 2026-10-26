"""
Tracking Dashboard - the outbound contact tracker, as one Flask blueprint.

One screen. A board of the live leads you are working, one column per stage,
with everything about a record in a slide-over panel beside it. There are no
filters and no number tiles: every current lead is on the board all the time,
and closed work sits behind one toggle. Reference material (the Role & Hook
library and the playbook) lives in a drawer, not in a nav bar.

  * a pipeline board with drag-and-drop: the three live stages and Add to
    InsideTrack (the win, called Work agreed until 20 Oct 2026), with Closed
    (Lost or Nurture) behind one toggle;
  * each card shows the initials of whoever is responsible for it, a sector
    tag and a few-word summary (20 Oct 2026), all typed in the record. The
    chance of a win came off the card the same day; it still sorts a column;
  * the card's arrow advances the stage without reloading the screen: every
    write returns the updated record;
  * the record (click a card) is the company's: the stage row, then five
    people to contact, each with a free-text contact status and a red /
    amber / green, then one Comments box. That replaced the latest update,
    chance slider, next step and Details on 8 Oct 2026 (user's request);
    those columns are still in the database, unshown;
  * the Role & Hook library, which pre-fills the role and the wedge when a
    contact is added.

Everything the dashboard owns is in this package and namespaced, so it can be
dropped into the live process dashboard as it stands:

  page      /tracking                 endpoint  tracking.page
  API       /api/tracking/...         endpoints tracking.api_*
  static    /tracking/static/...      tracking.css, tracking.js
  template  tracking.html             extends the host's _page.html

The host app supplies the layout (_page.html, _nav.html, _topbar.html), the
`is_test` and `dashboards` template globals, and any sign-in. Here that host is
../app.py, which has no sign-in, so keep it local; on the live site the team
login covers every route, these included.
"""

import json
import os
from datetime import date, datetime, timedelta

from flask import Blueprint, Response, current_app, jsonify, render_template, request, url_for

from .models import (
    init_db, SessionLocal, Company, Contact, Activity, RoleHook,
    get_setting,
)
from . import stages as ST
from .seed_data import seed_if_empty

bp = Blueprint(
    "tracking", __name__,
    template_folder="templates",
    static_folder="static",
    static_url_path="/tracking/static",
)


@bp.record_once
def _setup(state):
    """Create the tables (and run the one-time stage migration) when the
    blueprint is first registered, then import the workbook if the database is
    empty. Nothing else to call from the host app."""
    init_db()
    seed_if_empty()      # first launch only: import the workbook


@bp.app_template_global()
def tracking_static(filename):
    """This package's static URL with a cache-busting ?v=<mtime>, so browsers
    always fetch the current file. Named apart from the host's static_v."""
    try:
        ver = int(os.path.getmtime(os.path.join(bp.static_folder, filename)))
    except OSError:
        ver = 0
    return url_for("tracking.static", filename=filename) + f"?v={ver}"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _clean(v):
    return ("" if v is None else str(v)).strip()


def _parse_date(value):
    """'YYYY-MM-DD' -> date. Blank or unparseable -> None."""
    text = _clean(value)
    if not text:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _iso(d):
    return d.isoformat() if d else ""


def _days_since(when):
    if not when:
        return 0
    when = when.date() if isinstance(when, datetime) else when
    return max(0, (date.today() - when).days)


def _ageing(contact):
    """How stale a card is: cards sitting too long in one stage are the whole
    point of tracking the process. Agreed and closed contacts never age."""
    if contact.stage in ST.CLOSED_STAGES or contact.stage in ST.WON_STAGES:
        return "ok", 0
    days = _days_since(contact.stage_changed_at)
    if days >= ST.AGEING_STALE_DAYS:
        return "stale", days
    if days >= ST.AGEING_WARN_DAYS:
        return "warn", days
    return "ok", days


def _due_bucket(next_action_date):
    """Which slot of the numbers strip a next action falls in."""
    if not next_action_date:
        return ""
    today = date.today()
    if next_action_date < today:
        return "overdue"
    if next_action_date == today:
        return "today"
    if next_action_date <= today + timedelta(days=7):
        return "week"
    return "later"


def _parse_pct(value):
    """'35', '35%' or 35 -> 35, held to 0-100. Blank or unreadable -> None,
    which puts the contact back on its stage estimate."""
    text = _clean(value).rstrip("%").strip()
    if not text:
        return None
    try:
        return max(0, min(100, int(round(float(text)))))
    except ValueError:
        return None


def _chance(c):
    """The chance this contact ends with work agreed, and where the figure came
    from: 'won' once it has (100), 'outcome' once it has closed without work
    (Lost 0, Nurture none), 'own' when someone has set one, and 'estimate'
    when it is the stage's published average."""
    if c.stage in ST.WON_STAGES:
        return 100, "won"
    if c.stage in ST.CLOSED_STAGES:
        return {"Lost": 0}.get(c.outcome or ""), "outcome"
    if c.win_chance is not None:
        return c.win_chance, "own"
    return ST.WIN_CHANCE_ESTIMATE.get(c.stage), "estimate"


def _contact_dict(c, company_name=""):
    ageing, days = _ageing(c)
    chance, chance_source = _chance(c)
    return {
        "id": c.id,
        "company_id": c.company_id,
        "company": company_name,
        "priority": c.priority or "Medium",
        "sector": c.sector or "",
        "responsible": c.responsible or "",
        "summary": c.summary or "",
        "cost_area": c.cost_area or "",
        "target_role": c.target_role or "",
        "contact_name": c.contact_name or "",
        "linkedin_url": c.linkedin_url or "",
        "email": c.email or "",
        "phone": c.phone or "",
        "hook": c.hook or "",
        "stage": c.stage or 1,
        "stage_label": ST.stage_label(c.stage),
        "outcome": c.outcome or "",
        "win_chance": c.win_chance,
        "chance": chance,
        "chance_source": chance_source,
        "last_contact": _iso(c.last_contact),
        "next_action": c.next_action or "",
        "next_action_date": _iso(c.next_action_date),
        "notes": c.notes or "",
        "notes_on": _iso(c.notes_on),
        "days_in_stage": days,
        # The day it entered this stage, so a saved copy of the board can
        # count the days from whenever it is opened.
        "stage_since": _iso(c.stage_changed_at.date()) if c.stage_changed_at else "",
        "ageing": ageing,
        "due": _due_bucket(c.next_action_date),
    }


# The record's people table (8 Oct 2026): five people to contact at the
# company, each with a free-text contact status and a red / amber / green.
PEOPLE_SLOTS = 5
RAG = ("red", "amber", "green")


def _clean_people(value):
    """Five {name, status, rag} slots from whatever was sent: a list of
    dicts, or of plain names. Anything past five is dropped, short lists are
    padded with empty slots, and an unknown colour is no colour."""
    slots = []
    for item in (value if isinstance(value, list) else [])[:PEOPLE_SLOTS]:
        if not isinstance(item, dict):
            item = {"name": item}
        rag = _clean(item.get("rag")).lower()
        slots.append({"name": _clean(item.get("name")),
                      "status": _clean(item.get("status")),
                      "rag": rag if rag in RAG else ""})
    while len(slots) < PEOPLE_SLOTS:
        slots.append({"name": "", "status": "", "rag": ""})
    return slots


def _people_slots(co, contacts):
    """The company's five slots as saved. Until they are first saved, they
    offer the names already held on its contacts, so nothing has to be typed
    twice."""
    if co.people:
        try:
            return _clean_people(json.loads(co.people))
        except ValueError:
            pass
    names = []
    for c in sorted(contacts, key=lambda c: c.id):
        name = _clean(c.contact_name)
        if name and name.lower() not in {n.lower() for n in names}:
            names.append(name)
    return _clean_people(names)


def _company_dict(co, contacts=None):
    contacts = contacts or []
    open_ = [c for c in contacts
             if c.stage not in ST.CLOSED_STAGES and c.stage not in ST.WON_STAGES]
    return {
        "id": co.id,
        "name": co.name,
        "sub_sector": co.sub_sector or "",
        "ownership": co.ownership or "",
        "scale": co.scale or "",
        "why_fit": co.why_fit or "",
        "priority_functions": co.priority_functions or "",
        "warm_intro": co.warm_intro or "?",
        "website": co.website or "",
        "notes": co.notes or "",
        "company_number": co.company_number or "",
        "live_site_on": _iso(co.live_site_on),
        "people": _people_slots(co, contacts),
        "contact_count": len(contacts),
        "open_count": len(open_),
    }


def _hook_dict(h):
    return {
        "id": h.id, "tier": h.tier or "", "cost_area": h.cost_area or "",
        "role_titles": h.role_titles or "", "seniority": h.seniority or "",
        "benchmark": h.benchmark or "", "why_reachable": h.why_reachable or "",
        "proof_point": h.proof_point or "", "sharpness": h.sharpness or "",
    }


def _log(s, contact, kind, note="", from_stage=None, to_stage=None):
    """Insert one background Activity row (a stage move, or the contact being
    added). The page no longer shows these; they keep the stage timings."""
    a = Activity(
        contact_id=contact.id, kind=kind, note=note, happened_on=date.today(),
        from_stage=from_stage, to_stage=to_stage,
    )
    s.add(a)
    return a


def _apply(obj, fields, allowed, date_fields=(), pct_fields=()):
    """Write a {field: value} dict onto a row, one whitelist for the whole
    request. Replaces the old one-field-per-request endpoints, which is what
    lets the panel save a whole record in a single POST. Text is cut to its
    column's length: SQLite never minds, but Postgres (the live site) refuses
    the whole save over one long line."""
    for key, val in (fields or {}).items():
        key = _clean(key)
        if key in date_fields:
            setattr(obj, key, _parse_date(val))
        elif key in pct_fields:
            setattr(obj, key, _parse_pct(val))
        elif key in allowed:
            text = _clean(val)
            limit = getattr(obj.__table__.columns[key].type, "length", None)
            setattr(obj, key, text[:limit].rstrip() if limit else text)


def _fields_of(payload):
    """Accept either {fields: {...}} or the older {field, value} pair."""
    fields = payload.get("fields")
    if isinstance(fields, dict):
        return fields
    name = _clean(payload.get("field"))
    return {name: payload.get("value")} if name else {}


def _company_for_name(s, name):
    """Company id for a typed name, creating the account if it is new. Typing a
    company is how accounts actually get added, so there is no separate step."""
    if not name:
        return None
    co = s.query(Company).filter(Company.name.ilike(name)).first()
    if not co:
        co = Company(name=name)
        s.add(co)
        s.flush()
    return co.id


def _contact_payload(s, c):
    """The contact dict as the client should now hold it. Every mutating
    endpoint returns this, which is what removes the full-payload refetch."""
    s.flush()
    name = ""
    if c.company_id:
        co = s.get(Company, c.company_id)
        name = co.name if co else ""
    return _contact_dict(c, name)


# --------------------------------------------------------------------------- #
# The page - there is only one
# --------------------------------------------------------------------------- #
@bp.route("/tracking")
def page():
    return render_template("tracking.html")


# --------------------------------------------------------------------------- #
# API - one request boots the whole screen
# --------------------------------------------------------------------------- #
@bp.route("/api/tracking/data")
def api_data():
    payload = board_payload()
    # "Added to live site" (26 Oct 2026) is for a local copy whose lists are
    # moved up to the live site by hand. The live site itself leaves it off.
    payload["live_mark"] = bool(current_app.config.get("TRACKING_LIVE_SITE_MARK"))
    return jsonify(payload)


def board_payload():
    """Everything the board shows, as one dict. The page fetches it from
    /api/tracking/data; a host can also write it into a file (ARGUS's
    view-only "Tracking dashboard.html")."""
    with SessionLocal() as s:
        cos = {co.id: co for co in s.query(Company).all()}
        contacts = s.query(Contact).all()
        rows = [_contact_dict(c, cos[c.company_id].name if c.company_id in cos else "")
                for c in contacts]
        rows.sort(key=lambda r: (-r["stage"], r["company"], r["target_role"]))

        kids = {}
        for c in contacts:
            kids.setdefault(c.company_id, []).append(c)
        companies = sorted(
            [_company_dict(co, kids.get(co.id, [])) for co in cos.values()],
            key=lambda r: r["name"])

        payload = {
            "contacts": rows,
            "companies": companies,
            "stages": ST.STAGES,
            "closed_stages": ST.CLOSED_STAGES,
            "won_stages": ST.WON_STAGES,
            "last_open_stage": ST.LAST_OPEN_STAGE,
            "priorities": ST.PRIORITIES,
            "outcomes": ST.OUTCOMES,
            "hooks": [_hook_dict(h) for h in
                      s.query(RoleHook).order_by(RoleHook.tier, RoleHook.cost_area).all()],
            "playbook": get_setting(s, "playbook", []),
            "warm_note": get_setting(s, "warm_expand_note", ""),
            # offered when a sector tag is typed; the page adds the ones in use
            "sector_suggestions": ST.SECTOR_SUGGESTIONS + (get_setting(s, "sectors", []) or []),
            "win_chance_estimate": ST.WIN_CHANCE_ESTIMATE,
            "ageing": {"warn": ST.AGEING_WARN_DAYS, "stale": ST.AGEING_STALE_DAYS},
        }
    return payload


# --------------------------------------------------------------------------- #
# API - contacts
# --------------------------------------------------------------------------- #
CONTACT_FIELDS = {
    "priority", "sector", "cost_area", "target_role", "contact_name",
    "linkedin_url", "email", "phone", "hook", "next_action", "notes",
    "outcome", "responsible", "summary",
}
CONTACT_DATE_FIELDS = {"last_contact", "next_action_date"}
CONTACT_PCT_FIELDS = {"win_chance"}


@bp.route("/api/tracking/contact/add", methods=["POST"])
def api_contact_add():
    d = request.get_json(silent=True) or {}
    company_name = _clean(d.get("company"))
    if not company_name and not _clean(d.get("target_role")):
        return jsonify({"error": "A contact needs at least a company or a target role."}), 400
    with SessionLocal.begin() as s:
        company_id = _company_for_name(s, company_name)
        sector = _clean(d.get("sector"))
        if not sector and company_id:
            # Another card at the company lends its tag; the company's
            # sub-sector ("Care homes" since 26 Oct 2026) only when it has none.
            sib = (s.query(Contact).filter(Contact.company_id == company_id, Contact.sector != "")
                   .order_by(Contact.id).first())
            co = s.get(Company, company_id)
            sector = sib.sector if sib else ((co.sub_sector or "") if co else "")
        c = Contact(
            company_id=company_id,
            priority=_clean(d.get("priority")) or "Medium",
            sector=sector,
            responsible=_clean(d.get("responsible")),
            summary=_clean(d.get("summary")),
            cost_area=_clean(d.get("cost_area")),
            target_role=_clean(d.get("target_role")),
            contact_name=_clean(d.get("contact_name")),
            linkedin_url=_clean(d.get("linkedin_url")),
            email=_clean(d.get("email")),
            phone=_clean(d.get("phone")),
            hook=_clean(d.get("hook")),
            stage=int(d.get("stage") or 1),
            next_action=_clean(d.get("next_action")),
            next_action_date=_parse_date(d.get("next_action_date")),
            notes=_clean(d.get("notes")),
            notes_on=date.today() if _clean(d.get("notes")) else None,
            stage_changed_at=datetime.utcnow(),
        )
        s.add(c)
        s.flush()
        _log(s, c, "Note", note="Added to the tracker.")
        out = _contact_payload(s, c)
    return jsonify({"ok": True, "contact": out})


@bp.route("/api/tracking/contact/update", methods=["POST"])
def api_contact_update():
    """Edit a contact: {id, fields: {name: value, ...}}. Returns the updated
    record so the client patches one card instead of reloading the screen.
    Stage is not editable here; it has side effects, so it has its own route."""
    d = request.get_json(silent=True) or {}
    fields = _fields_of(d)
    for key in fields:
        if (key not in CONTACT_FIELDS and key not in CONTACT_DATE_FIELDS
                and key not in CONTACT_PCT_FIELDS and key != "company"):
            return jsonify({"error": f"Field '{key}' cannot be edited."}), 400
    outcome = _clean(fields.get("outcome"))
    if "outcome" in fields and outcome and outcome not in ST.OUTCOMES:
        return jsonify({"error": f"'{outcome}' is not a known outcome."}), 400
    with SessionLocal.begin() as s:
        c = s.get(Contact, int(d.get("id") or 0))
        if not c:
            return jsonify({"error": "No such contact."}), 404
        if "company" in fields:
            c.company_id = _company_for_name(s, _clean(fields["company"]))
        # The latest update is one comment that is replaced, not added to; its
        # date is what the card shows beside it.
        if "notes" in fields and _clean(fields["notes"]) != (c.notes or ""):
            c.notes_on = date.today() if _clean(fields["notes"]) else None
        _apply(c, fields, CONTACT_FIELDS, CONTACT_DATE_FIELDS, CONTACT_PCT_FIELDS)
        out = _contact_payload(s, c)
    return jsonify({"ok": True, "contact": out})


@bp.route("/api/tracking/contact/set_stage", methods=["POST"])
def api_contact_set_stage():
    """Move a contact along the pipeline. The one endpoint with side effects: it
    stamps stage_changed_at, moves last_contact to today on a forward move,
    clears the outcome when something is reopened, and writes the 'Stage change'
    activity row, all in one transaction."""
    d = request.get_json(silent=True) or {}
    try:
        new_stage = int(d.get("stage"))
    except (TypeError, ValueError):
        return jsonify({"error": "Bad stage."}), 400
    if new_stage not in ST.STAGE_BY_NUM:
        return jsonify({"error": "Bad stage."}), 400
    outcome = _clean(d.get("outcome"))
    if outcome and outcome not in ST.OUTCOMES:
        return jsonify({"error": f"'{outcome}' is not a known outcome."}), 400
    with SessionLocal.begin() as s:
        c = s.get(Contact, int(d.get("id") or 0))
        if not c:
            return jsonify({"error": "No such contact."}), 404
        old = c.stage
        if old != new_stage:
            c.stage = new_stage
            c.stage_changed_at = datetime.utcnow()
            # Moving forward is a touch; moving back (a correction) is not.
            if new_stage > old:
                c.last_contact = date.today()
            if new_stage in ST.CLOSED_STAGES:
                c.outcome = outcome or c.outcome or ""
            else:
                c.outcome = ""
            _log(s, c, "Stage change", from_stage=old, to_stage=new_stage)
        elif outcome:
            c.outcome = outcome
        out = _contact_payload(s, c)
    return jsonify({"ok": True, "contact": out})


@bp.route("/api/tracking/contact/delete", methods=["POST"])
def api_contact_delete():
    d = request.get_json(silent=True) or {}
    with SessionLocal.begin() as s:
        c = s.get(Contact, int(d.get("id") or 0))
        if c:
            s.query(Activity).filter(Activity.contact_id == c.id).delete()
            s.delete(c)
    return jsonify({"ok": True})


# --------------------------------------------------------------------------- #
# API - companies
#
# There is no add or delete route: typing a company name on a contact creates
# the account, which is the only way one was ever really created.
# --------------------------------------------------------------------------- #
COMPANY_FIELDS = {
    "name", "sub_sector", "ownership", "scale", "why_fit",
    "priority_functions", "warm_intro", "website", "notes",
}
COMPANY_DATE_FIELDS = {"live_site_on"}


@bp.route("/api/tracking/company/update", methods=["POST"])
def api_company_update():
    """Edit a company: {id, fields: {name: value, ...}}. `people` is the
    record's whole five-row table, saved together, so the client always sends
    every slot."""
    d = request.get_json(silent=True) or {}
    fields = _fields_of(d)
    for key in fields:
        if key not in COMPANY_FIELDS | COMPANY_DATE_FIELDS and key != "people":
            return jsonify({"error": f"Field '{key}' cannot be edited."}), 400
    with SessionLocal.begin() as s:
        co = s.get(Company, int(d.get("id") or 0))
        if not co:
            return jsonify({"error": "No such company."}), 404
        if "people" in fields:
            co.people = json.dumps(_clean_people(fields["people"]))
        _apply(co, fields, COMPANY_FIELDS, date_fields=COMPANY_DATE_FIELDS)
        s.flush()
        kids = s.query(Contact).filter(Contact.company_id == co.id).all()
        out = _company_dict(co, kids)
    return jsonify({"ok": True, "company": out})


# --------------------------------------------------------------------------- #
# API - the Role & Hook library
# --------------------------------------------------------------------------- #
HOOK_FIELDS = {
    "tier", "cost_area", "role_titles", "seniority", "benchmark",
    "why_reachable", "proof_point", "sharpness",
}


@bp.route("/api/tracking/library/update", methods=["POST"])
def api_library_update():
    d = request.get_json(silent=True) or {}
    fields = _fields_of(d)
    for key in fields:
        if key not in HOOK_FIELDS:
            return jsonify({"error": f"Field '{key}' cannot be edited."}), 400
    with SessionLocal.begin() as s:
        h = s.get(RoleHook, int(d.get("id") or 0))
        if not h:
            return jsonify({"error": "No such library row."}), 404
        _apply(h, fields, HOOK_FIELDS)
        s.flush()
        out = _hook_dict(h)
    return jsonify({"ok": True, "hook": out})


# --------------------------------------------------------------------------- #
# The board as a CSV, and companies added from one (csvio.py, 20 Oct 2026).
# csvio imports this module's helpers, so it is imported inside each route.
# --------------------------------------------------------------------------- #
@bp.route("/api/tracking/export.csv")
def api_export_csv():
    from . import csvio
    name = f"Tracker {date.today():%Y-%m-%d}.csv"
    return Response(csvio.write(csvio.board_rows()), mimetype="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@bp.route("/api/tracking/import", methods=["POST"])
def api_import_csv():
    from . import csvio
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify({"error": "Choose a CSV or Excel file first."}), 400
    try:
        result = csvio.import_file(f.read())
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify({"ok": True, **result})
