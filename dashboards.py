"""
The list of dashboards on the site. The nav strip (templates/_nav.html) and the home
page cards (templates/home.html) are both built from it, in this order, so adding a
dashboard is one entry here plus its route and template. See README "Adding a dashboard".

Each entry:
  key       short id; the page sets  {% set nav_active = "<key>" %}  to light up its tab
  title     the name on the tab, the home card and (usually) the page's top bar
  emoji     one emoji; use the colour form (with U+FE0F where it has one, e.g. 🖨️)
  endpoint  the Flask view function name, as passed to url_for()
  blurb     one sentence for the home card

An entry whose endpoint has no route yet is skipped rather than breaking every page,
so an entry can go in before its page is finished.
"""

DASHBOARDS = [
    {
        "key": "abacus",
        "title": "Abacus",
        "emoji": "🧮",
        "endpoint": "index",
        "blurb": "Work-package progress across the 12-step delivery framework, with Jira points and sync.",
    },
    {
        "key": "retro",
        "title": "Sprint Retro",
        "emoji": "📊",
        "endpoint": "sprint_retro",
        "blurb": "Points completed each sprint, by project and sector, for the Monday retro.",
    },
    {
        "key": "notes",
        "title": "Meeting Notes",
        "emoji": "📋",
        "endpoint": "meeting_notes",
        "blurb": "Every project's Jamie meeting notes in one place: overviews and key action items.",
    },
]
