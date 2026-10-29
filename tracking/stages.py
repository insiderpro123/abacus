"""
Shared vocabulary for the Tracking Dashboard (the outbound CRM).

Single source of truth for the pipeline stages, the closed outcomes, the
priorities and the activity kinds. Imported by routes.py (the web app) and
seed_data.py (the one-time workbook import), and handed to the front end by
/api/tracking/data so the board and the record panel always agree on what a stage is.
"""

# --------------------------------------------------------------------------- #
# Pipeline stages
#
# Five stages, one board column each. 'num' is what the database stores, so the
# funnel can be ordered and compared. 'tone' picks the colour token used by the
# column and the stage badge.
#
# The original workbook had twelve. Those twelve said the same thing twice: the
# difference between "LinkedIn request sent", "Connected" and "Email sent" is
# which touch you logged, not where the prospect is, and Won / Lost / Nurture
# are an outcome rather than a place in the process. So the touches live in the
# activity log and the outcome lives in Contact.outcome. That left six (v2).
# On 6 Oct 2026 Researched was dropped, Talking and Meeting became one phase and
# a stage was added after them (v3), which the same day became Work agreed: the
# win, as its own column, so Closed now only means it ended without work (v4).
# See STAGE_MAP_V2_TO_V3 and STAGE_MAP_V3_TO_V4.
# On 20 Oct 2026 Work agreed was renamed Add to InsideTrack (user's words). Only
# the label changed: it is still stage 4, the win.
# --------------------------------------------------------------------------- #
STAGES = [
    {"num": 1, "label": "New",                "short": "New",                "tone": "cold"},
    {"num": 2, "label": "Contacted",          "short": "Contacted",          "tone": "warm"},
    {"num": 3, "label": "Talking / Meeting",  "short": "Talking / Meeting",  "tone": "hot"},
    {"num": 4, "label": "Add to InsideTrack", "short": "Add to InsideTrack", "tone": "won"},
    {"num": 5, "label": "Closed",             "short": "Closed",             "tone": "park"},
]

STAGE_BY_NUM = {s["num"]: s for s in STAGES}

# Names a stage has had before, so a CSV saved under the old name still lands
# in the right column.
FORMER_LABELS = {"work agreed": 4}

# Add to InsideTrack (Work agreed until 20 Oct 2026) is the win. It is not a
# lead any more, so it never ages and is not counted as live, but it stays on
# the board, because wins are worth seeing.
WON_STAGES = [4]
# Closed means it ended without work. It never ages, never counts as live, and
# sits behind the board's Closed toggle.
CLOSED_STAGES = [5]
# The last stage a contact can be advanced into with the card arrow: Talking /
# Meeting moves to Add to InsideTrack in one click. Closing something needs an
# outcome, so it is a deliberate act in the record panel.
LAST_OPEN_STAGE = 4

# How a closed conversation ended, held on Contact.outcome. Won used to be one
# of these; it is the Work agreed stage now.
OUTCOMES = ["Lost", "Nurture"]


# --------------------------------------------------------------------------- #
# Chance of a win
#
# The card stopped showing this on 20 Oct 2026 (user: "remove percentage on
# the card"); it is still worked out, sorts each column and goes in the CSV.
#
# Every live card showed the chance it ends with work agreed. Somebody's own
# figure for a contact (Contact.win_chance, set in the record) always wins;
# until there is one, the card shows this estimate for its stage, marked with
# a "~". It is built from published averages, not from this team's history (no
# work has been agreed yet), so it is an estimate and the page says so:
#
#   meeting  -> agreed   20%  HubSpot's default for "Appointment scheduled"
#   reply    -> meeting  40%  30-60% of positive replies book a meeting
#   outreach -> reply     5%  B2B cold outreach averages about 3.4-5.1%
#
# Talking / Meeting starts at a reply, so it takes the reply figure (8%). New
# and Contacted have yet to get a reply, so they come out under 1%. Work agreed
# is 100% by definition. The record lost its chance slider on 8 Oct 2026, so
# a figure set before then still shows, but no new one can be set.
# --------------------------------------------------------------------------- #
_MEETING_TO_WON = 20.0
_REPLY_TO_MEETING = 0.40
_OUTREACH_TO_REPLY = 0.05

WIN_CHANCE_ESTIMATE = {
    1: _MEETING_TO_WON * _REPLY_TO_MEETING * _OUTREACH_TO_REPLY,   # New          0.4
    2: _MEETING_TO_WON * _REPLY_TO_MEETING * _OUTREACH_TO_REPLY,   # Contacted    0.4
    3: _MEETING_TO_WON * _REPLY_TO_MEETING,                        # Talking/Mtg  8
}


# --------------------------------------------------------------------------- #
# Migrations
#
# The original twelve stages went to six (v2) on 27 Aug 2026, the six to five
# (v3) on 6 Oct 2026, and Won moved out of Closed into Work agreed (v4) the
# same day. models.migrate_stages() runs each step once. seed_data.py reads a
# workbook that still writes the original twelve, so it goes straight from
# those to the current five with STAGE_MAP_V1.
# --------------------------------------------------------------------------- #
STAGE_MAP_V2_TO_V3 = {
    1: 1,   # New               -> New
    2: 1,   # Researched        -> New
    3: 2,   # Contacted         -> Contacted
    4: 3,   # Talking           -> Talking / Meeting
    5: 3,   # Meeting           -> Talking / Meeting
    6: 5,   # Closed            -> Closed (the outcome is unchanged)
}

STAGE_MAP_V1_TO_V2 = {
    1: 1,   # Not started          -> New
    2: 2,   # Researched           -> Researched
    3: 3,   # LinkedIn request sent -> Contacted
    4: 3,   # Connected            -> Contacted
    5: 3,   # Email sent           -> Contacted
    6: 4,   # Responded            -> Talking
    7: 5,   # Call/meeting booked  -> Meeting
    8: 5,   # Meeting held         -> Meeting
    9: 5,   # Pitched              -> Meeting
    10: 6,  # Won                  -> Closed, outcome Won
    11: 6,  # Lost                 -> Closed, outcome Lost
    12: 6,  # Nurture              -> Closed, outcome Nurture
}

# v3 -> v4 is not a renumbering: the stage numbers stay, and a contact at
# Closed with the outcome Won moves to Work agreed. models.migrate_stages()
# does that by the outcome, so there is no number map for it.
WON_FROM_CLOSED = 4

# The original twelve straight to the current five, for the workbook importer.
# Old status 10 (Won) is the Work agreed stage now, not Closed.
STAGE_MAP_V1 = {old: STAGE_MAP_V2_TO_V3[v2] for old, v2 in STAGE_MAP_V1_TO_V2.items()}
STAGE_MAP_V1[10] = WON_FROM_CLOSED

OUTCOME_FROM_V1 = {10: "Won", 11: "Lost", 12: "Nurture"}

# The old labels, kept only so the workbook importer can still read a status
# cell that spells the stage out rather than numbering it.
_V1_LABELS = {
    1: "Not started", 2: "Researched", 3: "LinkedIn request sent",
    4: "Connected", 5: "Email sent", 6: "Responded",
    7: "Call/meeting booked", 8: "Meeting held", 9: "Pitched",
    10: "Won", 11: "Lost", 12: "Nurture",
}


# The six v2 labels, so the v3 migration can still name a move between two
# stages that are now one (Talking to Meeting, New to Researched).
V2_LABELS = {1: "New", 2: "Researched", 3: "Contacted", 4: "Talking", 5: "Meeting", 6: "Closed"}


def stage_label(num):
    """Display label for a stage number, prefixed with its number."""
    s = STAGE_BY_NUM.get(int(num or 1))
    return f"{s['num']}. {s['label']}" if s else str(num)


def stage_from_excel(value):
    """Map a workbook status string to one of the current stages.

    The sheet writes them as '1. Not started', '3. LinkedIn request sent' and
    so on, so the leading number is the reliable part. Falls back to matching
    the old label text, then to stage 1. The result is always a current stage
    number, because the workbook is the only place the old twelve survive.
    """
    text = str(value or "").strip()
    if not text:
        return 1
    head = text.split(".", 1)[0].strip()
    if head.isdigit() and int(head) in STAGE_MAP_V1:
        return STAGE_MAP_V1[int(head)]
    lowered = text.lower()
    for old, label in _V1_LABELS.items():
        if label.lower() in lowered or lowered in label.lower():
            return STAGE_MAP_V1[old]
    return 1


def outcome_from_excel(value):
    """Lost / Nurture out of a workbook status cell, or '' for anything else.
    The outcome used to be stages 10 to 12; Won (10) is the Work agreed stage
    now, which stage_from_excel already returns, so it carries no outcome."""
    text = str(value or "").strip()
    if not text:
        return ""
    head = text.split(".", 1)[0].strip()
    if head.isdigit():
        name = OUTCOME_FROM_V1.get(int(head), "")
    else:
        lowered = text.lower()
        name = next((n for old, n in OUTCOME_FROM_V1.items()
                     if _V1_LABELS[old].lower() in lowered), "")
    return "" if name == "Won" else name


# --------------------------------------------------------------------------- #
# Other vocabularies
# --------------------------------------------------------------------------- #
PRIORITIES = ["High", "Medium", "Low"]

# The touch log on each card went on 6 Oct 2026. The owner went the same day,
# but on 20 Oct 2026 the user asked for the name of whoever is responsible for
# each card, shown as initials: that is Contact.responsible, typed in the record.
# On 29 Oct 2026 the user asked for a log again, this time one per company
# (models.CompanyLog), of who there has been contacted. How it happened is one
# of these; the last one covers anything else.
LOG_METHODS = ["Email", "Call", "LinkedIn", "Meeting", "Other"]

# Offered when a card's sector tag is typed, beside every sector already in
# use. The user's own examples (20 Oct 2026); any other word can be typed.
SECTOR_SUGGESTIONS = ["Healthcare", "Dentistry", "Hospitality"]

# Days in one stage before a card is flagged as ageing / stalled on the board.
AGEING_WARN_DAYS = 14
AGEING_STALE_DAYS = 30
