"""
Database models for the Tracking Dashboard (the outbound contact tracker).

Single source of truth for the schema, shared by routes.py (the web app) and
seed_data.py (the one-time workbook importer).

Uses plain SQLAlchemy so it works identically with SQLite (local dev) and
PostgreSQL (Render). The database is chosen by the DATABASE_URL env var:
  - unset          -> sqlite file  crm.db  in the folder that holds this
                      package (the app root), so the data never moves
  - postgres://... -> normalised to postgresql+psycopg2:// (Render)

The engine is this package's own, so on the live site it sits beside the
Abacus one on the same database. None of the five table names below is used
by the live site's models.py; keep it that way when adding a table.

Five tables:
  Company   the account (a target group, from the Target Companies tab)
  Contact   one target person: the CRM record that moves through the pipeline
  Activity  background history: every stage move (not shown on the page)
  RoleHook  the Role & Hook reference library (which role owns which budget)
  Setting   editable key/value lists (the owners, the playbook) plus the
            schema version, which is what makes the stage migration run once
"""

import json
import os
from datetime import datetime

from sqlalchemy import (
    create_engine, Column, Integer, String, Text, Date, DateTime, ForeignKey,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)          # the app root, where crm.db and the workbook live


def _database_url():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return "sqlite:///" + os.path.join(ROOT, "crm.db")
    # Render (and some hosts) hand out the legacy postgres:// scheme. Name the driver too:
    # a bare postgresql:// means psycopg (v3) from SQLAlchemy 2.1 on, which is not
    # installed. Same fix as the live site's models.py.
    for legacy in ("postgres://", "postgresql://"):
        if url.startswith(legacy):
            url = "postgresql+psycopg2://" + url[len(legacy):]
            break
    return url


DATABASE_URL = _database_url()

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)

Base = declarative_base()


class Company(Base):
    """A target company (an 'account'). One company holds many contacts, so the
    record panel can show every conversation running inside one group."""
    __tablename__ = "company"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), nullable=False, default="")
    sub_sector = Column(String(255), default="")
    ownership = Column(String(255), default="")       # 'Listed (LSE)', 'Private' ... verify before outreach
    scale = Column(String(255), default="")           # approximate size, free text
    why_fit = Column(Text, default="")                # which cost areas make them worth targeting
    priority_functions = Column(Text, default="")     # the functions to go after first
    warm_intro = Column(String(16), default="?")      # warm intro available? yes | no | ?
    status = Column(String(64), default="Not started")
    website = Column(Text, default="")
    notes = Column(Text, default="")                  # the record's Comments box since 8 Oct 2026
    # The Companies House number, set when the company came from a lead finder
    # such as ARGUS (see intake.py). It is what stops one being added twice.
    company_number = Column(String(16), default="")
    # The five people to contact here, as JSON: [{name, status, rag}, ...].
    # NULL until first saved, when the record offers the names already on the
    # company's contacts (routes._people_slots).
    people = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    contacts = relationship("Contact", back_populates="company")


class Contact(Base):
    """One target person: the record that moves along the five-stage pipeline.
    Mirrors a row of the Contact Tracker tab, plus the two timestamps the
    spreadsheet could never hold (when the stage last changed, when created)."""
    __tablename__ = "contact"
    id = Column(Integer, primary_key=True, autoincrement=True)
    company_id = Column(Integer, ForeignKey("company.id"), index=True, nullable=True)
    priority = Column(String(16), default="Medium")   # High | Medium | Low
    sector = Column(String(255), default="")
    cost_area = Column(String(255), default="")       # links to RoleHook.cost_area
    target_role = Column(Text, default="")            # the role title being approached
    contact_name = Column(String(255), default="")    # blank until found on LinkedIn
    linkedin_url = Column(Text, default="")
    email = Column(String(255), default="")
    phone = Column(String(64), default="")
    hook = Column(Text, default="")                   # the benchmarking wedge to lead with
    stage = Column(Integer, nullable=False, default=1)          # 1..5, see stages.py
    outcome = Column(String(32), default="")          # Won | Lost | Nurture, once closed
    win_chance = Column(Integer, nullable=True)       # 0-100 set by hand; blank = the stage estimate
    owner = Column(String(128), default="")           # workbook import only; not shown since 6 Oct 2026
    last_contact = Column(Date, nullable=True)
    next_action = Column(Text, default="")
    next_action_date = Column(Date, nullable=True)
    notes = Column(Text, default="")                  # the one "latest update" comment on the card
    notes_on = Column(Date, nullable=True)            # when that comment last changed
    stage_changed_at = Column(DateTime, default=datetime.utcnow)  # drives board ageing
    created_at = Column(DateTime, default=datetime.utcnow)

    company = relationship("Company", back_populates="contacts")
    activities = relationship("Activity", cascade="all, delete-orphan",
                              back_populates="contact")


class Activity(Base):
    """Background history for a contact: a row whenever a card changes stage
    (kind='Stage change', with the from/to stages recorded), plus the touches
    logged before 6 Oct 2026, when the touch log was replaced by the one
    "latest update" comment on the contact. Not shown on the page; kept so the
    time spent in each stage can still be measured. The owner column is no
    longer written."""
    __tablename__ = "activity"
    id = Column(Integer, primary_key=True, autoincrement=True)
    contact_id = Column(Integer, ForeignKey("contact.id", ondelete="CASCADE"),
                        index=True, nullable=False)
    kind = Column(String(64), nullable=False, default="Note")   # see stages.ACTIVITY_KINDS
    happened_on = Column(Date, nullable=True)
    owner = Column(String(128), default="")
    note = Column(Text, default="")
    from_stage = Column(Integer, nullable=True)   # set only on 'Stage change' rows
    to_stage = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    contact = relationship("Contact", back_populates="activities")


class RoleHook(Base):
    """The Role & Hook library: for each cost area, which role titles own the
    budget and which benchmark to lead with. Used as reference on its own page
    and to pre-fill the role and hook when a new contact is added."""
    __tablename__ = "role_hook"
    id = Column(Integer, primary_key=True, autoincrement=True)
    tier = Column(String(64), default="")             # '1 - Clinical' | '2 - Indirect'
    cost_area = Column(String(255), default="")
    role_titles = Column(Text, default="")
    seniority = Column(String(128), default="")
    benchmark = Column(Text, default="")              # the benchmark to lead with
    why_reachable = Column(Text, default="")
    proof_point = Column(Text, default="")            # the ISP work that backs it up
    sharpness = Column(String(32), default="")        # High | Medium | Low


class Setting(Base):
    """Editable app-level lists (owners, sectors, cost areas) stored as JSON, so
    a new name can be added in Settings without a code change."""
    __tablename__ = "setting"
    key = Column(String(64), primary_key=True)
    value = Column(Text, default="")


# --------------------------------------------------------------------------- #
# Settings helpers
# --------------------------------------------------------------------------- #
def get_setting(session, key, default=None):
    row = session.get(Setting, key)
    if not row or not row.value:
        return default
    try:
        return json.loads(row.value)
    except (ValueError, TypeError):
        return default


def set_setting(session, key, value):
    row = session.get(Setting, key)
    if not row:
        row = Setting(key=key)
        session.add(row)
    row.value = json.dumps(value)
    return row


# --------------------------------------------------------------------------- #
# Schema creation / lightweight migration
# --------------------------------------------------------------------------- #
def _ensure_columns():
    """Add columns introduced after a table was first created, so an existing
    SQLite/Postgres database gains them without a wipe. Same approach as the
    Abacus tracker: cheap, additive, safe to run on every start."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    wanted = {
        "contact": {
            "outcome": "VARCHAR(32)",
            "stage_changed_at": "TIMESTAMP",
            "hook": "TEXT",
            "next_action": "TEXT",
            "next_action_date": "DATE",
            "notes": "TEXT",
            "notes_on": "DATE",
            "win_chance": "INTEGER",
        },
        "company": {
            "website": "TEXT",
            "notes": "TEXT",
            "company_number": "VARCHAR(16)",
            "people": "TEXT",
        },
        "activity": {
            "from_stage": "INTEGER",
            "to_stage": "INTEGER",
        },
    }
    stmts = []
    for table, cols in wanted.items():
        if table not in tables:
            continue
        existing = {c["name"] for c in insp.get_columns(table)}
        for col, coltype in cols.items():
            if col not in existing:
                stmts.append(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
    if stmts:
        with engine.begin() as conn:
            for stmt in stmts:
                conn.execute(text(stmt))


def _claim_version(s, old, new):
    """Move schema_v from `old` to `new` in one conditional UPDATE and say
    whether this process did it. The UPDATE takes the write lock before any row
    is remapped, so when two copies of the app start together on one database
    (a debug reloader beside a second server, say) only one of them migrates;
    the other finds the version already moved and leaves the rows alone."""
    from sqlalchemy import text
    res = s.execute(
        text("UPDATE setting SET value = :new WHERE key = 'schema_v' AND value = :old"),
        {"new": json.dumps(new), "old": json.dumps(old)})
    return res.rowcount == 1


def migrate_stages():
    """Bring the stored stage numbers up to the current set in stages.py.

    One-way steps, each run at most once and recorded as schema_v in the
    setting table: the original twelve to six (v2, 27 Aug 2026), those six to
    five (v3, 6 Oct 2026), then Won out of Closed into Work agreed (v4, the
    same day). A brand new database is migrated while it is still empty, which
    just stamps the version, so the workbook importer that follows writes the
    current numbering straight away.
    """
    from . import stages as ST

    with SessionLocal.begin() as s:
        if int(get_setting(s, "schema_v", 1) or 1) < 2:
            for c in s.query(Contact).all():
                old = c.stage or 1
                c.stage = ST.STAGE_MAP_V1_TO_V2.get(old, min(old, 6))
                if not c.outcome and old in ST.OUTCOME_FROM_V1:
                    c.outcome = ST.OUTCOME_FROM_V1[old]
            for a in s.query(Activity).all():
                if a.from_stage:
                    a.from_stage = ST.STAGE_MAP_V1_TO_V2.get(a.from_stage, a.from_stage)
                if a.to_stage:
                    a.to_stage = ST.STAGE_MAP_V1_TO_V2.get(a.to_stage, a.to_stage)
            set_setting(s, "schema_v", 2)

    # v2 -> v3: Researched folds into New, Talking and Meeting become one
    # phase, a stage is added after them and Closed moves from 6 to 5.
    m = ST.STAGE_MAP_V2_TO_V3
    with SessionLocal.begin() as s:
        if _claim_version(s, 2, 3):
            for c in s.query(Contact).all():
                c.stage = m.get(c.stage or 1, 1)
            for a in s.query(Activity).all():
                if not (a.from_stage and a.to_stage):
                    continue
                new_from, new_to = m.get(a.from_stage, 1), m.get(a.to_stage, 1)
                if new_from == new_to:
                    # A move between two stages that are now one would read
                    # "New to New". Keep what happened, as a note.
                    a.kind = "Note"
                    a.note = (f"Moved from {ST.V2_LABELS.get(a.from_stage, a.from_stage)} "
                              f"to {ST.V2_LABELS.get(a.to_stage, a.to_stage)}, "
                              f"stages that have since been merged.")
                    a.from_stage = a.to_stage = None
                else:
                    a.from_stage, a.to_stage = new_from, new_to

    # v3 -> v4: the win is its own stage, Work agreed, so a contact closed as
    # Won moves there and Closed keeps only Lost and Nurture.
    with SessionLocal.begin() as s:
        if _claim_version(s, 3, 4):
            for c in (s.query(Contact)
                      .filter(Contact.stage.in_(ST.CLOSED_STAGES), Contact.outcome == "Won")
                      .all()):
                c.stage = ST.WON_FROM_CLOSED
                c.outcome = ""


def init_db():
    """Create any missing tables, add any newly-introduced columns, then run the
    one-time stage migration."""
    Base.metadata.create_all(engine)
    _ensure_columns()
    migrate_stages()
