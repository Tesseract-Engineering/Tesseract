"""
timeutil.py - the one timestamp format the swarm writes and reads back.

Checkpoint cursors, fetched_at stamps and run ids all use whole-second UTC
with a Z, so two of them compare correctly as strings.
"""

from __future__ import annotations

from datetime import datetime, timezone


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))
