"""
One-time import of InsiderPro_Contact_Tracker.xlsx into the CRM database.

Reads four tabs of the workbook and writes them into the tables:
    Target Companies    -> Company   (15 rows)
    Contact Tracker     -> Contact   (17 rows, linked to their Company by name)
    Role & Hook Library -> RoleHook  (15 rows)
    How to Use          -> Setting['playbook'] (the play, the steps, the cadence)

After this runs the app owns the data and the workbook is never touched again.

Idempotent: does nothing if any Contact already exists, so app.py can call it on
every start and only the first launch actually imports. Pass --force to wipe the
seeded tables and re-import.

Run it from the app root (the folder holding this package):

    python -m tracking.seed_data                 # seed if empty
    python -m tracking.seed_data --force         # wipe and re-seed
    python -m tracking.seed_data path/to.xlsx    # seed from a workbook elsewhere
"""

import os
import sys
from datetime import date, datetime

from .models import (
    SessionLocal, init_db, Company, Contact, Activity, RoleHook,
    set_setting, ROOT,
)
from .stages import stage_from_excel, outcome_from_excel

DEFAULT_WORKBOOK = os.path.join(ROOT, "InsiderPro_Contact_Tracker.xlsx")


# --------------------------------------------------------------------------- #
# Sheet reading helpers
# --------------------------------------------------------------------------- #
def _clean(v):
    """Normalise a cell to a stripped string ('' for blanks)."""
    if v is None:
        return ""
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v).strip()


def _as_date(v):
    """Cell -> date, for the two date columns. Blank or unparseable gives None."""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    text = _clean(v)
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(text[:10] if fmt == "%Y-%m-%d" else text, fmt).date()
        except ValueError:
            continue
    return None


def _key(name):
    """Match key for a company name.

    The two sheets do not always spell a group identically: the Contact Tracker
    says 'Group Name' where the target list says 'Group Name / Partner'. Keying on the part before a ' / ' separator, lower-cased, links the two
    instead of creating a duplicate account.
    """
    return str(name or "").split("/")[0].strip().lower()


def _rows_below_header(ws, first_header):
    """Yield the data rows of a sheet.

    The workbook puts a title and a hint line above each table, so the header is
    not row 1. Find the row whose first cell is `first_header`, then yield every
    following row until the first blank first-cell (which also skips the footnote
    paragraph that sits below a blank line on the Target Companies tab).
    """
    rows = list(ws.iter_rows(values_only=True))
    start = None
    for i, row in enumerate(rows):
        if row and _clean(row[0]).lower() == first_header.lower():
            start = i + 1
            break
    if start is None:
        return
    for row in rows[start:]:
        if not row or not _clean(row[0]):
            return
        yield row


# --------------------------------------------------------------------------- #
# The import
# --------------------------------------------------------------------------- #
def seed(workbook=None, force=False, quiet=False):
    """Import the workbook. Returns a dict of what was written (empty if skipped)."""
    import openpyxl

    path = workbook or DEFAULT_WORKBOOK
    init_db()

    def say(msg):
        if not quiet:
            print(msg)

    with SessionLocal.begin() as s:
        existing = s.query(Contact).count()
        if existing and not force:
            say(f"Database already holds {existing} contacts - nothing to do "
                f"(use --force to re-import).")
            return {}
        if force and existing:
            say("--force: clearing seeded tables ...")
            s.query(Activity).delete()
            s.query(Contact).delete()
            s.query(Company).delete()
            s.query(RoleHook).delete()

    if not os.path.exists(path):
        raise SystemExit(f"Workbook not found: {path}")

    wb = openpyxl.load_workbook(path, data_only=True)
    counts = {"companies": 0, "contacts": 0, "hooks": 0}

    with SessionLocal.begin() as s:
        # ---- Target Companies -------------------------------------------- #
        by_name = {}
        if "Target Companies" in wb.sheetnames:
            for row in _rows_below_header(wb["Target Companies"], "Company"):
                r = [_clean(c) for c in row] + [""] * 8
                c = Company(
                    name=r[0], sub_sector=r[1], ownership=r[2], scale=r[3],
                    why_fit=r[4], priority_functions=r[5],
                    warm_intro=r[6] or "?", status=r[7] or "Not started",
                )
                s.add(c)
                s.flush()
                by_name[_key(c.name)] = c
                counts["companies"] += 1

        # ---- Contact Tracker --------------------------------------------- #
        if "Contact Tracker" in wb.sheetnames:
            for row in _rows_below_header(wb["Contact Tracker"], "ID"):
                r = list(row) + [None] * 17
                name = _clean(r[2])
                company = by_name.get(_key(name))
                if not company and name:
                    # A contact at a company that is not on the target list yet:
                    # create the account so nothing is dropped on import.
                    company = Company(name=name, sub_sector=_clean(r[3]))
                    s.add(company)
                    s.flush()
                    by_name[_key(name)] = company
                    counts["companies"] += 1
                # The workbook still spells the status as one of the original
                # twelve stages, so it is split into a stage plus an outcome.
                stage = stage_from_excel(r[11])
                outcome = outcome_from_excel(r[11])
                s.add(Contact(
                    company_id=company.id if company else None,
                    priority=_clean(r[1]) or "Medium",
                    sector=_clean(r[3]),
                    cost_area=_clean(r[4]),
                    target_role=_clean(r[5]),
                    contact_name=_clean(r[6]),
                    linkedin_url=_clean(r[7]),
                    email=_clean(r[8]),
                    phone=_clean(r[9]),
                    hook=_clean(r[10]),
                    stage=stage,
                    outcome=outcome,
                    last_contact=_as_date(r[13]),
                    next_action=_clean(r[14]),
                    next_action_date=_as_date(r[15]),
                    notes=_clean(r[16]),
                ))
                counts["contacts"] += 1

        # ---- Role & Hook Library ----------------------------------------- #
        if "Role & Hook Library" in wb.sheetnames:
            for row in _rows_below_header(wb["Role & Hook Library"], "Tier"):
                r = [_clean(c) for c in row] + [""] * 8
                s.add(RoleHook(
                    tier=r[0], cost_area=r[1], role_titles=r[2], seniority=r[3],
                    benchmark=r[4], why_reachable=r[5], proof_point=r[6],
                    sharpness=r[7],
                ))
                counts["hooks"] += 1

        # ---- How to Use -> the playbook page ----------------------------- #
        if "How to Use" in wb.sheetnames:
            ws = wb["How to Use"]
            play = []
            for row in ws.iter_rows(min_row=4, values_only=True):
                title, body = _clean(row[0] if row else ""), _clean(row[1] if len(row) > 1 else "")
                if title and body:
                    play.append({"title": title, "body": body})
            if play:
                set_setting(s, "playbook", play)

        # ---- Vocabularies for the filter dropdowns ----------------------- #
        contacts = s.query(Contact).all()
        sectors = sorted({c.sector for c in contacts if c.sector})
        cost_areas = sorted({c.cost_area for c in contacts if c.cost_area} |
                            {h.cost_area for h in s.query(RoleHook).all() if h.cost_area})
        set_setting(s, "sectors", sectors)
        set_setting(s, "cost_areas", cost_areas)
        set_setting(s, "warm_expand_note",
                    "Warm expand list (existing ISP clients - lead with the "
                    "relationship, not a cold benchmark): mydentist, Practice Plus "
                    "Group, itsu, Kerv Group, SK Aerosafety, Bridgepoint portfolio "
                    "companies.")

    say(f"Imported {counts['companies']} companies, {counts['contacts']} contacts, "
        f"{counts['hooks']} library rows from {os.path.basename(path)}.")
    return counts


def seed_if_empty(quiet=True):
    """Called when the blueprint is registered: import only when the database has no
    contacts. On the live site the workbook is not there (git ignores .xlsx), so
    this does nothing and the board starts empty."""
    try:
        with SessionLocal() as s:
            if s.query(Contact).count():
                return {}
        if not os.path.exists(DEFAULT_WORKBOOK):
            return {}
        return seed(quiet=quiet)
    except Exception as exc:                      # never block the app from starting
        print(f"[seed] skipped: {exc}")
        return {}


if __name__ == "__main__":
    args = [a for a in sys.argv[1:]]
    force = "--force" in args
    paths = [a for a in args if not a.startswith("--")]
    seed(workbook=paths[0] if paths else None, force=force)
