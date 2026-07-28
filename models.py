"""
Database models for the Abacus Work Package Tracker.

Single source of truth for the schema, shared by app.py (the web app) and
import_data.py (the one-time Excel importer).

Uses plain SQLAlchemy so it works identically with SQLite (local dev) and
PostgreSQL (Render). The database is chosen by the DATABASE_URL env var:
  - unset            -> sqlite file  abacus.db  next to this module (local dev)
  - postgres://...    -> normalised to postgresql:// and used as-is (Render)
"""

import os
from datetime import datetime

from sqlalchemy import (
    create_engine, Column, Integer, String, Text, DateTime, ForeignKey,
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship, backref

HERE = os.path.dirname(os.path.abspath(__file__))


def _database_url():
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        return "sqlite:///" + os.path.join(HERE, "abacus.db")
    # Render (and some hosts) hand out the legacy postgres:// scheme
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


DATABASE_URL = _database_url()

_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(DATABASE_URL, future=True, pool_pre_ping=True, connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)

Base = declarative_base()


class Process(Base):
    """One of the 12 top-level process groups (shared across all work packages)."""
    __tablename__ = "process"
    num = Column(Integer, primary_key=True)          # 1..12
    title = Column(String(255), nullable=False)


class Subprocess(Base):
    """A sub-point (e.g. '1.1'). Definition + shared guidance, keyed by code."""
    __tablename__ = "subprocess"
    code = Column(String(16), primary_key=True)      # '1.1' .. '12.4'
    process_num = Column(Integer, ForeignKey("process.num"), nullable=False, index=True)
    seq = Column(Integer, nullable=False)            # order within the phase
    question = Column(Text, default="")              # original WP_Master question text
    # guidance (shared per code) - 'outcomes' doubles as the displayed label
    outcomes = Column(Text, default="")
    operational = Column(Text, default="")
    top_level = Column(Text, default="")
    assets = Column(Text, default="")
    comment = Column(Text, default="")


class WorkPackage(Base):
    __tablename__ = "work_package"
    id = Column(Integer, primary_key=True, autoincrement=False)  # ids assigned explicitly
    client = Column(String(255), nullable=False, default="")
    name = Column(String(255), nullable=False, default="")       # short project title
    description = Column(Text, default="")           # optional longer description (<=35 words)
    year = Column(String(16), default="")            # e.g. "2026" or "2025/26"
    status = Column(String(32), nullable=False, default="Active")
    # Top-level project category: 'Customer' (full 12-step Abacus tracker),
    # 'Marketing' or 'Process and Ops' (no Abacus - just sub-WPs + tasks).
    # Sub-work-packages store their parent's category.
    category = Column(String(32), nullable=False, default="Customer")
    points = Column(String(32), default="")          # legacy 'Jira points', not edited
    icon = Column(String(16), default="")            # emoji shown on the card
    created_at = Column(DateTime, default=datetime.utcnow)
    # Jira link (read-only): mapped project key + last-synced story-point totals
    jira_project_key = Column(String(32), default="")
    jira_done = Column(Integer, default=0)
    jira_total = Column(Integer, default=0)
    jira_synced_at = Column(DateTime, nullable=True)
    # External links (per work package) shown as buttons on the detail panel
    confluence_url = Column(Text, default="")
    dropbox_url = Column(Text, default="")
    jamie_tag = Column(String(128), default="")   # Jamie tag NAME for this project's meeting notes
    # NULL = top-level project; set = a sub-work-package nested under another work package
    parent_id = Column(Integer, ForeignKey("work_package.id", ondelete="CASCADE"), nullable=True, index=True)
    sub_num = Column(Integer, nullable=True)   # per-parent sub-work-package number (1, 2, 3 ...)

    statuses = relationship("WpStatus", cascade="all, delete-orphan", backref="wp")
    finished = relationship("WpFinished", cascade="all, delete-orphan", backref="wp")
    tasks = relationship("WpTask", cascade="all, delete-orphan", backref="wp")
    children = relationship("WorkPackage", cascade="all, delete-orphan",
                            backref=backref("parent", remote_side=[id]))


class WpStatus(Base):
    """A work package's RAG value for one sub-point. Missing row = not started."""
    __tablename__ = "wp_status"
    wp_id = Column(Integer, ForeignKey("work_package.id", ondelete="CASCADE"), primary_key=True)
    code = Column(String(16), ForeignKey("subprocess.code"), primary_key=True)
    value = Column(String(8), nullable=False)        # '1' | '2' | '3' | 'N/R'


class WpFinished(Base):
    """Presence = the 'Finished?' flag for a phase (forces that phase to 100%)."""
    __tablename__ = "wp_finished"
    wp_id = Column(Integer, ForeignKey("work_package.id", ondelete="CASCADE"), primary_key=True)
    process_num = Column(Integer, ForeignKey("process.num"), primary_key=True)


class WpTask(Base):
    """A free-form task on a work package's simple two-box task board.
    status: 'todo' (red) | 'progress' (amber) | 'done' (green, right-hand box)."""
    __tablename__ = "wp_task"
    id = Column(Integer, primary_key=True, autoincrement=True)
    wp_id = Column(Integer, ForeignKey("work_package.id", ondelete="CASCADE"),
                   index=True, nullable=False)
    title = Column(Text, nullable=False, default="")
    status = Column(String(16), nullable=False, default="todo")   # todo | progress | done
    points = Column(Integer, default=0)   # modified Fibonacci story points (0 = unset)
    assignee = Column(String(128), default="")   # who the task is assigned to (from Jira / free text)
    priority = Column(Integer, default=3)        # 1 = highest … 5 = lowest (3 = medium/default)
    blocker = Column(Integer, default=0)         # 1 = blocked/blocker (shown with ⛔)
    jira_key = Column(String(32), default="")    # source Jira issue key; merge key for re-sync
    sub_wp_id = Column(Integer, nullable=True)   # optional link to one of the WP's sub-work-packages
    sprint_id = Column(Integer, nullable=True)   # which Sprint this task belongs to (NULL = active)
    seq = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)


class Sprint(Base):
    """A weekly (Mon-Fri) sprint. One row is 'active', at most one 'upcoming'; the rest
    are 'closed'. Tasks link to a sprint via WpTask.sprint_id (NULL means the active one)."""
    __tablename__ = "sprint"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), default="")             # e.g. "20-24 Jul 26"
    week_start = Column(String(10), default="")        # ISO date "YYYY-MM-DD" (Monday)
    week_end = Column(String(10), default="")          # ISO date "YYYY-MM-DD" (Friday)
    status = Column(String(16), nullable=False, default="active")   # active | upcoming | closed
    created_at = Column(DateTime, default=datetime.utcnow)


class SprintHistory(Base):
    """Per-week, per-category points snapshot. Backfilled from Jira closed sprints
    (source='jira') and written by the app itself when a sprint closes (source='app')."""
    __tablename__ = "sprint_history"
    id = Column(Integer, primary_key=True, autoincrement=True)
    week_start = Column(String(10), default="")        # ISO Monday - the history key
    week_end = Column(String(10), default="")
    label = Column(String(128), default="")            # human label for the week
    category = Column(String(32), default="Customer")
    points_planned = Column(Integer, default=0)
    points_done = Column(Integer, default=0)
    tasks_total = Column(Integer, default=0)
    tasks_todo = Column(Integer, default=0)
    tasks_progress = Column(Integer, default=0)
    tasks_done = Column(Integer, default=0)
    source = Column(String(8), default="jira")         # jira | app
    captured_at = Column(DateTime, default=datetime.utcnow)


class Customer(Base):
    """A customer login. Linked to a client (all that client's projects) and a
    Jamie tag NAME used to pull their meeting notes. Password is hashed."""
    __tablename__ = "customer"
    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    client = Column(String(255), default="")      # client they're linked to
    jamie_tag = Column(String(128), default="")   # Jamie tag NAME for their meetings
    created_at = Column(DateTime, default=datetime.utcnow)


class StaffUser(Base):
    """An ISP staff login (email + hashed password). Full operational access, but
    not the admin-only 'full settings' (editing the 12 steps / managing logins)."""
    __tablename__ = "staff_user"
    id = Column(Integer, primary_key=True, autoincrement=True)
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(String(255), nullable=False)
    name = Column(String(255), default="")
    created_at = Column(DateTime, default=datetime.utcnow)


def _ensure_columns():
    """Lightweight migration: add columns that were introduced after the table
    was first created (so an existing SQLite/Postgres DB gains them without a wipe)."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if "work_package" not in insp.get_table_names():
        return
    existing = {c["name"] for c in insp.get_columns("work_package")}
    to_add = []
    if "description" not in existing:
        to_add.append("ALTER TABLE work_package ADD COLUMN description TEXT DEFAULT ''")
    if "year" not in existing:
        to_add.append("ALTER TABLE work_package ADD COLUMN year VARCHAR(16) DEFAULT ''")
    if "jira_project_key" not in existing:
        to_add.append("ALTER TABLE work_package ADD COLUMN jira_project_key VARCHAR(32) DEFAULT ''")
    if "jira_done" not in existing:
        to_add.append("ALTER TABLE work_package ADD COLUMN jira_done INTEGER DEFAULT 0")
    if "jira_total" not in existing:
        to_add.append("ALTER TABLE work_package ADD COLUMN jira_total INTEGER DEFAULT 0")
    if "jira_synced_at" not in existing:
        to_add.append("ALTER TABLE work_package ADD COLUMN jira_synced_at TIMESTAMP")
    if "confluence_url" not in existing:
        to_add.append("ALTER TABLE work_package ADD COLUMN confluence_url TEXT DEFAULT ''")
    if "dropbox_url" not in existing:
        to_add.append("ALTER TABLE work_package ADD COLUMN dropbox_url TEXT DEFAULT ''")
    if "jamie_tag" not in existing:
        to_add.append("ALTER TABLE work_package ADD COLUMN jamie_tag VARCHAR(128) DEFAULT ''")
    if "category" not in existing:
        to_add.append("ALTER TABLE work_package ADD COLUMN category VARCHAR(32) DEFAULT 'Customer'")
    if "parent_id" not in existing:
        to_add.append("ALTER TABLE work_package ADD COLUMN parent_id INTEGER")
    if "sub_num" not in existing:
        to_add.append("ALTER TABLE work_package ADD COLUMN sub_num INTEGER")
    # wp_task.points (the table may already exist from an earlier run without this column)
    if "wp_task" in insp.get_table_names():
        task_cols = {c["name"] for c in insp.get_columns("wp_task")}
        if "points" not in task_cols:
            to_add.append("ALTER TABLE wp_task ADD COLUMN points INTEGER DEFAULT 0")
        if "sub_wp_id" not in task_cols:
            to_add.append("ALTER TABLE wp_task ADD COLUMN sub_wp_id INTEGER")
        if "assignee" not in task_cols:
            to_add.append("ALTER TABLE wp_task ADD COLUMN assignee VARCHAR(128) DEFAULT ''")
        if "priority" not in task_cols:
            to_add.append("ALTER TABLE wp_task ADD COLUMN priority INTEGER DEFAULT 3")
        if "blocker" not in task_cols:
            to_add.append("ALTER TABLE wp_task ADD COLUMN blocker INTEGER DEFAULT 0")
        if "jira_key" not in task_cols:
            to_add.append("ALTER TABLE wp_task ADD COLUMN jira_key VARCHAR(32) DEFAULT ''")
        if "sprint_id" not in task_cols:
            to_add.append("ALTER TABLE wp_task ADD COLUMN sprint_id INTEGER")
    if to_add:
        with engine.begin() as conn:
            for stmt in to_add:
                conn.execute(text(stmt))


def _ensure_active_sprint():
    """Guarantee exactly one 'active' Sprint exists, and back-fill any tasks that have
    no sprint_id onto it (so legacy rows belong to the current sprint)."""
    from sqlalchemy.orm import Session
    with Session(engine) as s:
        active = s.query(Sprint).filter_by(status="active").first()
        if not active:
            active = Sprint(name="Current sprint", status="active")
            s.add(active)
            s.flush()
        s.query(WpTask).filter(WpTask.sprint_id.is_(None)).update(
            {WpTask.sprint_id: active.id}, synchronize_session=False)
        s.commit()


def init_db():
    """Create any missing tables, then add any newly-introduced columns."""
    Base.metadata.create_all(engine)
    _ensure_columns()
    _ensure_active_sprint()
