"""
What anyone opening a company's record needs to judge it: its key facts, and
whether it is suitable to work with, with the reasons written down.

Asked for on 27 Oct 2026: a company moved onto the tracker still has to be
checked by hand as suitable to work with, so the record carries the facts that
decision rests on (the Companies House link, the turnover and the rest), and
the decision itself with its reasons, so that someone who has never seen the
company before can see why it was chosen, or why not.

The facts come from a lead finder (ARGUS's "Add to tracker", its CSVs and its
Excel) and are a snapshot, dated by Company.facts_on; the page only reads
them. Each is plain text, one line, or one item a line for the lists
(Reasons to call ...). They are stored as JSON under the keys in FACTS, so a
new fact needs no new column, and their labels are also the CSV headers, so a
file carries them from one tracker to the other.
"""

import json

# (key, label) in the order the record shows them. The label is the CSV header.
FACTS = [
    ("turnover", "Turnover"),
    ("profit", "Operating profit"),
    ("staff", "Staff"),
    ("incorporated", "Incorporated"),
    ("office", "Registered office"),
    ("runs", "What it runs"),
    ("accounts", "Latest accounts"),
    ("score", "ARGUS score"),
    ("verdict", "ARGUS verdict"),
    ("red_flags", "Red flags"),
    ("to_call", "Reasons to call"),
    ("to_wait", "Reasons to wait"),
    ("worth_knowing", "Worth knowing"),
]
FACT_KEYS = [k for k, _ in FACTS]

# Whether the company is suitable to work with. Blank is "not decided yet".
SUITABLE = {"yes": "Suitable", "no": "Not suitable"}

# The company's fields a later hand-over may fill in when they are still
# blank (fill_blanks). Never the name or the comments, which people type.
BLANK_FILLABLE = ("company_number", "sub_sector", "ownership", "scale", "why_fit",
                  "priority_functions", "website")

CH_COMPANY_URL = "https://find-and-update.company-information.service.gov.uk/company/{}"


def companies_house_url(number) -> str:
    number = ("" if number is None else str(number)).strip()
    return CH_COMPANY_URL.format(number) if number else ""


def clean_facts(value) -> dict:
    """{key: text} for the known keys with something in them, whatever was
    sent: a dict, or the JSON of one. Lines are trimmed, blank ones dropped."""
    if isinstance(value, str):
        try:
            value = json.loads(value) if value.strip() else {}
        except ValueError:
            value = {}
    if not isinstance(value, dict):
        return {}
    out = {}
    for key in FACT_KEYS:
        text = "\n".join(line.strip() for line in str(value.get(key) or "").splitlines()
                         if line.strip())
        if text:
            out[key] = text
    return out


def facts_of(co) -> dict:
    return clean_facts(co.facts or "")


def facts_list(co) -> list:
    """The company's facts as the page shows them: [{key, label, value}] in
    FACTS order, the empty ones left out."""
    have = facts_of(co)
    return [{"key": k, "label": label, "value": have[k]} for k, label in FACTS if k in have]


def _fit(co, field, text):
    """Cut to the column's length: SQLite never minds, Postgres refuses."""
    limit = getattr(co.__table__.columns[field].type, "length", None)
    return text[:limit].rstrip() if limit else text


def _says_nothing(co, key) -> bool:
    """Whether a company field is as good as blank. Empty, or, for the
    sub-sector, only the card's sector tag again: companies moved to the live
    site from files made before 26 Oct 2026 hold "Healthcare" there, which the
    card hides as a repeat of its tag, so a real one ("Care homes") may
    replace it (27 Oct 2026)."""
    have = (getattr(co, key) or "").strip()
    if not have:
        return True
    if key == "sub_sector":
        return have.lower() in {(c.sector or "").strip().lower() for c in co.contacts}
    return False


def fill_blanks(co, fields=None, facts=None, facts_on=None, decision=None) -> bool:
    """Fill in what a company on the board is missing from a new hand-over of
    it (Add to tracker again, or a file dropped in), never changing anything
    already there. `fields` is {company field: text}, `facts` {key: text},
    `decision` {suitable, suitable_why, suitable_by, suitable_on}, taken only
    while the company has no decision. Says whether anything changed."""
    changed = False
    for key in BLANK_FILLABLE:
        text = ("" if (fields or {}).get(key) is None else str(fields[key])).strip()
        have = (getattr(co, key) or "").strip()
        if text and text.lower() != have.lower() and _says_nothing(co, key):
            setattr(co, key, _fit(co, key, text))
            changed = True

    have = facts_of(co)
    new = {k: v for k, v in clean_facts(facts or {}).items() if k not in have}
    if new:
        co.facts = json.dumps({**have, **new}, ensure_ascii=False)
        co.facts_on = co.facts_on or facts_on
        changed = True

    d = decision or {}
    if not co.suitable and d.get("suitable") in SUITABLE:
        co.suitable = d["suitable"]
        co.suitable_why = (d.get("suitable_why") or "").strip()
        co.suitable_by = _fit(co, "suitable_by", (d.get("suitable_by") or "").strip())
        co.suitable_on = d.get("suitable_on")
        changed = True
    return changed
