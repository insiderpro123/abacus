# Abacus Work Package Tracker (online)

> **Quick links**
> - **App:** https://abacus-tracker.onrender.com
> - **Password:** shared team password — _ask Sam Hucks_ (kept out of this page on purpose)
> - **Code (GitHub):** https://github.com/insiderpro123/abacus
> - **Hosting (Render):** https://dashboard.render.com

---

## What it is

The Abacus Work Package Tracker is a web app that shows the delivery progress of every
work package against the **12 key Abacus processes** (sub-points 1.1 – 12.4). It replaces
the old Excel-on-Dropbox version: everything now lives in a database and is edited straight
from the browser, so multiple people can use it at once with no file-locking or version
clashes.

It is hosted on **Render** and protected by a single **shared team password**.

---

## Getting in

1. Go to **https://abacus-tracker.onrender.com**
2. Enter the **team password** and click **Sign in**.
3. You'll land on the dashboard.

> ⏳ **First visit of the day may take ~30–50 seconds to load.** The free hosting "sleeps"
> when idle and wakes on the first request. After it wakes it's fast. Just wait — don't refresh
> repeatedly.

---

## Reading the dashboard

- Work packages are shown as **cards**, grouped into **Active** and **Inactive** (use the
  chips top-left to switch). Each card shows the client, project name and a brand emoji.
- Across the top are the **12 processes**. Each cell is a coloured **% pill** showing how
  complete that process is for that project.

**Colour key**

| Colour | Meaning |
|--------|---------|
| 🟢 Green | Complete |
| 🟡 Amber | In progress |
| 🔴 Red | Outstanding |
| ⚪ Grey | Not started |
| 🔵 Slate | Not required (excluded from the %) |

A phase's % is the average of its sub-point scores (Complete = 100%, In progress = 50%,
Outstanding/Not started = 0%). Points marked **Not required** are excluded, so a phase can
still reach 100% when some points don't apply.

---

## Looking at a project in detail

Click a work-package card to expand it. You'll see:

- A **timeline strip** of the 12 phases with their % and a status dot.
- **Progress bars** for each phase (one segment per sub-point).
- Click a phase to **drill into its sub-points 1.1 – 12.4**. Each is shown by its
  **Outcome Story** (e.g. _"Create a proposal with a clear scope"_).
- The expanded header shows the project's **overall % complete**.

---

## Editing

Click any sub-point to open its editor:

- **Set the status:** Complete / In progress / Outstanding / Not started / **Not required**.
- **Guidance for the point** (shared across all projects): _Operational relevant, Abacus Top
  level, Existing Assets, Comment_ — editable here.

Other controls (on an expanded, Active project):

- **Set all** on a phase — set every point in that phase at once (leaves any "Not required"
  points untouched).
- **Edit** (top of the panel) — change the client, project name, emoji icon, or which points
  are Not required. In that form, a **ticked** box = the point applies; **untick** a box to
  mark it **Not required**.
- **Make inactive / Make active** — see below.

Every change saves immediately; a "✓ Saved" note appears top-right.

---

## Adding, and Active vs Inactive (locking)

- **+ Add project** (below the Active list) — enter client, project name, an optional emoji,
  and tick which points already apply. New projects start as **Active**.
- **Make inactive** — parks a project. **Inactive projects are locked (read-only)** — you can
  view them but not edit points, run Set all, or edit details.
- **Make active** — unlocks an inactive project so it can be edited again.

---

## Where the data lives

- All data is stored in a **PostgreSQL database on Render** (EU / Frankfurt region). The
  database is the single source of truth — the old Excel files are no longer used day-to-day.
- Access is limited to whoever has the **URL + team password**.

---

## Admin notes (for whoever maintains it)

**Change the team password**
1. Render dashboard → **abacus-tracker** service → **Environment**.
2. Edit **`APP_PASSWORD`** → **Save changes** (redeploys in ~1 min).
3. To also force everyone to re-login, change **`SECRET_KEY`** to any new value too.

**Update the app (new features/fixes)**
- Changes are pushed to the GitHub repo's `main` branch; Render **auto-redeploys** within a
  few minutes. No manual deploy needed.

**Free-tier limits to be aware of**
- The web app **sleeps after ~15 min idle** (slow first load, then fast).
- The **free database expires after ~30 days**. Before then, upgrade the Render Postgres to a
  paid plan (~£5/month, permanent + backups) or recreate and re-seed it.

**Re-seed / reset the data from the master Excel** (rarely needed)
- Run `import_data.py --yes` locally with the database's External URL set as `DATABASE_URL`.
  This wipes and reloads everything from `Process_Abacus_Vertical.xlsm` + the prompts file.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| Page slow / spinner on first load | Normal — the free host is waking up (~30–50s). Wait. |
| "Incorrect password" | Check with Sam; the password may have been changed. |
| Can't edit a project | It's **Inactive** (locked). Click **Make active** first. |
| An edit didn't stick | Refresh; check the "✓ Saved" note appeared. |
| Site won't load at all | Check the Render dashboard — the service may be redeploying or the free DB may have expired. |

---

_Last updated: July 2026._
