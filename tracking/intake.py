"""
Leads handed over from outside the board, ready to contact.

The Tracking Dashboard does not know where a lead was found. A host app that
finds companies (ARGUS, in the argus lead detection system folder) builds two
plain dicts and calls add_lead(); every write stays in this package, so the
schema has one owner. On a host with no lead finder nothing here is called.

A lead is matched on its Companies House number first, then on the exact
company name, so handing the same company over twice adds no second card; it
only fills in what the first is missing (27 Oct 2026, facts.fill_blanks).
"""

import json
from datetime import date, datetime

from .models import SessionLocal, Company, Contact
from .routes import _clean, _clean_people, _company_dict, _contact_dict, _log, _parse_date
from . import facts as FX, stages as ST

COMPANY_KEYS = {
    "name", "sub_sector", "ownership", "scale", "why_fit",
    "priority_functions", "website", "notes", "company_number",
}
CONTACT_KEYS = {
    "priority", "sector", "cost_area", "target_role", "contact_name",
    "linkedin_url", "email", "phone", "hook", "next_action", "notes",
}


def _find_company(s, number, name):
    if number:
        co = s.query(Company).filter(Company.company_number == number).first()
        if co:
            return co
    if name:
        return s.query(Company).filter(Company.name.ilike(name)).first()
    return None


def add_lead(company, contact, source="ARGUS"):
    """Put one company on the board as a New contact.

    `company` and `contact` are {field: value} dicts using the model's field
    names; anything else is ignored. `company` may also carry `people`, up to
    five names to put in the record's people slots, and `facts` ({key: text},
    facts.FACTS) with `facts_on`, the day they were taken. Returns (contact,
    company, created) as the page's own dicts, so the board can patch them in
    without a reload. When the company is already on the board, its first
    contact comes back and created is False: nothing is overwritten, but its
    blank fields and missing facts are filled in.
    """
    number = _clean(company.get("company_number"))
    name = _clean(company.get("name"))
    if not name:
        raise ValueError("A lead needs a company name.")
    facts = FX.clean_facts(company.get("facts"))
    facts_on = company.get("facts_on")
    if not isinstance(facts_on, date):
        facts_on = _parse_date(facts_on)
    with SessionLocal.begin() as s:
        co = _find_company(s, number, name)
        if co is None:
            co = Company(**{k: _clean(v) for k, v in company.items() if k in COMPANY_KEYS})
            if company.get("people"):
                co.people = json.dumps(_clean_people(company["people"]))
            if facts:
                co.facts = json.dumps(facts, ensure_ascii=False)
                co.facts_on = facts_on
            s.add(co)
            s.flush()
        else:
            FX.fill_blanks(co, company, facts, facts_on)

        c = (s.query(Contact).filter(Contact.company_id == co.id)
             .order_by(Contact.id).first())
        created = c is None
        if created:
            fields = {k: _clean(v) for k, v in contact.items() if k in CONTACT_KEYS}
            fields["priority"] = fields.get("priority") or "Medium"
            fields["sector"] = fields.get("sector") or (co.sub_sector or "")
            c = Contact(company_id=co.id, stage=1, stage_changed_at=datetime.utcnow(), **fields)
            s.add(c)
            s.flush()
            _log(s, c, "Note", note=f"Added from {source}.")

        kids = s.query(Contact).filter(Contact.company_id == co.id).all()
        return _contact_dict(c, co.name), _company_dict(co, kids), created


def stages_for(numbers):
    """{company number: stage} for those of `numbers` already on the board,
    taking the furthest stage when a company has more than one contact. The
    stage is the dict from stages.STAGES (num, label, short, tone)."""
    wanted = sorted({_clean(n) for n in numbers if _clean(n)})
    furthest = {}
    with SessionLocal() as s:
        for i in range(0, len(wanted), 500):
            rows = (s.query(Company.company_number, Contact.stage)
                    .outerjoin(Contact, Contact.company_id == Company.id)
                    .filter(Company.company_number.in_(wanted[i:i + 500]))
                    .all())
            for number, stage in rows:
                furthest[number] = max(furthest.get(number, 1), stage or 1)
    return {n: ST.STAGE_BY_NUM.get(st, ST.STAGES[0]) for n, st in furthest.items()}
