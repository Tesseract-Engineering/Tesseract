"""
swarm_queries.py - read the swarm graph as one viewer: only what they may see, and aggregates
computed from only that. Anything that reads the graph for a person (forge's MCP tools, a chat
front end) should import READABLE, EDGE_READABLE, LIVE and viewer() from here, so the rule
cannot drift. Code that holds a node's properties rather than a query uses readable() and
edge_readable(), the same rule in Python. Queries go through Neo4jStore.read.

A viewer is an email. Their principals are user:{email}, domain:{their domain}, and every Group
their Person is MEMBER_OF, directly or through groups inside groups. A node is readable when it is
public, or internal and the viewer belongs to the company, or restricted to a principal they hold.
A node with no visibility is not readable.

Edges extracted by the model carry the access of the items they were read from (visibility,
aclGroups), so the same rule applies to them: a Decision can be readable while the DECIDED_BY edge
read from a private thread is not. Edges without their own access (roles, refs) are only as
readable as their endpoints, which every query here checks.

Aggregates are never stored (contract.py says why): "how much has this person done in this repo"
summed over items the viewer cannot read would reveal those items. So they are computed here, per
query, over readable items only.

    python -m graph.swarm_queries sam@acme.com
"""

from __future__ import annotations

import sys

from .store import Neo4jStore

_RULE = ("({v} = 'public' OR ({v} = 'internal' AND $member) "
         "OR any(g IN coalesce({n}.aclGroups, []) WHERE g IN $principals))")
# Nodes: format with n=<variable>.
READABLE = _RULE.replace("{v}", "{n}.visibility")
# Edges: one without its own visibility is left to its endpoints.
EDGE_READABLE = _RULE.replace("{v}", "coalesce({n}.visibility, 'public')")

# The current version of a fact, at or above the confidence floor. Superseded edges stay in the
# graph for history; edges under the floor (escalated: a link spotted in prose, a weak extraction)
# are stored but left out unless a caller lowers the floor.
CONFIDENCE_FLOOR = 0.80
LIVE = "({r}.supersededAt IS NULL AND coalesce({r}.confidence, 1.0) >= $floor)"


def readable(visibility: str | None, acl_groups: list[str] | None, v: dict) -> bool:
    """READABLE, in Python: for code holding a node's properties rather than a query."""
    return (visibility == "public" or (visibility == "internal" and bool(v["member"]))
            or bool(set(acl_groups or []) & set(v["principals"])))


def edge_readable(visibility: str | None, acl_groups: list[str] | None, v: dict) -> bool:
    """EDGE_READABLE, in Python: an edge with no visibility of its own is left to its endpoints."""
    return readable(visibility or "public", acl_groups, v)


def viewer(store: Neo4jStore, ns: str, email: str) -> dict:
    email = email.lower()
    rows = store.read(
        "OPTIONAL MATCH (p:Person {ns: $ns})-[:HAS_ACCOUNT]->(:Account {ns: $ns, email: $email}) "
        "OPTIONAL MATCH (p)-[m:MEMBER_OF*1..6]->(g:Group {ns: $ns}) "
        "WHERE all(r IN m WHERE r.supersededAt IS NULL) "
        "RETURN count(DISTINCT p) > 0 AS member, collect(DISTINCT g.id) AS groups", ns=ns, email=email)
    row = rows[0] if rows else {"member": False, "groups": []}
    return {"email": email, "member": bool(row["member"]),
            "principals": [f"user:{email}", f"domain:{email.split('@')[-1]}", *row["groups"]]}


def _params(ns: str, v: dict, floor: float) -> dict:
    return {"ns": ns, "member": v["member"], "principals": v["principals"], "floor": floor}


def decisions(store: Neo4jStore, ns: str, v: dict, floor: float = CONFIDENCE_FLOOR) -> list[dict]:
    """Decisions the viewer may see, with who made them and where. Who and where come only from
    edges the viewer may read, read from items the viewer may read."""
    return store.read(
        f"MATCH (d:Decision {{ns: $ns}}) WHERE {READABLE.format(n='d')} "
        f"OPTIONAL MATCH (d)-[w:DECIDED_IN]->(i {{ns: $ns}}) "
        f"  WHERE {EDGE_READABLE.format(n='w')} AND {LIVE.format(r='w')} AND {READABLE.format(n='i')} "
        f"OPTIONAL MATCH (d)-[b:DECIDED_BY]->(who {{ns: $ns}}) "
        f"  WHERE {EDGE_READABLE.format(n='b')} AND {LIVE.format(r='b')} "
        "OPTIONAL MATCH (who)<-[:HAS_ACCOUNT]-(owner:Person) "
        "RETURN d.name AS decision, collect(DISTINCT coalesce(owner.name, who.name, who.handle)) AS by, "
        "collect(DISTINCT i.title) AS where",
        **_params(ns, v, floor))


def contributors(store: Neo4jStore, ns: str, v: dict, floor: float = CONFIDENCE_FLOOR) -> list[dict]:
    """Who has done the most in each repo, channel, folder or Jira project, as this viewer can see
    it: the sum of role weights over readable items only. Role edges point either way (an assignee
    edge runs item -> account), so both directions count. Bots are left out."""
    return store.read(
        "MATCH (p:Person {ns: $ns})-[:HAS_ACCOUNT]->(a:Account)-[r]-(i {ns: $ns})-[:IN]->(c:Container {ns: $ns}) "
        f"WHERE r.weight IS NOT NULL AND {LIVE.format(r='r')} AND NOT coalesce(p.isBot, false) "
        f"AND {READABLE.format(n='i')} AND {READABLE.format(n='c')} "
        "RETURN c.title AS container, p.name AS person, round(sum(r.weight) * 100) / 100 AS weight, "
        "count(DISTINCT i) AS items ORDER BY container, weight DESC",
        **_params(ns, v, floor))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ns = "kg:swarm"
    store = Neo4jStore()
    try:
        for email in sys.argv[1:] or ["sam@acme.com"]:
            v = viewer(store, ns, email)
            print(f"\n{email}: member={v['member']} principals={v['principals']}")
            print("  decisions:", decisions(store, ns, v) or "none visible")
            for row in contributors(store, ns, v):
                print(f"  {row['container']:<12} {row['person']:<10} {row['weight']:>5}  ({row['items']} items)")
    finally:
        store.close()
