"""
owners.py - who owns each system, from what the company wrote down, and which system an ask is about.

The approver for an ask is the owner of the system it asks about, never whoever approved last time:
a wrong person approving once must not make them the approver. Owners come from, in order of trust:

  1. a table with an Owner column, in any document the viewer can read (the handbook's "Service
     owners"): | Service | Team | Owner |. Several owners ("Dhruv Pendharkar and Hamilton Park") all approve.
  2. the catch-all line of a repo's CODEOWNERS (`*  @ishan-sinha-ucb`), for the repo itself
  3. an `owns` relation the swarm's model extracted (person owns system), when nothing above names one

Every owner keeps the item it came from, so the report can link the line that says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from directory import Directory, norm_name

_SPLIT = re.compile(r"\s*(?:,|&|\+|/|;|\band\b)\s*")
_CELLS = re.compile(r"^\s*\|(.+)\|\s*$")
_PAREN = re.compile(r"\(([^)]*)\)")


def _names(cell: str) -> tuple[str, list[str]]:
    """A system cell as written -> (display name, aliases). "Grafana (staging and prod dashboards)" is
    Grafana; "prod read replica (`pg-prod-ro`)" is also pg-prod-ro, since a one-word aside is an
    identifier; "ml-platform and the retrain pipeline" is two names for one owner."""
    cell = cell.strip("`*_ ")
    base = _PAREN.sub(" ", cell).replace("`", "").strip()
    asides = [a.strip("` ") for a in _PAREN.findall(cell) if a.strip("` ") and " " not in a.strip("` ")]
    parts = [re.sub(r"^the\s+", "", p.strip(), flags=re.I) for p in re.split(r"\s+and\s+", base) if p.strip()]
    return base, [*parts, *asides]


@dataclass
class Owner:
    system: str                              # as written
    people: list[str]                        # directory keys, or the name as written when nobody matches
    source: str                              # item id it came from
    url: str | None
    how: str                                 # "owners table" | "CODEOWNERS" | "extracted"
    aliases: set[str] = field(default_factory=set)


def _table_owners(item: dict) -> list[tuple[str, list[str]]]:
    rows, header = [], None
    for line in (item.get("text") or "").splitlines():
        m = _CELLS.match(line)
        if not m:
            header = None
            continue
        cells = [c.strip() for c in m.group(1).split("|")]
        if all(re.fullmatch(r":?-{2,}:?", c) for c in cells if c):
            continue
        if header is None:
            header = [c.lower() for c in cells]
            continue
        col = next((n for n, h in enumerate(header) if "owner" in h), None)
        if col is None or col == 0 or col >= len(cells) or not cells[0]:
            continue
        rows.append((cells[0], [p for p in _SPLIT.split(_PAREN.sub(" ", cells[col]).strip()) if p]))
    return rows


def _codeowners(item: dict) -> list[str]:
    for line in (item.get("text") or "").splitlines():
        parts = line.split("#")[0].split()
        if parts and parts[0] == "*":
            return parts[1:]
    return []


def owners(items: dict[str, dict], directory: Directory, extractions: dict[str, dict] | None = None) -> list[Owner]:
    found: dict[str, Owner] = {}

    def add(system: str, people: list[str], item: dict, how: str, aliases=()) -> None:
        key = norm_name(system)
        if not key or key in found:          # earlier sources win
            return
        resolved = [directory.find(p) or p for p in people]
        found[key] = Owner(system, resolved, item["id"], item.get("url"), how, {key, *map(norm_name, aliases)})

    docs = [i for i in items.values() if i.get("kind") == "document"]
    for d in docs:
        for cell, people in _table_owners(d):
            system, aliases = _names(cell)
            add(system, people, d, "owners table", aliases=aliases)
    for d in docs:
        if (d.get("meta") or {}).get("path", "").endswith("CODEOWNERS"):
            repo = items.get(d["id"].split(":file:")[0]) or {}
            name = (repo.get("title") or "").split("/")[-1]
            if name and (handles := _codeowners(d)):
                add(name, handles, d, "CODEOWNERS", aliases=[f"{name} repo"])
    for ex in (extractions or {}).values():
        if ex["item_id"] not in items:
            continue
        ents = {e["local_id"]: e for e in ex.get("entities") or []}
        for r in ex.get("relations") or []:
            target = ents.get(r.get("target"))
            if r.get("type") == "owns" and target and target.get("type") == "system":
                add(target["name"], [r["source"]], items[ex["item_id"]], "extracted")
    for o in found.values():
        o.people = [directory.key(p) if p.startswith("person:") else p for p in o.people]
    return list(found.values())


def _norm_text(*parts: str) -> str:
    return " " + norm_name(" ".join(p for p in parts if p)) + " "


def system_of(item: dict, known: list[Owner], deep: bool = True) -> Owner | None:
    """The system an ask is about: the longest known system name in the model's system field, then its
    phrase, then the title, then (with `deep`) the text. The text comes last because a body may carry a
    checklist naming every system the steps touch ("switch auth-service's signer, check Grafana")."""
    for text in (item.get("_system"), item.get("_request"), item.get("title"), item.get("text") if deep else None):
        if not text:
            continue
        hay = _norm_text(text)
        hits = [(len(a), o) for o in known for a in o.aliases if f" {a} " in hay]
        if hits:
            return max(hits, key=lambda h: h[0])[1]
    return None
