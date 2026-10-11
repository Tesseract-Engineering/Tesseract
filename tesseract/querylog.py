"""Query log: which questions were asked, what Cypher ran, which nodes came back.

record / top_nodes / ids_from_rows are stdlib only (no Neo4j). sync_to_graph
writes the log back into the graph as usage structure, and co_asked reads it:

    (:Question {ns, id: "q:<sha1>", text, askCount, lastAsked,
                source: "querylog", producer: "usage"})
      -[:LED_TO {count, lastAt, viewer, viewers, source: "querylog", producer: "usage"}]->
    (any node matched by {ns, id})          plus n.askCount, n.lastAskedAt

Everything here is inferred from usage, never parsed from a source document:
the source/producer stamps say so. Both take a neo4j driver as an argument, so
this module still imports without the neo4j package.
"""
import hashlib
import json
import random
import re
import sqlite3
import time

from .config import settings

# None means <TESSERACT_STATE>/querylog.sqlite; tests point it elsewhere.
DB_PATH = None

_SCHEMA = """CREATE TABLE IF NOT EXISTS queries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    question TEXT,
    cypher TEXT,
    node_ids TEXT NOT NULL DEFAULT '[]',
    latency_ms REAL)"""
# Columns added after the first release; migrated in place on connect.
# synced: 0 = not yet in the graph, 1 = synced, negative = claimed by a sync in flight.
_EXTRA = {"synced": "INTEGER NOT NULL DEFAULT 0", "viewer": "TEXT", "origin": "TEXT"}
NS = settings().namespace


def _db_path():
    if DB_PATH:
        return DB_PATH
    path = settings().state_dir / "querylog.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    return str(path)


def _connect(path=None):
    conn = sqlite3.connect(path or _db_path(), timeout=10)
    conn.execute(_SCHEMA)
    have = {r[1] for r in conn.execute("PRAGMA table_info(queries)")}
    for col, decl in _EXTRA.items():
        if col not in have:
            try:
                conn.execute(f"ALTER TABLE queries ADD COLUMN {col} {decl}")
            except sqlite3.OperationalError:
                pass    # another process added it first
    return conn


def record(question, cypher, node_ids, latency_ms, path=None, viewer=None, origin=None):
    """Append one query. Returns the new row id. `viewer` is the viewer preset
    name, `origin` where it came from ("api", "seed"); both optional."""
    with _connect(path) as conn:
        cur = conn.execute(
            "INSERT INTO queries (ts, question, cypher, node_ids, latency_ms, viewer, origin) "
            "VALUES (?,?,?,?,?,?,?)",
            (time.strftime("%Y-%m-%dT%H:%M:%S"), question, cypher,
             json.dumps(list(node_ids or []), default=str), latency_ms, viewer, origin))
        return cur.lastrowid


def count(origin=None, path=None):
    """How many queries are logged (optionally only from `origin`)."""
    with _connect(path) as conn:
        if origin is None:
            return conn.execute("SELECT count(*) FROM queries").fetchone()[0]
        return conn.execute("SELECT count(*) FROM queries WHERE origin = ?", (origin,)).fetchone()[0]


def top_nodes(limit=10, path=None):
    """Most-returned node ids as [(node_id, count)], highest first."""
    counts = {}
    with _connect(path) as conn:
        for (raw,) in conn.execute("SELECT node_ids FROM queries"):
            for nid in json.loads(raw):
                counts[nid] = counts.get(nid, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], str(kv[0])))[:limit]


def ids_from_rows(rows):
    """Pull node ids out of result rows: any value under a key named id or ending in _id/Id."""
    found = []
    for row in rows or []:
        if isinstance(row, dict):
            for key, val in row.items():
                if (key == "id" or key.endswith("_id") or key.endswith("Id")) and isinstance(val, (str, int)):
                    found.append(val)
    return found


# --------------------------------------------------------------------------- #
# usage graph: the log written back as (:Question)-[:LED_TO]->(node)
# --------------------------------------------------------------------------- #

def normalise(text):
    """Lowercase, single spaces, no trailing punctuation: 'Who owns X?' == 'who owns x'."""
    return re.sub(r"\s+", " ", (text or "").strip().lower()).rstrip(" ?!.")


def question_id(text):
    return "q:" + hashlib.sha1(normalise(text).encode("utf-8")).hexdigest()


Q_SYNC_QUESTIONS = """
UNWIND $qs AS q
MERGE (x:Question {ns: $ns, id: q.id})
  ON CREATE SET x.text = q.text, x.askCount = 0, x.source = 'querylog', x.producer = 'usage',
                x.firstAsked = q.first
SET x.askCount = x.askCount + q.n, x.lastAsked = q.last
""".strip()

# n is any node with this {ns, id}: the hits are Issues, Documents, Services,
# so no single label. A Question never LED_TO another Question.
Q_SYNC_EDGES = """
UNWIND $edges AS e
MATCH (x:Question {ns: $ns, id: e.qid})
MATCH (n {ns: $ns, id: e.nid}) WHERE NOT n:Question
MERGE (x)-[r:LED_TO]->(n)
  ON CREATE SET r.count = 0, r.source = 'querylog', r.producer = 'usage', r.viewers = []
SET r.count = r.count + e.n, r.lastAt = e.last, r.viewer = e.viewer,
    r.viewers = [v IN r.viewers WHERE NOT v IN e.viewers] + e.viewers,
    n.askCount = coalesce(n.askCount, 0) + e.n, n.lastAskedAt = e.last
RETURN count(r) AS edges
""".strip()


def _aggregate(rows):
    """Rows (id, ts, question, node_ids, viewer) -> (questions, edges) batches.
    A node listed twice in one row counts once for that ask."""
    qs, edges = {}, {}
    for _id, ts, question, raw, viewer in rows:
        if not normalise(question):
            continue
        qid = question_id(question)
        q = qs.setdefault(qid, {"id": qid, "text": (question or "").strip(), "n": 0, "first": ts, "last": ts})
        q["n"] += 1
        q["first"], q["last"] = min(q["first"], ts), max(q["last"], ts)
        try:
            ids = json.loads(raw or "[]")
        except ValueError:
            ids = []
        for nid in dict.fromkeys(str(i) for i in ids if i not in (None, "")):
            e = edges.setdefault((qid, nid), {"qid": qid, "nid": nid, "n": 0, "last": ts,
                                              "viewer": viewer, "viewers": []})
            e["n"] += 1
            if ts >= e["last"]:
                e["last"], e["viewer"] = ts, viewer
            if viewer and viewer not in e["viewers"]:
                e["viewers"].append(viewer)
    return list(qs.values()), list(edges.values())


def _read(driver, cypher, database=None, **params):
    """Read query with notifications off (askCount / LED_TO are unknown keys
    until the first sync, and the driver would warn on every read)."""
    with driver.session(database=database, notifications_min_severity="OFF") as s:
        return s.run(cypher, **params).data()


def ensure_usage_schema(driver, database=None):
    driver.execute_query("CREATE INDEX question_ns_id IF NOT EXISTS FOR (q:Question) ON (q.ns, q.id)",
                         database_=database)


def sync_to_graph(driver, ns=NS, path=None, database=None):
    """Write every not-yet-synced logged query into the graph (one transaction)
    and mark those rows synced. Rows are claimed first, so two processes
    syncing at once never count the same ask twice. Returns
    {"rows", "questions", "edges"}; a failed write releases the claim."""
    token = -random.randint(2, 2**31)
    with _connect(path) as conn:
        conn.execute("UPDATE queries SET synced = ? WHERE synced = 0", (token,))
        rows = conn.execute("SELECT id, ts, question, node_ids, viewer FROM queries WHERE synced = ? "
                            "ORDER BY id", (token,)).fetchall()
    if not rows:
        return {"rows": 0, "questions": 0, "edges": 0}
    qs, edges = _aggregate(rows)
    try:
        ensure_usage_schema(driver, database)

        def work(tx):
            tx.run(Q_SYNC_QUESTIONS, qs=qs, ns=ns).consume()
            rec = tx.run(Q_SYNC_EDGES, edges=edges, ns=ns).single() if edges else None
            return rec["edges"] if rec else 0

        with driver.session(database=database, notifications_min_severity="OFF") as s:
            written = s.execute_write(work)
    except Exception:
        with _connect(path) as conn:
            conn.execute("UPDATE queries SET synced = 0 WHERE synced = ?", (token,))
        raise
    with _connect(path) as conn:
        conn.execute("UPDATE queries SET synced = 1 WHERE synced = ?", (token,))
    return {"rows": len(rows), "questions": len(qs), "edges": written}


Q_CO_ASKED = """
MATCH (n {ns: $ns, id: $id})<-[a:LED_TO]-(q:Question {ns: $ns})-[b:LED_TO]->(m)
WHERE m <> n
WITH m, count(DISTINCT q) AS shared, sum(CASE WHEN a.count < b.count THEN a.count ELSE b.count END) AS weight,
     collect(DISTINCT q.text)[..3] AS questions
RETURN m.id AS node_id, labels(m)[0] AS label, coalesce(m.title, m.name, m.id) AS title,
       m.visibility AS visibility, coalesce(m.aclGroups, []) AS acl,
       shared, weight, coalesce(m.askCount, 0) AS askCount, questions
ORDER BY shared DESC, weight DESC, node_id
LIMIT $limit
""".strip()


def co_asked(driver, node_id, limit=10, ns=NS, database=None):
    """Nodes that the same questions led to as `node_id`, most shared questions
    first: edges inferred from access patterns, read on demand (not stored)."""
    return _read(driver, Q_CO_ASKED, database, ns=ns, id=node_id, limit=int(limit))


Q_TOP_ASKED = """
MATCH (n {ns: $ns}) WHERE n.askCount > 0 AND NOT n:Question
RETURN n.id AS node_id, labels(n)[0] AS label, coalesce(n.title, n.name, n.id) AS title,
       n.visibility AS visibility, coalesce(n.aclGroups, []) AS acl,
       n.askCount AS askCount, toString(n.lastAskedAt) AS lastAskedAt
ORDER BY askCount DESC, node_id
LIMIT $limit
""".strip()


def top_asked(driver, limit=10, ns=NS, database=None):
    """Most-asked nodes in the graph (by askCount written by sync_to_graph)."""
    return _read(driver, Q_TOP_ASKED, database, ns=ns, limit=int(limit))


def reset_graph(driver, ns=NS, path=None, database=None):
    """Remove all usage structure from the graph and mark every log row unsynced,
    so the next sync_to_graph rebuilds the graph from the log alone."""
    with driver.session(database=database, notifications_min_severity="OFF") as s:
        s.run("MATCH (q:Question {ns: $ns}) DETACH DELETE q", ns=ns).consume()
        s.run("MATCH (n {ns: $ns}) WHERE n.askCount IS NOT NULL REMOVE n.askCount, n.lastAskedAt",
              ns=ns).consume()
    with _connect(path) as conn:
        conn.execute("UPDATE queries SET synced = 0")
