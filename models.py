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
from sqlalchemy.orm import declarative_base, sessionmaker, relationship

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
    # guidance (shared per code) — 'outcomes' doubles as the displayed label
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
    points = Column(String(32), default="")          # legacy 'Jira points', not edited
    icon = Column(String(16), default="")            # emoji shown on the card
    created_at = Column(DateTime, default=datetime.utcnow)
    # Jira link (read-only): mapped project key + last-synced story-point totals
    jira_project_key = Column(String(32), default="")
    jira_done = Column(Integer, default=0)
    jira_total = Column(Integer, default=0)
    jira_synced_at = Column(DateTime, nullable=True)

    statuses = relationship("WpStatus", cascade="all, delete-orphan", backref="wp")
    finished = relationship("WpFinished", cascade="all, delete-orphan", backref="wp")


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
    if to_add:
        with engine.begin() as conn:
            for stmt in to_add:
                conn.execute(text(stmt))


def init_db():
    """Create any missing tables, then add any newly-introduced columns."""
    Base.metadata.create_all(engine)
    _ensure_columns()
