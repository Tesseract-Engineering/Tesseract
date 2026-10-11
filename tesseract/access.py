"""access.py - who is asking decides what can be read.

A record is readable when its visibility is open (internal or public), or when
the viewer holds one of its aclGroups. Everything else is counted and named,
never quoted: a sealed record appears as a stub that says who to ask.

Viewer presets come from the company file (config.py); a caller may also pass
an explicit {"name", "acl_groups"}.
"""

from __future__ import annotations

from . import graph
from .config import settings

OPEN_VISIBILITY = ("internal", "public")

# Cypher predicate over `node`, given $acl (the viewer's groups).
READABLE = ("(coalesce(node.visibility, 'internal') IN ['internal', 'public'] "
            "OR any(g IN coalesce(node.aclGroups, []) WHERE g IN $acl))")


def viewers() -> dict[str, dict]:
    return {k: v.as_dict() for k, v in settings().company.viewers.items()}


def default_viewer() -> dict:
    company = settings().company
    return company.viewers[company.default_viewer].as_dict()


def viewer_of(viewer: dict | str | None) -> dict:
    """A preset name, a {"name", "acl_groups"} dict, or None (the default viewer)."""
    if viewer is None:
        return default_viewer()
    if isinstance(viewer, str):
        presets = viewers()
        if viewer not in presets:
            raise ValueError(f"unknown viewer {viewer!r}; presets: {', '.join(presets)}")
        return presets[viewer]
    if not isinstance(viewer, dict):
        raise ValueError("viewer must be a preset name or {name, acl_groups}")
    groups = viewer.get("acl_groups") or []
    if not isinstance(groups, list) or not all(isinstance(g, str) for g in groups):
        raise ValueError("viewer.acl_groups must be a list of strings")
    return {"name": str(viewer.get("name") or "viewer"), "acl_groups": groups}


def readable(visibility: str | None, acl: list | None, viewer: dict) -> bool:
    if (visibility or "internal") in OPEN_VISIBILITY:
        return True
    return bool(set(acl or []) & set(viewer.get("acl_groups") or []))


def grant(acl: list | None, viewer: dict) -> str:
    """Which of the viewer's groups opens this record."""
    return next((g for g in acl or [] if g in (viewer.get("acl_groups") or [])), "")


def owner_of(acl: list | None) -> str:
    """Who to ask for access, read off the ACL groups themselves."""
    names = []
    for g in acl or []:
        if g.startswith("sharepoint:"):
            names.append(g.split(":", 1)[1].replace("-", " ").title())
        elif ":user:" in g:
            names.append(g.split(":user:", 1)[1])
        elif g.startswith("slack:"):
            names.append(f"the #{g.split(':', 1)[1].lstrip('#')} channel owners")
        else:
            names.append(g)
    return ", ".join(dict.fromkeys(names)) or "the document owner"


def visibility_of(ids: list[str]) -> dict[str, tuple[str, list]]:
    """node id -> (visibility, aclGroups)."""
    ids = [i for i in ids if i]
    if not ids:
        return {}
    rows = graph.run("MATCH (n {ns: $ns}) WHERE n.id IN $ids "
                     "RETURN n.id AS id, n.visibility AS v, coalesce(n.aclGroups, []) AS acl", ids=ids)
    return {r["id"]: (r["v"], list(r["acl"])) for r in rows}


def restricted_note(rows: list[dict]) -> str:
    """'N matching documents are restricted (groups) ... ask X for access.'"""
    if not rows:
        return ""
    groups = list(dict.fromkeys(g for r in rows for g in r["acl"]))
    n = len(rows)
    return (f"{n} matching document{'s are' if n != 1 else ' is'} restricted "
            f"({', '.join(groups)}) and {'were' if n != 1 else 'was'} not read; "
            f"ask {owner_of(groups)} for access.")
