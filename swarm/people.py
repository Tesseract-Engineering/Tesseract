"""
people.py - the company's people, for finding the ones a text names.

An item's people list holds who acted on it: the author, assignee,
commenters, a document's owner. A text also names people who did not touch
it ("Ishan to write up the options"), and an action item or decision about
them should link to them. The identity pass publishes every person it
resolved, with every name seen for them, in state/identity/people.json; the
extraction stage reads it and offers the model the people a text names, by
their resolved id (person:c:...), so what it links reaches that person's
work in every tool.

Matching is plain code and errs toward missing a name rather than linking
the wrong person:

  full name           "Ishan Sinha", any case
  initial, surname    "D. Pendharkar", "D Pendharkar"
  first name alone    "Ishan", capitalised as a name, and only when no one
                      else in the company has that first name

A name that is an email or a handle (no space, or an @) is never matched.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from .stages.write import state_key
from .store import ObjectStore

MIN_FIRST_NAME = 3


def _words(name: str) -> list[str]:
    return re.findall(r"[^\W\d_]+(?:['-][^\W\d_]+)*", name)


@dataclass
class Directory:
    # person_id -> {"names": [...], "members": [source identity ids]}
    people: dict[str, dict[str, Any]] = field(default_factory=dict)
    _patterns: list[tuple[re.Pattern[str], str]] = field(default_factory=list, init=False, repr=False)
    _member_of: dict[str, str] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        firsts: dict[str, set[str]] = defaultdict(set)
        full: list[tuple[re.Pattern[str], str]] = []
        for person_id, entry in sorted(self.people.items()):
            for member in entry.get("members", []):
                self._member_of[member] = person_id
            for name in entry.get("names", []):
                if "@" in name:
                    continue
                words = _words(name)
                if len(words) >= 2:
                    first, last = words[0], words[-1]
                    full.append((re.compile(rf"\b{re.escape(first)}\s+{re.escape(last)}\b", re.I), person_id))
                    full.append((re.compile(rf"\b{re.escape(first[0])}\.?\s+{re.escape(last)}\b"), person_id))
                    if len(first) >= MIN_FIRST_NAME:
                        firsts[first.lower()].add(person_id)
                elif len(words) == 1 and " " not in name.strip() and name.strip()[:1].isupper() \
                        and len(words[0]) >= MIN_FIRST_NAME and words[0] == name.strip():
                    firsts[words[0].lower()].add(person_id)
        alone = [(re.compile(rf"\b{re.escape(first.capitalize())}\b"), next(iter(ids)))
                 for first, ids in sorted(firsts.items()) if len(ids) == 1]
        self._patterns = full + alone

    @classmethod
    def from_store(cls, store: ObjectStore) -> Directory:
        return cls(json.loads(store.get(state_key("identity", "people")) or b"{}"))

    def person_of(self, source_identity: str) -> str | None:
        """The resolved person a source identity (person:slack:U123) belongs to."""
        return self._member_of.get(source_identity)

    def named_in(self, text: str) -> list[tuple[str, str]]:
        """(person_id, the name as written) for each person the text names,
        in the order they first appear."""
        found: dict[str, tuple[int, str]] = {}
        for pattern, person_id in self._patterns:
            m = pattern.search(text)
            if m and (person_id not in found or m.start() < found[person_id][0]):
                found[person_id] = (m.start(), m.group(0))
        return [(pid, name) for pid, (_, name) in sorted(found.items(), key=lambda kv: kv[1][0])]

    def names(self, person_id: str) -> list[str]:
        return self.people.get(person_id, {}).get("names", [])
