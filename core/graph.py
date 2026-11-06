"""
graph.py - everything the bot reads from Neo4j, as one viewer.

The access rule is graph/swarm_queries.py's (READABLE, EDGE_READABLE, LIVE, viewer()); nothing here
restates it. One addition: `granted`, item ids a Drive owner shared with this viewer through the bot.
The Drive change is real, but the graph only learns it on the next swarm run and rebuild, so until
then the bot's own ledger (ledger.py) vouches for it, and only until the grant expires.

Unreadable items are found too, on purpose: the answer card says how many matching items the viewer
may not read and who owns them, so they can ask. Their text and titles never leave this module in a
viewer's result; only the owner's request card names them (cards.owner_request).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

from graph.store import Neo4jStore
from graph.swarm_queries import CONFIDENCE_FLOOR, EDGE_READABLE, LIVE, READABLE, viewer

# Readable as this viewer, or shared with them through the bot since the last build.
_READ = "(" + READABLE.format(n="n") + " OR n.id IN $granted)"

STOP = set("""a an and are as at be been but by can could did do does for from had has have how i if in into is it its
me my of on or our so than that the their them then there these they this to us was we were what when where which who
whom why will with would you your any anyone anything about last year use used using get got does did done know""".split())


@dataclass
class Who:
    """A person in the graph: their name, Drive or work email, and Slack user id when known."""
    person_id: str | None
    name: str
    email: str | None = None
    slack_id: str | None = None


@dataclass
class Hit:
    id: str
    label: str
    title: str
    url: str | None
    source: str
    kind: str
    text: str
    when: datetime | None
    score: float
    readable: bool
    authors: list[str] = field(default_factory=list)
    owners: list[Who] = field(default_factory=list)


def lucene(terms: list[str]) -> str:
    """Terms -> a Lucene OR query, every special character escaped."""
    safe = [re.sub(r'([+\-&|!(){}\[\]^"~*?:\\/])', r"\\\1", t) for t in terms if t.strip()]
    return " OR ".join(f'"{t}"' if " " in t else t for t in safe)


def keywords(question: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9_.-]*", question)
    out: list[str] = []
    for w in words:
        w = w.strip(".-")
        if len(w) >= 3 and w.lower() not in STOP and w.lower() not in (x.lower() for x in out):
            out.append(w)
    # Jira keys and ADR numbers are worth keeping whole even when short ("ML-1").
    out += [k for k in re.findall(r"\b[A-Z]{2,}-\d+\b", question) if k not in out]
    return out


def _when(value) -> datetime | None:
    if value is None:
        return None
    return value.to_native() if hasattr(value, "to_native") else value


class Graph:
    def __init__(self, store: Neo4jStore, ns: str, floor: float = CONFIDENCE_FLOOR):
        self.store = store
        self.ns = ns
        self.floor = floor

    def viewer(self, email: str) -> dict:
        return viewer(self.store, self.ns, email)

    def _params(self, v: dict, granted: list[str] | None = None, **more) -> dict:
        return {"ns": self.ns, "member": v["member"], "principals": v["principals"], "floor": self.floor,
                "granted": granted or [], **more}

    # -- search ------------------------------------------------------------------------------ #

    def search(self, v: dict, terms: list[str], granted: list[str] | None = None, limit: int = 8) -> list[Hit]:
        """Items matching any term, best first: up to `limit` the viewer may read and up to `limit`
        they may not. Readable ones also come from extracted entities (a Decision named in the
        question leads to the items that mention it), through edges the viewer may read."""
        q = lucene(terms)
        if not q:
            return []
        params = self._params(v, granted, q=q, limit=limit)
        rows = self.store._run(
            "CALL db.index.fulltext.queryNodes('knowledgeSearch', $q) YIELD node AS n, score "
            f"WHERE n.ns = $ns WITH n, score, {_READ} AS readable "
            "ORDER BY score DESC "
            "WITH readable, collect({n: n, score: score})[..$limit] AS top UNWIND top AS t "
            "RETURN t.n AS n, t.score AS score, readable", **params)
        rows += self.store._run(
            "CALL db.index.fulltext.queryNodes('entitySearch', $q) YIELD node AS e, score "
            f"WHERE e.ns = $ns AND {READABLE.format(n='e')} AND NOT e:Container "
            "MATCH (n {ns: $ns})-[m:MENTIONS]->(e) "
            f"WHERE {EDGE_READABLE.format(n='m')} AND {LIVE.format(r='m')} AND {_READ} "
            "RETURN n, max(score) * 0.8 AS score, true AS readable ORDER BY score DESC LIMIT $limit", **params)
        hits: dict[str, Hit] = {}
        for r in rows:
            n = r["n"]
            if n["id"] in hits:
                hits[n["id"]].score = max(hits[n["id"]].score, r["score"])
                continue
            hits[n["id"]] = Hit(id=n["id"], label=next(iter(n.labels)), title=n.get("title") or n["id"],
                                url=n.get("url"), source=n.get("source") or "", kind=n.get("kind") or "",
                                text=n.get("text") or n.get("body") or "",
                                when=_when(n.get("sourceUpdatedAt") or n.get("createdAt")),
                                score=float(r["score"]), readable=bool(r["readable"]))
        out = sorted(hits.values(), key=lambda h: -h.score)
        self._people(out)
        return out

    def _people(self, hits: list[Hit]) -> None:
        """Authors (for the citation line) and owners (who to ask), from field-backed role edges."""
        if not hits:
            return
        rows = self.store._run(
            "UNWIND $ids AS id MATCH (n {ns: $ns, id: id}) "
            "MATCH (a:Account {ns: $ns})-[r:OWNS|AUTHORED]->(n) WHERE r.supersededAt IS NULL "
            "OPTIONAL MATCH (p:Person {ns: $ns})-[:HAS_ACCOUNT]->(a) "
            "OPTIONAL MATCH (p)-[:HAS_ACCOUNT]->(s:Account {ns: $ns, source: 'slack'}) "
            "OPTIONAL MATCH (p)-[:HAS_ACCOUNT]->(m:Account {ns: $ns}) WHERE m.email IS NOT NULL "
            "WITH id, r, a, p, collect(DISTINCT m.email) AS emails, collect(DISTINCT s.handle) AS slacks "
            "RETURN id, type(r) AS role, p.id AS pid, coalesce(p.name, a.name, a.handle) AS name, "
            "coalesce(a.email, head(emails)) AS email, head(slacks) AS slack, coalesce(p.isBot, false) AS bot",
            ns=self.ns, ids=[h.id for h in hits])
        by_id = {h.id: h for h in hits}
        for r in rows:
            if r["bot"]:
                continue
            h = by_id[r["id"]]
            if r["name"] and r["name"] not in h.authors:
                h.authors.append(r["name"])
            if r["role"] == "OWNS" and not any(o.person_id == r["pid"] and o.name == r["name"] for o in h.owners):
                h.owners.append(Who(r["pid"], r["name"], r["email"], r["slack"]))
        for h in hits:   # no owner field (a Slack thread, a Jira ticket): its author is who to ask
            if not h.owners:
                mine = [r for r in rows if r["id"] == h.id and not r["bot"]]
                if mine:
                    h.owners.append(Who(mine[0]["pid"], mine[0]["name"], mine[0]["email"], mine[0]["slack"]))

    def items(self, ids: list[str]) -> list[Hit]:
        """Items by id, with no access check: only for the owner's own request card and for
        re-reading items after their owner granted access."""
        rows = self.store._run("UNWIND $ids AS id MATCH (n {ns: $ns, id: id}) RETURN n", ns=self.ns, ids=ids)
        hits = [Hit(id=r["n"]["id"], label=next(iter(r["n"].labels)), title=r["n"].get("title") or r["n"]["id"],
                    url=r["n"].get("url"), source=r["n"].get("source") or "", kind=r["n"].get("kind") or "",
                    text=r["n"].get("text") or r["n"].get("body") or "",
                    when=_when(r["n"].get("sourceUpdatedAt") or r["n"].get("createdAt")), score=0.0, readable=False)
                for r in rows]
        self._people(hits)
        return hits

    def readable_ids(self, v: dict, ids: list[str], granted: list[str] | None = None) -> set[str]:
        rows = self.store._run(f"UNWIND $ids AS id MATCH (n {{ns: $ns, id: id}}) WHERE {_READ} RETURN n.id AS id",
                               **self._params(v, granted, ids=ids))
        return {r["id"] for r in rows}

    def person_by_slack(self, slack_id: str) -> Who | None:
        rows = self.store._run(
            "MATCH (p:Person {ns: $ns})-[:HAS_ACCOUNT]->(:Account {ns: $ns, source: 'slack', handle: $h}) "
            "OPTIONAL MATCH (p)-[:HAS_ACCOUNT]->(m:Account {ns: $ns}) WHERE m.email IS NOT NULL "
            "RETURN p.id AS pid, p.name AS name, collect(DISTINCT m.email) AS emails", ns=self.ns, h=slack_id)
        if not rows:
            return None
        return Who(rows[0]["pid"], rows[0]["name"], (rows[0]["emails"] or [None])[0], slack_id)
