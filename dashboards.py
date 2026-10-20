"""
The list of dashboards on the site. The nav strip (templates/_nav.html) and the home
page cards (templates/home.html) are both built from it, so adding a dashboard is one
entry here plus its route and template. See README "Adding a dashboard".

Since 20 Oct 2026 every dashboard belongs to a group, Marketing or Process. The home
page shows each group as its own section and the nav strip colours each group's tabs
(Marketing orange, Process green: the same colours the Sprint Retro and Meeting Notes
already use for those categories). Groups appear in GROUPS order, Marketing first, and
the dashboards inside a group in DASHBOARDS order.

Each group:
  key       short id, also the CSS class suffix (g-marketing, g-process)
  title     the section heading on the home page (no line under it, at Sam's request)

Each dashboard:
  key       short id; the page sets  {% set nav_active = "<key>" %}  to light up its tab
  title     the name on the tab, the home card and (usually) the page's top bar
  emoji     one emoji; use the colour form (with U+FE0F where it has one, e.g. 🖨️)
  endpoint  the Flask view function name, as passed to url_for()
  blurb     one sentence for the home card
  group     a key from GROUPS

An entry whose endpoint has no route yet is skipped rather than breaking every page,
so an entry can go in before its page is finished.
"""

GROUPS = [
    {"key": "marketing", "title": "Marketing"},
    {"key": "process", "title": "Process"},
]

DASHBOARDS = [
    {
        "key": "tracking",
        "title": "Tracking",
        "emoji": "🎯",
        "endpoint": "tracking.page",
        "blurb": "Every outbound lead on one board by stage. Bring a list in from ARGUS with a CSV.",
        "group": "marketing",
    },
    {
        "key": "abacus",
        "title": "Abacus",
        "emoji": "🧮",
        "endpoint": "index",
        "blurb": "Work-package progress across the 12-step delivery framework, with Jira points and sync.",
        "group": "process",
    },
    {
        "key": "retro",
        "title": "Sprint Retro",
        "emoji": "📊",
        "endpoint": "sprint_retro",
        "blurb": "Points completed each sprint, by project and sector, for the Monday retro.",
        "group": "process",
    },
    {
        "key": "notes",
        "title": "Meeting Notes",
        "emoji": "📋",
        "endpoint": "meeting_notes",
        "blurb": "Every project's Jamie meeting notes in one place: overviews and key action items.",
        "group": "process",
    },
]
