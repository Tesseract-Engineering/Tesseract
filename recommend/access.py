"""
access.py - the report as one viewer may see it.

Same rules as graph/swarm_queries.py: a viewer's principals are user:{email}, domain:{domain} and every
group listing them (by email or by any of their source accounts), or listing a group of theirs, up to
GROUP_HOPS deep. An item is readable when it is
public, internal and the viewer works here, or restricted to a principal they hold. The agent runs on
readable items only, so a count, an owner or a step never comes from something the viewer can't open:
someone outside #acme-escalations sees 9 export asks, not 14.
"""

from __future__ import annotations

from dataclasses import dataclass

from directory import Directory


@dataclass(frozen=True)
class Viewer:
    email: str
    member: bool                    # a person the records know, so internal items are theirs to read
    principals: frozenset[str]


# Groups inside groups are followed this far, as graph/swarm_queries.py does (MEMBER_OF*1..6).
GROUP_HOPS = 6


def viewer(email: str, groups: dict[str, dict], directory: Directory) -> Viewer:
    email = email.lower()
    key = directory.by_email.get(email)
    ids = {f"user:{email}"} | (directory.ids.get(key, set()) if key else set())
    mine = {g["id"] for g in groups.values() if ids & set(g.get("members") or [])}
    frontier = set(mine)
    for _ in range(GROUP_HOPS - 1):          # a group listing one of mine as a member is mine too
        frontier = {g["id"] for g in groups.values() if frontier & set(g.get("members") or [])} - mine
        if not frontier:
            break
        mine |= frontier
    return Viewer(email, key is not None, frozenset({f"user:{email}", f"domain:{email.split('@')[-1]}", *mine}))


def readable(item: dict, v: Viewer) -> bool:
    acl = item.get("acl") or {}
    vis = acl.get("visibility")
    return (vis == "public" or (vis == "internal" and v.member)
            or bool(set(acl.get("principals") or []) & v.principals))


def visible(items: dict[str, dict], v: Viewer | None) -> dict[str, dict]:
    return items if v is None else {k: i for k, i in items.items() if readable(i, v)}
