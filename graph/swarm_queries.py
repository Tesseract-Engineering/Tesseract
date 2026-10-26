"""
swarm_queries.py - read the swarm graph as one viewer: only what they may see, and aggregates
computed from only that.

A viewer is an email. Their principals are user:{email}, domain:{their domain}, and every Group
their Person is MEMBER_OF (group records, rule 7). A node is readable when it is public, or
internal and the viewer belongs to the company, or restricted to a principal they hold.

Aggregates are never stored (contract.py says why): "how much has this person done in this repo"
summed over items the viewer cannot read would reveal those items. So they are computed here, per
query, over readable items only.

    python swarm_queries.py sam@acme.com
"""

from __future__ import annotations

import sys

from store import Neo4jStore

READABLE = ("({n}.visibility = 'public' OR ({n}.visibility = 'internal' AND $member) "
            "OR any(g IN coalesce({n}.aclGroups, []) WHERE g IN $principals))")


def viewer(store: Neo4jStore, ns: str, email: str) -> dict:
    email = email.lower()
    rows = store._run(
        "OPTIONAL MATCH (p:Person {ns: $ns})-[:HAS_ACCOUNT]->(:Account {ns: $ns, email: $email}) "
        "OPTIONAL MATCH (p)-[:MEMBER_OF]->(g:Group {ns: $ns}) "
        "RETURN count(DISTINCT p) > 0 AS member, collect(DISTINCT g.id) AS groups", ns=ns, email=email)
    row = rows[0] if rows else {"member": False, "groups": []}
    return {"email": email, "member": bool(row["member"]),
            "principals": [f"user:{email}", f"domain:{email.split('@')[-1]}", *row["groups"]]}


def decisions(store: Neo4jStore, ns: str, v: dict) -> list[dict]:
    """Decisions the viewer may see, with who made them and where, from readable items only."""
    return store._run(
        f"MATCH (d:Decision {{ns: $ns}}) WHERE {READABLE.format(n='d')} "
        f"OPTIONAL MATCH (d)-[:DECIDED_IN]->(i {{ns: $ns}}) WHERE {READABLE.format(n='i')} "
        "OPTIONAL MATCH (d)-[:DECIDED_BY]->(:Account)<-[:HAS_ACCOUNT]-(p:Person) "
        "RETURN d.name AS decision, collect(DISTINCT p.name) AS by, collect(DISTINCT i.title) AS where",
        ns=ns, member=v["member"], principals=v["principals"])


def contributors(store: Neo4jStore, ns: str, v: dict) -> list[dict]:
    """Who has done the most in each repo, channel or folder, as this viewer can see it:
    the sum of role weights (strength x recency) over readable items only."""
    return store._run(
        "MATCH (p:Person {ns: $ns})-[:HAS_ACCOUNT]->(:Account)-[r]->(i {ns: $ns})-[:IN]->(c:Container {ns: $ns}) "
        f"WHERE r.weight IS NOT NULL AND {READABLE.format(n='i')} AND {READABLE.format(n='c')} "
        "RETURN c.title AS container, p.name AS person, round(sum(r.weight) * 100) / 100 AS weight, "
        "count(DISTINCT i) AS items ORDER BY container, weight DESC",
        ns=ns, member=v["member"], principals=v["principals"])


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
