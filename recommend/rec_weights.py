"""
weights.py - every number the recommendation agent uses, in one place.

Same rule as graph/weights.py: first guesses, tuned against the sandbox's answer key, and changing one
needs a re-score, never a re-run of the swarm.
"""

from datetime import datetime, timezone


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def when(item: dict) -> tuple:
    """Sort key for items by when they were made. Real time: Jira writes local time with an offset and
    Slack writes UTC, so the strings do not sort."""
    return (parse_time(item.get("created_at")) or _EPOCH, item["id"])


# Item kinds that are somebody asking for something. Commits, PRs, repos and containers are work or places.
REQUEST_KINDS = {"issue", "thread"}

WINDOW_DAYS = 90            # "asked N times this quarter"
MIN_OCCURRENCES = 3         # fewer than this is a coincidence, not a pattern

# Two titles are the same request when their word sets overlap this much (Jaccard) after normalising.
SAME_REQUEST = 0.6

# This many occurrences in the window scores 1.0 on frequency.
FREQ_SATURATES_AT = 50

# How the composite is built: how often it comes back, and how automatable it is. Must sum to 1.
# There is no time-cost axis: sandbox data is seeded in one sitting, so resolution times say nothing.
SCORE_WEIGHTS = {"frequency": 0.5, "definition": 0.5}

# How well-defined the steps are: what each signal contributes. Must sum to 1.
DEFINITION_WEIGHTS = {
    "runbook": 0.35,         # the instances link a document that lists the steps (runbooks.py)
    "stepped_text": 0.20,    # the instances themselves list steps (numbered, checklist)
    "same_resolver": 0.20,   # one person or team always handles it
    "tight_wording": 0.15,   # the requests read alike
    "same_place": 0.10,      # always filed in the same project or channel
}

# A runbook: linked from at least this many asks, and this share of them.
RUNBOOK_MIN_LINKS = 2
RUNBOOK_MIN_SHARE = 0.2
RUNBOOK_ENOUGH_LINKS = 3     # this many asks linking one document makes it the runbook, whatever the share
RUNBOOK_NO_STEPS = 0.5       # the runbook signal when the linked document lists no steps code can read
MAX_STEPS = 15

# "Why it ranks low" is said when the definition axis is below this.
LOW_DEFINITION = 0.45

for _w in (SCORE_WEIGHTS, DEFINITION_WEIGHTS):
    assert abs(sum(_w.values()) - 1.0) < 1e-9

# -- the model steps (judge.py). Off unless a model is given. ---------------------------------------- #

# READ sees an item's title and the start of its text: the ask is at the top of a ticket or thread, and a
# long thread's replies say how it was handled, which REVIEW reads. The cut is marked in the message.
READ_TEXT_CHARS = 4000
MAX_READ_ITEMS = 2000       # per run; items past this are grouped by title alone, and the report says so
MAX_MERGE_GROUPS = 400      # groups shown to MERGE at once, largest first; the rest are not merged
MERGE_HANDLED_CHARS = 160   # how much of each sample's last reply MERGE sees
REVIEW_TOP = 15             # how many of the best-ranked patterns REVIEW reads
REVIEW_ITEMS = 12           # items per REVIEW, newest first
REVIEW_TEXT_CHARS = 6000    # per item in REVIEW, marked when cut

# When REVIEW has judged a pattern, how much its same_steps counts against the code's signals.
MODEL_DEFINITION_SHARE = 0.5
