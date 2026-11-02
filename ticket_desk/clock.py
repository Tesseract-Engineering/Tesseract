"""
clock.py - the desk's clock. Unset, it is the real time. TICKET_DESK_NOW=2026-10-29T20:00:00Z starts it
at that moment when the desk starts, and it runs on from there: the seed tickets are dated late
October, so a demo on a later day still shows their first-response SLAs, and the agent's "today", as
they were when the saved runs were made (as ADDIN_TODAY does for the Docs add-in).
"""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone

_START = os.environ.get("TICKET_DESK_NOW", "").strip()
_SKEW = datetime.fromisoformat(_START).timestamp() - time.time() if _START else 0.0


def now() -> datetime:
    return datetime.fromtimestamp(time.time() + _SKEW, timezone.utc)


def stamp() -> str:
    return now().strftime("%Y-%m-%dT%H:%M:%SZ")
