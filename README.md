# Abacus - Full System (Test)

A Flask web app that tracks delivery progress of every work package across the
**12 key Abacus processes**. It runs behind a shared team password and stores all data
in a SQL database (SQLite locally, PostgreSQL when hosted on Render).

> **This is the "full system" test copy** of the Abacus tracker. It runs on **port 5070**
> (double-click `RUN - WINDOWS.bat`, or `python app.py`, then open http://127.0.0.1:5070),
> so it can run alongside the original Abacus app (5010) and the Jamie dashboard (5057).
> On top of the standard tracker it adds three things:
>
> 1. **A simple task board per work package** - open a work package and click
>    **📋 Task board**. It has **two boxes**: a **Working** box on the left holding tasks
>    that are **To Do (red)** or **In Progress (amber)**, and a **Complete** box on the right
>    **(green)**. Type a task, then **click a card to advance it** To Do → In Progress →
>    Complete (a completed card moves to the right box); click it again to send it back.
>    The matrix row shows a `✓ done/total tasks` badge.
> 2. **Per-work-package links** - each work package can carry its own **Confluence** and
>    **Dropbox** links (set them in the Add/Edit project dialog); they appear as buttons on
>    its detail panel. If the Dropbox link is left blank it is auto-filled from the client name.
> 3. **A global "🎙 Jamie meetings" link** in the header, pointing at the Jamie dashboard
>    (`http://127.0.0.1:5057` by default; override with the `JAMIE_URL` environment variable).

## What it shows

1. **Summary matrix** - every work package as a card, the 12 processes as columns.
   Each cell is a colour-coded % pill; work packages are grouped by **Active / Inactive**.
2. **Gantt / phase view** - click a work package to expand it:
   - a **timeline strip** of the 12 phases with % and RAG dots;
   - **segmented progress bars** per phase (one segment per sub-point);
   - drill into sub-points **1.1 – 12.4**, each shown by its **Outcome Story**, with a
     click-to-edit status + guidance dialog.
3. **Editing** - set each point to Complete / In progress / Outstanding / Not started /
   Not required; per-phase "Set all"; add / edit projects (client, name, emoji icon,
   which points are Not required); make projects active/inactive. **Inactive packages are
   locked (read-only) until reactivated.**

## Colour key

| Colour | Meaning | Stored value |
|--------|---------|-------------|
| 🟢 Green | Complete | `3` (or the phase's Finished flag) |
| 🟡 Amber | In progress | `2` |
| 🔴 Red | Outstanding | `1` |
| ⚫ Grey | Not started | (no value) |
| 🔵 Slate | Not required | `N/R` (excluded from the %) |

A phase's % = average of its sub-point scores (3=100%, 2=50%, 1/blank=0%); Not-required
points are excluded. Overall % = average across the 12 phases.

## Architecture

- **Flask** app + **SQLAlchemy** ORM. Schema in `models.py`; data in `abacus.db` locally
  or Render Postgres via the `DATABASE_URL` env var.
- **Auth:** one shared password (`APP_PASSWORD` env var); signed-session cookie. All pages
  and APIs are gated by `/login`.
- The front-end (`static/app.js`, `templates/index.html`) talks to 8 JSON endpoints and is
  storage-agnostic.

Environment variables:
- `APP_PASSWORD` - the shared team password (local default: `abacus`).
- `SECRET_KEY` - signs session cookies (auto-random locally; set a fixed value in prod).
- `DATABASE_URL` - Postgres URL in prod; unset locally → SQLite `abacus.db`.
- `PORT` / `NO_BROWSER` - local dev only.

## Local development

```sh
pip install -r requirements.txt
python import_data.py --yes      # one-time: build abacus.db from the Excel files
python app.py                    # opens a browser; log in with APP_PASSWORD (default: abacus)
```
`RUN - WINDOWS.bat` / `RUN - MAC.command` still launch `app.py` for convenience (see the
`SETUP-*.txt` files) - but run `import_data.py --yes` once first to create the database.

## Deploy online (Render)

1. **Put the code on GitHub** (client Excel files are git-ignored and never uploaded).
2. On **Render** → **New → Blueprint** → select the repo. `render.yaml` creates a **web
   service** + a **free Postgres** database (Frankfurt/EU region).
3. In the web service's **Environment** tab, set **`APP_PASSWORD`** to your team password.
   (`SECRET_KEY` is auto-generated; `DATABASE_URL` is wired automatically.)
4. **Seed the database** (see below).
5. Open the Render URL and log in.

### Seed the database

The importer reads the current Excel files, so run it **from your machine** (keeps client
data off GitHub) pointed at Render's database:

1. In Render, open the `abacus-db` database → copy its **External Database URL**.
2. Locally, in this folder:
   ```sh
   # Windows PowerShell
   $env:DATABASE_URL="postgresql://…external…"; python import_data.py --yes
   # macOS / Linux
   DATABASE_URL="postgresql://…external…" python3 import_data.py --yes
   ```
Re-running `import_data.py --yes` wipes and reloads everything - only do it to reset.

## Files

| File | Purpose |
|------|---------|
| `app.py` | Flask app: routes, auth, DB reads/writes |
| `models.py` | SQLAlchemy schema (Process, Subprocess, WorkPackage, WpStatus, WpFinished) |
| `import_data.py` | One-time Excel → database importer / seeder |
| `templates/index.html`, `templates/login.html` | Page shells |
| `static/app.js`, `static/style.css`, `static/ip-logo.svg` | Front-end |
| `requirements.txt`, `render.yaml`, `Procfile`, `.python-version` | Deploy config |
| `RUN - *.bat/.command`, `SETUP-*.txt` | Local-dev launchers + notes |
| `Process_Abacus_Vertical.xlsm`, `Abacus_prompts…xlsx`, `project_icons.json` | Import source only (git-ignored) |

## Notes

- The database is the single source of truth once seeded; the Excel files are only used to
  seed it.
- Client cost data lives on Render (Frankfurt/EU) - access is limited to whoever has the
  URL + shared password. To upgrade later to per-person or Microsoft logins, the schema
  leaves room to add a users/auth layer.
