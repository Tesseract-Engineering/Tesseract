"""
patterns.py - find the same request asked again and again.

Deterministic first: normalise each request's title, then group by word overlap. No model is needed to
say "Access to prod DB for Dana" and "Access to prod DB for Lee" are one request. Names of systems the
company has owners for are taken out first, so "read access to Grafana" and "read access to the prod
read replica" are one request too; which system an ask is about decides its approver, not its group.
Grouping runs over asks, not items: a ticket filed from a Slack ask goes where the ask goes. With a model
(judge.py), each item is first read into a plain phrase of what it asks for, grouping runs over those
phrases instead of titles, and `merge_hook` joins groups worded too differently for overlap to catch.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

import rec_weights as W
from linking import Links, asks
from rec_weights import parse_time, when

_KEY = re.compile(r"\b[A-Z][A-Z0-9]+-\d+\b|#\d+|\b\d[\w.:-]*\b")       # AUTH-12, #412, 2026-10-01, 10.0.0.1
_EMAIL = re.compile(r"\S+@\S+")
_HANDLE = re.compile(r"[@<]\S+")
_WORD = re.compile(r"[a-z]{3,}")
_STOP = frozenset("the and for with from please can you could would need needs want new request "
                  "help get got have has this that our your their into onto not".split())
_STEP = re.compile(r"^\s*(?:\d+[.)]|[-*]\s*\[[ xX]\]|[-*])\s+\S", re.M)


def words(item: dict, systems: tuple[str, ...] = ()) -> frozenset[str]:
    text = _EMAIL.sub(" ", item.get("title") or "")
    text = " " + re.sub(r"[^a-z0-9]+", " ", _HANDLE.sub(" ", _KEY.sub(" ", text)).lower()) + " "
    for s in systems:                      # longest first, so "prod read replica" goes before "prod"
        text = text.replace(f" {s} ", " ")
    return frozenset(w for w in _WORD.findall(text) if w not in _STOP)


def signature(item: dict, systems: tuple[str, ...] = ()) -> frozenset[str]:
    """What a request is grouped by: the model's phrase for it when there is one, else its title."""
    return words({"title": item["_request"]}, systems) if item.get("_request") else words(item, systems)


def jaccard(a: frozenset, b: frozenset) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


@dataclass
class Pattern:
    words: frozenset                       # the first member's words; the cluster's name comes from the common core
    items: list[dict] = field(default_factory=list)
    sims: list[float] = field(default_factory=list)     # each member's overlap with the founding member
    merges: list[dict] = field(default_factory=list)    # groups the model joined into this one, with its reason
    systems: tuple[str, ...] = ()                        # system names left out of the wording

    @property
    def label(self) -> str:
        """The words at least half the members share, most common first. Never one member's words alone:
        after a merge that would put a requester's name in the label."""
        seen = Counter(w for i in self.items for w in signature(i, self.systems))
        core = [w for w, n in seen.items() if n * 2 >= len(self.items)] or [w for w, _ in seen.most_common(3)]
        return " ".join(sorted(core, key=lambda w: (-seen[w], w)))


def in_window(item: dict, as_of: datetime) -> bool:
    when = parse_time(item.get("created_at"))
    return when is not None and as_of - timedelta(days=W.WINDOW_DAYS) <= when <= as_of


def bot_only(item: dict) -> bool:
    """Bot posts (PagerDuty, CI) with no person asking: alerts, not asks."""
    return bool((item.get("meta") or {}).get("bots")) and not requesters(item)


def requests(items: dict[str, dict], as_of: datetime) -> list[dict]:
    """The window's tickets and threads, oldest first: everything that might be somebody asking."""
    return sorted((i for i in items.values()
                   if i.get("kind") in W.REQUEST_KINDS and in_window(i, as_of) and not bot_only(i)),
                  key=when)


def detect(requests: list[dict],
           merge_hook: Callable[[list[Pattern]], list[Pattern]] | None = None,
           links: Links | None = None, systems: tuple[str, ...] = ()) -> list[Pattern]:
    """Group asks by their first item's wording; keep groups with at least MIN_OCCURRENCES items
    (score.rank then counts asks)."""
    units = asks(requests, links) if links else [[i] for i in requests]
    patterns: list[Pattern] = []
    for unit in units:
        w = signature(unit[0], systems) or next((s for i in unit[1:] if (s := signature(i, systems))), frozenset())
        best, best_sim = None, 0.0
        for p in patterns:
            sim = jaccard(w, p.words)
            if sim > best_sim:
                best, best_sim = p, sim
        if best is not None and best_sim >= W.SAME_REQUEST:
            best.items.extend(unit)
            best.sims.extend([best_sim] * len(unit))
        elif w:
            patterns.append(Pattern(words=w, items=list(unit), sims=[1.0] * len(unit), systems=systems))
    if merge_hook:
        patterns = merge_hook(patterns)
    return [p for p in patterns if len(p.items) >= W.MIN_OCCURRENCES]


def resolvers(item: dict) -> set[str]:
    return {p["id"] for p in item.get("people") or [] if set(p.get("roles") or []) & {"assignee", "owner"}}


def requesters(item: dict) -> set[str]:
    return {p["id"] for p in item.get("people") or [] if set(p.get("roles") or []) & {"author", "reporter"}}


def stepped(item: dict) -> bool:
    return len(_STEP.findall(item.get("text") or "")) >= 2
