"""
runbooks.py - the document a pattern's instances keep linking to, and the steps it lists.

A document linked from at least RUNBOOK_MIN_LINKS asks and RUNBOOK_MIN_SHARE of them, or from at least
RUNBOOK_ENOUGH_LINKS asks whatever the share, is the pattern's runbook (an access policy linked from 5 of
24 asks is still the access policy). Its steps are its first ordered list (numbered or checkbox lines), else its first bulleted
list. A runbook with steps is the strongest sign an ask can be automated: someone already wrote down
what to do every time.
"""

from __future__ import annotations

import re
from collections import Counter

import rec_weights as W
from linking import Links

_ORDERED = re.compile(r"^\s*(?:\d+[.)]|[-*]\s*\[[ xX]\])\s+(.+?)\s*$")
_BULLET = re.compile(r"^\s*[-*•]\s+(.+?)\s*$")


def steps_in(text: str) -> list[str]:
    for pattern in (_ORDERED, _BULLET):
        block: list[str] = []
        for line in (text or "").splitlines():
            m = pattern.match(line)
            if m:
                block.append(m.group(1))
            elif block and line[:1].isspace() and line.strip():
                block[-1] += " " + line.strip()      # a step that wraps onto an indented second line
            elif block and line.strip():
                break
        if len(block) >= 2:
            return block[:W.MAX_STEPS]
    return []


def runbook(asks: list[list[dict]], links: Links) -> dict | None:
    linked = Counter(d for ask in asks for d in set().union(*(links.documents(i["id"]) for i in ask)))
    if not linked:
        return None
    doc_id, n = linked.most_common(1)[0]
    if n < W.RUNBOOK_ENOUGH_LINKS and (n < W.RUNBOOK_MIN_LINKS or n < W.RUNBOOK_MIN_SHARE * len(asks)):
        return None
    doc = links.items[doc_id]
    return {"id": doc_id, "title": doc.get("title"), "url": doc.get("url"), "linked_by": n,
            "steps": steps_in(doc.get("text") or "")}
