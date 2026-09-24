"""
One-off: copy the local Sprint Retro Dashboard's typed notes (retro_notes.json) into the
Abacus database, so the "Next sprint" goals already written appear on /sprint-retro.

Usage:
    python import_retro_notes.py                  # into the local SQLite (abacus.db)
    python import_retro_notes.py --force          # also overwrite weeks that already exist

To load the live site, set DATABASE_URL to Render's EXTERNAL database URL first (same as
import_data.py), e.g. in PowerShell:
    $env:DATABASE_URL = "<external url from Render>?sslmode=require"
    python import_retro_notes.py

A week that already has notes in the database is skipped unless --force, so running it
again never overwrites goals typed on the live page. The notes file is read straight from
the local dashboard's folder, so the goals never have to be committed to GitHub.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

from models import DATABASE_URL, init_db, SessionLocal, RetroNote

HERE = Path(__file__).resolve().parent
# ...\01 Standardisation\Process - Abacus\02 Working\abacus on render -> ...\01 Standardisation
NOTES = (HERE.parents[2] / "Process - Sprint Retro Dashboard" / "03 Live" / "retro_notes.json")


def _where(url):
    """The database being written to, without its password."""
    if url.startswith("sqlite"):
        return url
    head, _, tail = url.partition("@")
    return head.split("://")[0] + "://***@" + tail


def main(argv):
    force = "--force" in argv
    if not NOTES.exists():
        print(f"No notes file at {NOTES}")
        return 1
    data = json.loads(NOTES.read_text(encoding="utf-8"))
    weeks = data.get("weeks") or {}
    if not weeks:
        print("The notes file has no weeks in it; nothing to import.")
        return 0

    print(f"Notes file : {NOTES}")
    print(f"Database   : {_where(DATABASE_URL)}")
    init_db()                                   # creates retro_note if this DB predates it

    added = replaced = skipped = 0
    now = datetime.utcnow()
    with SessionLocal.begin() as s:
        for week in sorted(weeks):
            entry = weeks[week]
            if not isinstance(entry, dict):
                continue
            body = json.dumps(entry, ensure_ascii=False)
            row = s.get(RetroNote, week)
            if row and not force:
                skipped += 1
                print(f"  {week}  skipped (already in the database; use --force to overwrite)")
                continue
            if row:
                row.entry, row.saved_at = body, now
                replaced += 1
                print(f"  {week}  replaced")
            else:
                s.add(RetroNote(week_start=week, entry=body, saved_at=now))
                added += 1
                print(f"  {week}  added")
    print(f"Done: {added} added, {replaced} replaced, {skipped} skipped.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
