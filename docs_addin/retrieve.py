"""
retrieve.py - what the graph holds about one question, as one viewer may see it.

The sidebar asks a question (an auditor's, a reviewer's, the doc author's own); this finds the
items that bear on it and hands them to answer.py. Every read goes through graph/swarm_queries.py's
READABLE, EDGE_READABLE and LIVE, so the add-in sees exactly what the viewer could open themselves.

    keyword search     knowledgeSearch over item text, entitySearch over decisions, systems and
                       projects (an entity leads to the items it was decided in or mentioned by)
    one hop out        refs from the best items (SBX-5 -> OPS-1, the design doc -> PR #4), so the
                       runbook a ticket points at comes along with the ticket
    locked             items a readable source points at but the viewer may not open

Locked items are reported as a count per source and the people who could share them, never by
title or text. Only items a readable source already links to are counted: a search hit the
viewer cannot read is dropped without a trace, since even "3 hidden matches for 'layoffs'" says
too much.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from graph.store import Neo4jStore
from graph.swarm_queries import CONFIDENCE_FLOOR, EDGE_READABLE, LIVE, READABLE, viewer

ITEM_LABELS = ("Document", "Issue", "Commit")
LINKS = ["REFERENCES", "BLOCKS", "CLOSES", "IMPLEMENTS", "DUPLICATES"]
MAX_SOURCES = 20
EXCERPT = 1500                      # characters of an item's text the model sees
HOP_DECAY = 0.5                     # a linked item ranks below the item that links it
ENTITY_DECAY = 0.6                  # an item reached through a decision or system

_STOP = set("""a an and are as at be been by can could did do does for from has have how i if in
into is it its of on or our should that the their them then there these they this to under was
we were what when where which who whom why will with would you your provide evidence
any all also each""".split())


@dataclass
class Source:
    id: str
    title: str
    source: str                     # github | jira | slack | gdrive
    kind: str
    url: str | None
    alias: str | None               # SBX-4, tesseract-sandbox/auth-service#4, the channel
    updated: str | None
    container: str | None
    people: list[str]
    text: str
    decisions: list[str]            # decisions recorded as made in this item
    score: float


@dataclass
class Locked:
    count: int = 0
    by_source: dict[str, int] = field(default_factory=dict)
    owners: list[dict] = field(default_factory=list)    # [{"name", "email"}], who could share them


@dataclass
class Context:
    viewer: dict
    sources: list[Source]
    locked: Locked


_SUFFIX = re.compile(r"(ations?|ions?|ed|ing|es|e|s)$")
_INFLECT = ("e", "es", "ed", "ing", "ion", "s")


def search_terms(question: str) -> str:
    """A Lucene query from free text: the words that carry meaning and their common inflections,
    OR'd. The index does not stem, and "deprecated" should find "Deprecate"; exact variants rather
    than a `deprecat*` wildcard, because Lucene gives wildcard matches a flat score. Only [a-z0-9]
    survive, so nothing in a question can reach Lucene's syntax."""
    terms: list[str] = []
    for w in re.findall(r"[A-Za-z0-9]+", question.lower()):
        if len(w) < 3 or w in _STOP:
            continue
        stem = _SUFFIX.sub("", w)
        variants = [w] if len(stem) < 4 else [w, stem, *(stem + s for s in _INFLECT)]
        for t in variants:
            if t not in terms:
                terms.append(t)
    return " ".join(terms)


def paragraph_query(paragraph: str, section: str | None) -> str:
    """What to search for when the writer is on a paragraph: its section heading helps find the
    right records ("Rollout", "Token validation")."""
    return f"{section}. {paragraph}" if section else paragraph


def _normal(text: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def asks(item_text: str, question: str) -> bool:
    """The item quotes the question: it is where the question came from (the auditor's list),
    not evidence for an answer."""
    q = _normal(question)
    return len(q) > 30 and q in _normal(item_text)


# Whose account names to trust for a display name: people set their Slack name themselves, and a
# GitHub profile name can be anything (a sandbox account's says "Raj Patel").
NAME_SOURCES = ("slack", "jira", "gdrive", "github")


def display_name(person: str | None, accounts: list[dict]) -> str | None:
    """A Person node's name can be a Drive handle (dhruv.tesseract1); the accounts usually hold a
    real one. Prefer a full name from the most trusted source."""
    if person and " " in person:
        return person
    full = [a for a in accounts if a.get("name") and " " in a["name"] and "." not in a["name"].split()[0]]
    full.sort(key=lambda a: NAME_SOURCES.index(a["source"]) if a.get("source") in NAME_SOURCES else len(NAME_SOURCES))
    return full[0]["name"] if full else person


def _params(ns: str, v: dict) -> dict:
    return {"ns": ns, "member": v["member"], "principals": v["principals"], "floor": CONFIDENCE_FLOOR}


def _hits(store: Neo4jStore, ns: str, v: dict, terms: str, exclude: list[str]) -> dict[str, float]:
    if not terms:
        return {}
    found: dict[str, float] = {}
    for row in store._run(
            "CALL db.index.fulltext.queryNodes('knowledgeSearch', $terms) YIELD node, score "
            f"WHERE node.ns = $ns AND NOT node.id IN $exclude AND {READABLE.format(n='node')} "
            "RETURN node.id AS id, score ORDER BY score DESC LIMIT 24",
            terms=terms, exclude=exclude, **_params(ns, v)):
        found[row["id"]] = row["score"]
    # A decision or system named in the question leads to the items it came from.
    for row in store._run(
            "CALL db.index.fulltext.queryNodes('entitySearch', $terms) YIELD node, score "
            f"WHERE node.ns = $ns AND {READABLE.format(n='node')} "
            "WITH node, score ORDER BY score DESC LIMIT 8 "
            "MATCH (node)-[r:DECIDED_IN|MENTIONS]-(i {ns: $ns}) "
            f"WHERE (i:Document OR i:Issue OR i:Commit) AND NOT i.id IN $exclude "
            f"AND {EDGE_READABLE.format(n='r')} AND {LIVE.format(r='r')} AND {READABLE.format(n='i')} "
            "RETURN i.id AS id, max(score) AS score",
            terms=terms, exclude=exclude, **_params(ns, v)):
        found[row["id"]] = max(found.get(row["id"], 0.0), row["score"] * ENTITY_DECAY)
    return found


def _hop(store: Neo4jStore, ns: str, v: dict, ids: list[str], exclude: list[str]):
    """Items linked to `ids`, readable ones with the score of the item that links them, and the
    unreadable ones a readable item points at (the locked ones)."""
    readable, locked = {}, []
    for row in store._run(
            "MATCH (a {ns: $ns})-[r]-(b {ns: $ns}) "
            "WHERE a.id IN $ids AND type(r) IN $links AND (b:Document OR b:Issue OR b:Commit) "
            "AND NOT b.id IN $exclude "
            f"AND {LIVE.format(r='r')} "
            f"RETURN a.id AS via, b.id AS id, startNode(r) = a AS outward, {READABLE.format(n='b')} AS ok",
            ids=ids, exclude=exclude, links=LINKS, **_params(ns, v)):
        if row["ok"]:
            readable.setdefault(row["id"], row["via"])
        elif row["outward"]:
            locked.append(row["id"])
    return readable, sorted(set(locked))


def _items(store: Neo4jStore, ns: str, v: dict, ids: list[str]) -> list[dict]:
    return store._run(
        "MATCH (i {ns: $ns}) WHERE i.id IN $ids "
        "OPTIONAL MATCH (i)-[:IN]->(c:Container {ns: $ns}) "
        f"  WHERE {READABLE.format(n='c')} "
        "OPTIONAL MATCH (acct:Account)-[w:AUTHORED]->(i) "
        "OPTIONAL MATCH (p:Person)-[:HAS_ACCOUNT]->(acct) "
        "OPTIONAL MATCH (p)-[:HAS_ACCOUNT]->(other:Account) "
        "WITH i, c, acct, p, collect(DISTINCT {name: other.name, source: other.source}) AS names "
        "WITH i, c, collect(DISTINCT {person: p.name, names: names, account: acct.name}) AS authors "
        "OPTIONAL MATCH (d:Decision {ns: $ns})-[e:DECIDED_IN]->(i) "
        f"  WHERE {READABLE.format(n='d')} AND {EDGE_READABLE.format(n='e')} AND {LIVE.format(r='e')} "
        "RETURN i.id AS id, i.title AS title, i.source AS source, i.kind AS kind, i.url AS url, "
        "i.aliases AS aliases, toString(i.sourceUpdatedAt) AS updated, c.title AS container, "
        "coalesce(i.text, i.body, '') AS text, authors, collect(DISTINCT d.name) AS decisions",
        ids=ids, **_params(ns, v))


def _owners(store: Neo4jStore, ns: str, ids: list[str]) -> list[dict]:
    """Who authored or is assigned the locked items: the people a viewer would ask for access.
    Names and emails only, which the viewer can already see on the readable item that links."""
    rows = store._run(
        "MATCH (i {ns: $ns}) WHERE i.id IN $ids "
        "MATCH (i)-[r:AUTHORED|ASSIGNED_TO]-(acct:Account)<-[:HAS_ACCOUNT]-(p:Person) "
        "WHERE NOT coalesce(p.isBot, false) "
        "MATCH (p)-[:HAS_ACCOUNT]->(a:Account) "
        "RETURN p.id AS id, p.name AS person, collect(DISTINCT {name: a.name, source: a.source}) AS names, "
        "[e IN collect(DISTINCT a.email) WHERE e IS NOT NULL AND NOT e ENDS WITH 'noreply.github.com'] AS emails, "
        "count(DISTINCT i) AS n ORDER BY n DESC",
        ns=ns, ids=ids)
    return [{"name": display_name(r["person"], r["names"]), "email": (r["emails"] or [None])[0]} for r in rows]


def _source(row: dict, score: float) -> Source:
    people = []
    for a in row["authors"]:
        name = display_name(a["person"], a["names"]) if a["person"] else a["account"]
        if name and name not in people:
            people.append(name)
    aliases = row["aliases"] or []
    return Source(id=row["id"], title=row["title"], source=row["source"], kind=row["kind"], url=row["url"],
                  alias=aliases[0] if aliases else None, updated=(row["updated"] or "")[:10] or None,
                  container=row["container"], people=people, text=row["text"][:EXCERPT],
                  decisions=[d for d in row["decisions"] if d], score=round(score, 3))


def gather(store: Neo4jStore, ns: str, email: str, question: str, exclude: list[str] | None = None) -> Context:
    v = viewer(store, ns, email)
    exclude = exclude or []
    scores = _hits(store, ns, v, search_terms(question), exclude)
    top = sorted(scores, key=scores.get, reverse=True)[:10]
    linked, locked_ids = _hop(store, ns, v, top, exclude)
    for item, via in linked.items():
        scores[item] = max(scores.get(item, 0.0), scores[via] * HOP_DECAY)
    keep = sorted(scores, key=scores.get, reverse=True)[:MAX_SOURCES]
    rows = {r["id"]: r for r in _items(store, ns, v, keep)}
    sources = [_source(rows[i], scores[i]) for i in keep if i in rows and not asks(rows[i]["text"], question)]

    locked = Locked()
    if locked_ids:
        for r in store._run("MATCH (i {ns: $ns}) WHERE i.id IN $ids RETURN i.source AS s, count(*) AS n",
                            ns=ns, ids=locked_ids):
            locked.by_source[r["s"]] = r["n"]
        locked.count = len(locked_ids)
        locked.owners = _owners(store, ns, locked_ids)
    return Context(viewer=v, sources=sources, locked=locked)


def directory(store: Neo4jStore, ns: str) -> dict[str, str]:
    """Display name -> email for every person, so the sidebar can draft a note to whoever an
    answer says to ask."""
    out = {}
    for r in store._run(
            "MATCH (p:Person {ns: $ns})-[:HAS_ACCOUNT]->(a:Account) WHERE NOT coalesce(p.isBot, false) "
            "RETURN p.name AS person, collect(DISTINCT {name: a.name, source: a.source}) AS names, "
            "[e IN collect(DISTINCT a.email) WHERE e IS NOT NULL AND NOT e ENDS WITH 'noreply.github.com'] AS emails",
            ns=ns):
        name = display_name(r["person"], r["names"])
        if name and r["emails"]:
            out[name] = r["emails"][0]
    return out


def slack_people(store: Neo4jStore, ns: str) -> dict[str, dict]:
    """Display name -> where to message them on Slack: user id, workspace id and the workspace's
    web address, for the sidebar's "Message on Slack". Nobody is messaged from here; the sidebar
    opens a DM and the writer sends it."""
    teams = store._run(
        "MATCH (c:Container {ns: $ns, source: 'slack'}) WHERE c.id STARTS WITH 'slack:' "
        "RETURN split(c.id, ':')[1] AS team, c.url AS url LIMIT 1", ns=ns)
    if not teams:
        return {}
    team = teams[0]["team"]
    url = teams[0]["url"] or ""
    web = url.split("/archives/")[0] if "/archives/" in url else None
    out = {}
    for r in store._run(
            "MATCH (p:Person {ns: $ns})-[:HAS_ACCOUNT]->(s:Account {source: 'slack'}) "
            "WHERE NOT coalesce(p.isBot, false) "
            "MATCH (p)-[:HAS_ACCOUNT]->(a:Account) "
            "RETURN p.name AS person, s.handle AS user, collect(DISTINCT {name: a.name, source: a.source}) AS names",
            ns=ns):
        name = display_name(r["person"], r["names"])
        if name and r["user"]:
            out[name] = {"user": r["user"], "team": team, "web": web}
    return out
