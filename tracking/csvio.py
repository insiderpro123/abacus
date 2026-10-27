"""
The tracker as a CSV: download the board, or add companies from a file.

Asked for on 20 Oct 2026 so a list built on one copy of the tracker (ARGUS's,
on the PC) can be moved to another (the live process dashboard): "run it
locally and get a list then move it to the live site". The same file format
comes out of ARGUS's "Download for the tracker" (argus/tracker_link.py builds
those rows with lead_row below), so either kind of file can be dropped in.

One row per contact, with the company's details repeated on each. Import only
ever ADDS: a company already on the board (same Companies House number, or
the same name) keeps everything it has, so dropping the same file in twice
changes nothing. Since 27 Oct 2026 the one exception is a blank: a company
already there gets the key facts it is missing, its blank details, and the
file's Suitable? answer if it has none of its own (facts.fill_blanks), so a
list moved to the live site before the facts existed can be topped up. Rows
for one company inside one file become that company's contacts. Every write
stays in this package, as with intake.py.

The key facts (facts.FACTS) go one column each under their own labels, then
"Facts as of"; the decision is "Suitable?" (Yes / No / blank), "Why suitable
or not", "Decided by" and "Decided on". "Companies House link" is written for
reading only; the number is what an import goes by.

Since 20 Oct 2026 an Excel file can be dropped in too: ARGUS's "companies to
contact" workbook, a tab per area, read top to bottom. It has an "Add to live
site?" column, and only the rows marked Yes are added, so a list can be
screened in Excel and moved across a few companies at a time. Three of its
headers are worded for reading (PLAIN_COLUMNS) and fill a tracker field when
the file has no column of the tracker's own name.
"""

import csv
import io
import json
import re
from datetime import date, datetime

from .models import SessionLocal, Company, Contact
from .routes import (_clean, _clean_people, _log, _parse_date, _parse_pct, _people_slots,
                     PEOPLE_SLOTS)
from . import facts as FX, stages as ST

# (header, where it lives). Headers are the words a person sees in Excel; the
# import reads them back case-insensitively, in any order, with any missing.
COMPANY_COLUMNS = [
    ("Company", "name"),
    ("Companies House number", "company_number"),
]
CONTACT_COLUMNS = [
    ("Stage", "stage"),
    ("Outcome", "outcome"),
    ("Chance %", "win_chance"),
    ("Priority", "priority"),
]
# What the card itself shows (20 Oct 2026). "Sector tag" is the card's own;
# "Sub-sector" below is the company's ("Care homes"). Files from before 26 Oct
# 2026 called that column "Sector" and the import still reads them that way
# (LEGACY_SUB_SECTOR); a file with both, ARGUS's Excel, means the card's sector.
CARD_COLUMNS = [
    ("Responsible", "responsible"),
    ("Sector tag", "sector"),
    ("Summary", "summary"),
]
COMPANY_DETAIL_COLUMNS = [
    ("Sub-sector", "sub_sector"),
    ("Size", "scale"),
    ("Ownership", "ownership"),
    ("Why they fit", "why_fit"),
    ("Functions to target", "priority_functions"),
    ("Website", "website"),
]
CONTACT_DETAIL_COLUMNS = [
    ("Contact name", "contact_name"),
    ("Role to target", "target_role"),
    ("Cost area", "cost_area"),
    ("LinkedIn", "linkedin_url"),
    ("Email", "email"),
    ("Phone", "phone"),
    ("Hook", "hook"),
]
COMMENT_COLUMNS = [("Comments", "notes")]
# Whether the company is suitable to work with, and why (27 Oct 2026).
SUITABLE_COLUMNS = [
    ("Suitable?", "suitable"),
    ("Why suitable or not", "suitable_why"),
    ("Decided by", "suitable_by"),
    ("Decided on", "suitable_on"),
]
CH_LINK = "Companies House link"
FACT_COLUMNS = [(label, key) for key, label in FX.FACTS]
FACTS_ON = "Facts as of"
PEOPLE_COLUMNS = [h for i in range(1, PEOPLE_SLOTS + 1)
                  for h in (f"Person {i}", f"Person {i} status", f"Person {i} RAG")]

HEADERS = ([h for h, _ in COMPANY_COLUMNS + CONTACT_COLUMNS + CARD_COLUMNS
            + COMPANY_DETAIL_COLUMNS + CONTACT_DETAIL_COLUMNS + COMMENT_COLUMNS
            + SUITABLE_COLUMNS]
           + [CH_LINK] + [h for h, _ in FACT_COLUMNS] + [FACTS_ON]
           + PEOPLE_COLUMNS)

COMPANY_FIELDS = dict(COMPANY_COLUMNS + COMPANY_DETAIL_COLUMNS + COMMENT_COLUMNS)
CONTACT_FIELDS = dict(CARD_COLUMNS + CONTACT_DETAIL_COLUMNS)

RAG_WORDS = {"red": "red", "r": "red", "amber": "amber", "a": "amber",
             "green": "green", "g": "green"}

# ARGUS's companies-to-contact workbook: the rows to add carry a Yes here.
PICK_COLUMN = "Add to live site?"
YES_WORDS = {"yes", "y", "x", "true", "1", "✓", "✔"}
NO_WORDS = {"no", "n", "false", "0", "✗", "✘"}
# Its score and verdict columns, read as those facts when a file has no
# "ARGUS score" / "ARGUS verdict" column of its own.
FACT_ALIASES = {"score": "Score (out of 100)", "verdict": "Verdict"}
# Its plainer headers: "Why now" lists the reasons one a line (the tracker's
# "Why they fit"), "What ARGUS found" is the evidence (the "Hook", which ARGUS
# heads "What ARGUS found:"), "Other directors" follow the contact into the
# people slots.
PLAIN_COLUMNS = ("Why now", "What ARGUS found", "Other directors")
HOOK_HEAD = "What ARGUS found:"
LEGACY_SUB_SECTOR = "Sector"
# Cells whose link matters more than the word shown ("Search LinkedIn").
LINK_COLUMNS = ("LinkedIn", "Website")


# --------------------------------------------------------------- writing

def write(rows) -> bytes:
    """Rows ({header: value}) as CSV bytes. The BOM makes Excel read the
    pound signs as UTF-8, the same as ARGUS's other CSVs."""
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=HEADERS, extrasaction="ignore", lineterminator="\r\n")
    w.writeheader()
    for row in rows:
        w.writerow({h: ("" if v is None else v) for h, v in row.items()})
    return ("﻿" + out.getvalue()).encode("utf-8")


def _people_cells(people) -> dict:
    cells = {}
    for i, p in enumerate(_clean_people(people), start=1):
        cells[f"Person {i}"] = p["name"]
        cells[f"Person {i} status"] = p["status"]
        cells[f"Person {i} RAG"] = p["rag"]
    return cells


def _facts_cells(number, facts, facts_on) -> dict:
    """The Companies House link, the key facts and their date as cells."""
    cells = {CH_LINK: FX.companies_house_url(number)}
    have = FX.clean_facts(facts)
    cells.update({h: have.get(key, "") for h, key in FACT_COLUMNS})
    cells[FACTS_ON] = facts_on.isoformat() if isinstance(facts_on, date) else _clean(facts_on)
    return cells


def _suitable_cells(suitable, why, by, on) -> dict:
    return {"Suitable?": {"yes": "Yes", "no": "No"}.get(suitable or "", ""),
            "Why suitable or not": why or "", "Decided by": by or "",
            "Decided on": on.isoformat() if on else ""}


def board_rows() -> list[dict]:
    """Every contact on the board as a row, company details repeated, best
    stage first. A company with no contact still gets one row."""
    rows = []
    with SessionLocal() as s:
        contacts = s.query(Contact).all()
        kids = {}
        for c in contacts:
            kids.setdefault(c.company_id, []).append(c)
        for co in sorted(s.query(Company).all(), key=lambda co: (co.name or "").lower()):
            base = {h: getattr(co, f) or "" for h, f in COMPANY_FIELDS.items()}
            base.update(_suitable_cells(co.suitable, co.suitable_why, co.suitable_by, co.suitable_on))
            base.update(_facts_cells(co.company_number, co.facts, co.facts_on))
            base.update(_people_cells(_people_slots(co, kids.get(co.id, []))))
            mine = sorted(kids.get(co.id, []), key=lambda c: (-(c.stage or 1), c.id))
            for c in mine or [None]:
                row = dict(base)
                if c is not None:
                    row.update({h: getattr(c, f) or "" for h, f in CONTACT_FIELDS.items()})
                    row["Stage"] = ST.STAGE_BY_NUM.get(c.stage or 1, ST.STAGES[0])["label"]
                    row["Outcome"] = c.outcome or ""
                    row["Chance %"] = "" if c.win_chance is None else c.win_chance
                    row["Priority"] = c.priority or "Medium"
                rows.append(row)
    return rows


def lead_row(company: dict, contact: dict) -> dict:
    """One row from intake.add_lead's two dicts, so a lead finder's file and
    "Add to tracker" carry exactly the same fields."""
    row = {h: company.get(f, "") for h, f in COMPANY_FIELDS.items()}
    row.update({h: contact.get(f, "") for h, f in CONTACT_FIELDS.items()})
    row["Stage"] = ST.STAGES[0]["label"]
    row["Priority"] = contact.get("priority") or "Medium"
    row.update(_facts_cells(company.get("company_number"), company.get("facts"),
                            company.get("facts_on")))
    row.update(_people_cells(company.get("people") or []))
    return row


# --------------------------------------------------------------- reading

def _decode(raw: bytes) -> str:
    """Excel saves "CSV UTF-8" with a BOM and plain "CSV" in the Windows code
    page, so try the one and fall back to the other (which reads any byte)."""
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def _norm(header: str) -> str:
    return re.sub(r"\s+", " ", (header or "").replace("﻿", "")).strip().lower()


def company_number(value) -> str:
    """As Companies House writes it. Excel turns 01603760 into 1603760 when it
    opens a CSV, so an all-digit number gets its leading zeros back."""
    text = re.sub(r"\s+", "", _clean(value)).upper()
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    if text.isdigit() and len(text) < 8:
        text = text.zfill(8)
    return text


def _fit(model, field: str, value: str) -> str:
    """Trim a value to its column's length. SQLite never enforces a length,
    but Postgres (the live site) refuses the whole import over one long
    ownership line, so a value too long is cut, and says so with "..."."""
    limit = getattr(model.__table__.columns[field].type, "length", None)
    if limit and len(value) > limit:
        return value[:limit - 3].rstrip() + "..."
    return value


def _stage(value) -> int:
    text = _clean(value).lower()
    if not text:
        return 1
    m = re.match(r"^(\d)\b", text)            # "2", or "2. Contacted" as the page labels it
    if m and int(m.group(1)) in ST.STAGE_BY_NUM:
        return int(m.group(1))
    text = re.sub(r"^\d\.\s*", "", text)
    for s in ST.STAGES:
        if text in (s["label"].lower(), s["short"].lower()):
            return s["num"]
    return ST.FORMER_LABELS.get(text, 1)


def _csv_tables(raw: bytes) -> list:
    """The file as one (header, rows) table."""
    text = _decode(raw)
    first = text.splitlines()[0] if text else ""
    # A UK Excel writes commas; some European set-ups write semicolons.
    delimiter = ";" if first.count(";") > first.count(",") else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError("The file is empty.")
    return [(header, reader)]


def _cell_text(cell, link: bool = False) -> str:
    """An Excel cell as the text a CSV would hold. A link column gives the
    address behind the word shown."""
    target = getattr(getattr(cell, "hyperlink", None), "target", None)
    if link and target:
        return target
    value = cell.value
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)            # a company number Excel holds as 1603760.0
    if isinstance(value, datetime):
        value = value.date() if value.time() == datetime.min.time() else value
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _xlsx_tables(raw: bytes) -> list:
    """Each tab as a (header, rows) table. The header is the first row in a
    tab's top ten with a Company cell, since ARGUS's tabs open with a title
    and a how-to line. A tab with no such row (a notes page) is passed over."""
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
    except Exception as exc:
        raise ValueError("That Excel file could not be read. Save it as an .xlsx "
                         "workbook and drop it in again.") from exc
    links = {_norm(h) for h in LINK_COLUMNS}
    tables = []
    for ws in wb.worksheets:
        rows = ws.iter_rows()
        header = None
        for cells in rows:
            texts = [_cell_text(c) for c in cells]
            if "company" in {_norm(t) for t in texts}:
                header = texts
                break
            if cells[0].row >= 10:
                break
        if header is None:
            continue
        linked = {i for i, h in enumerate(header) if _norm(h) in links}
        tables.append((header, [[_cell_text(c, i in linked) for i, c in enumerate(cells)]
                                for cells in rows]))
    return tables


def _lines(text: str, commas: bool = False) -> list[str]:
    """One item a line ("Why now", "Other directors"); a semicolon, or for
    names a comma, also splits, for a list someone typed on one line."""
    parts = re.split(r"[\n;,]+" if commas else r"[\n;]+", text or "")
    return [p.strip(" -•\t") for p in parts if p.strip(" -•\t")]


def _suitable(value) -> str:
    """'Yes' / 'Suitable' -> 'yes', 'No' / 'Not suitable' -> 'no', else ''."""
    text = _clean(value).lower()
    if text in YES_WORDS or text == "suitable":
        return "yes"
    if text in NO_WORDS or text in ("not suitable", "unsuitable"):
        return "no"
    return ""


def import_file(raw: bytes) -> dict:
    """Add the companies in a CSV, or in an Excel file's tabs, as leads.
    Returns what happened, for the page to say: companies added, contacts
    added, companies already on the board (kept as they are), those of them
    whose blanks were filled in, rows skipped, and, for a file with an "Add
    to live site?" column, the rows not marked Yes."""
    excel = raw[:4] == b"PK\x03\x04"           # an .xlsx is a zip
    tables = _xlsx_tables(raw) if excel else _csv_tables(raw)
    if not any("company" in {_norm(h) for h in header} for header, _ in tables):
        raise ValueError('No "Company" column. Use a file downloaded from a tracker '
                         'or from ARGUS ("Download for the tracker", or the Excel of '
                         'companies to contact).')

    added, already, filled, skipped, contacts = [], [], [], 0, 0
    picking, marked, unmarked = False, 0, 0
    with SessionLocal.begin() as s:
        here = {}            # this file's companies: key -> Company, or None when already on the board
        for header, rows in tables:
            col = {_norm(h): i for i, h in enumerate(header)}
            if _norm("Sub-sector") not in col and _norm(LEGACY_SUB_SECTOR) in col:
                col[_norm("Sub-sector")] = col.pop(_norm(LEGACY_SUB_SECTOR))
            pick = col.get(_norm(PICK_COLUMN))
            picking = picking or pick is not None
            plain = {h: _norm(h) in col for h in PLAIN_COLUMNS}

            def cell(values, name):
                i = col.get(_norm(name))
                return _clean(values[i]) if i is not None and i < len(values) else ""

            def company_cell(values, header, field):
                return _fit(Company, field, cell(values, header))

            def contact_cell(values, header, field):
                return _fit(Contact, field, cell(values, header))

            def company_fields(values):
                fields = {f: company_cell(values, h, f) for h, f in COMPANY_FIELDS.items()}
                if not fields["why_fit"] and plain["Why now"]:
                    fields["why_fit"] = _fit(Company, "why_fit", "; ".join(_lines(cell(values, "Why now"))))
                return fields

            def row_facts(values):
                facts = {key: cell(values, label) for label, key in FACT_COLUMNS}
                for key, alias in FACT_ALIASES.items():
                    label = dict(FX.FACTS)[key]
                    if _norm(label) not in col and cell(values, alias):
                        facts[key] = cell(values, alias)
                        if key == "score":
                            facts[key] += " of 100"
                return FX.clean_facts(facts)

            def row_decision(values):
                answer = _suitable(cell(values, "Suitable?"))
                return {"suitable": answer,
                        "suitable_why": cell(values, "Why suitable or not"),
                        "suitable_by": cell(values, "Decided by"),
                        "suitable_on": _parse_date(cell(values, "Decided on"))} if answer else None

            for values in rows:
                name = company_cell(values, "Company", "name")
                if not name:
                    skipped += 1 if any(_clean(v) for v in values) else 0
                    continue
                if pick is not None:
                    if cell(values, PICK_COLUMN).lower() not in YES_WORDS:
                        unmarked += 1
                        continue
                    marked += 1
                number = _fit(Company, "company_number",
                              company_number(cell(values, "Companies House number")))
                key = number or name.lower()
                facts_on = _parse_date(cell(values, FACTS_ON))
                if key not in here:
                    co = None
                    if number:
                        co = s.query(Company).filter(Company.company_number == number).first()
                    if co is None:
                        co = s.query(Company).filter(Company.name.ilike(name)).first()
                    if co is not None:
                        # Kept as it is, bar its blanks (27 Oct 2026).
                        here[key] = None
                        already.append(co.name)
                        if FX.fill_blanks(co, {**company_fields(values), "company_number": number},
                                          row_facts(values), facts_on, row_decision(values)):
                            filled.append(co.name)
                        continue
                    co = Company(**company_fields(values))
                    co.name, co.company_number = name, number
                    FX.fill_blanks(co, None, row_facts(values), facts_on, row_decision(values))
                    people = [{"name": cell(values, f"Person {i}"),
                               "status": cell(values, f"Person {i} status"),
                               "rag": RAG_WORDS.get(cell(values, f"Person {i} RAG").lower(), "")}
                              for i in range(1, PEOPLE_SLOTS + 1)]
                    if not any(p["name"] or p["status"] for p in people) and plain["Other directors"]:
                        names = []
                        for n in [cell(values, "Contact name")] + _lines(cell(values, "Other directors"), True):
                            if n and n.lower() not in {x.lower() for x in names}:
                                names.append(n)
                        people = [{"name": n, "status": "", "rag": ""} for n in names]
                    if any(p["name"] or p["status"] for p in people):
                        co.people = json.dumps(_clean_people(people))
                    s.add(co)
                    s.flush()
                    here[key] = co
                    added.append(name)
                co = here[key]
                if co is None:
                    continue
                stage = _stage(cell(values, "Stage"))
                outcome = cell(values, "Outcome").capitalize()
                priority = cell(values, "Priority").capitalize()
                c = Contact(
                    company_id=co.id, stage=stage,
                    outcome=outcome if stage in ST.CLOSED_STAGES and outcome in ST.OUTCOMES else "",
                    win_chance=_parse_pct(cell(values, "Chance %")),
                    priority=priority if priority in ST.PRIORITIES else "Medium",
                    stage_changed_at=datetime.utcnow(),
                    **{f: contact_cell(values, h, f) for h, f in CONTACT_FIELDS.items()})
                # No "Sector tag": ARGUS's Excel has "Sector" beside "Sub-sector";
                # a file from before the tag column had only the company's.
                c.sector = (c.sector or _fit(Contact, "sector", cell(values, LEGACY_SUB_SECTOR))
                            or co.sub_sector or "")
                found = cell(values, "What ARGUS found")
                if not c.hook and plain["What ARGUS found"] and found:
                    c.hook = found if found.startswith(HOOK_HEAD) else f"{HOOK_HEAD}\n{found}"
                s.add(c)
                s.flush()
                _log(s, c, "Note", note=f"Added from {'an Excel' if excel else 'a CSV'} import.")
                contacts += 1
    return {"added": added, "contacts": contacts, "already": already, "filled": filled,
            "skipped": skipped, "pick_column": picking, "marked": marked, "unmarked": unmarked}
