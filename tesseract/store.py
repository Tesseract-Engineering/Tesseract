"""
store.py - the only module that writes to the graph.

The builder works in plain proposal rows; this class turns admitted rows into
batched, idempotent Cypher. Reads for answering go through graph.py.

Design choices that matter at scale:

  (ns, id) uniqueness  namespacing is enforced by the database, not by
                       convention, so one producer's teardown cannot delete
                       another's nodes.
  supersede not delete  a replaced fact is marked, never
                       removed, so the audit trail survives re-ingestion.
  UNWIND batching      one round trip per N rows, not per row.
"""

from __future__ import annotations

from typing import Any

from neo4j import GraphDatabase

from .config import settings
from .contract import EDGE_TYPES, EXTERNAL_TYPES, NODE_TYPES


class Neo4jStore:
    def __init__(self, uri: str | None = None, auth: tuple[str, str] | None = None,
                 database: str | None = None, batch_size: int = 1000):
        s = settings()
        self.driver = GraphDatabase.driver(uri or s.neo4j_uri, auth=auth or (s.neo4j_user, s.neo4j_password))
        database = database or s.neo4j_database
        self.database = database
        self.batch_size = batch_size

    def close(self) -> None:
        self.driver.close()

    def __enter__(self) -> "Neo4jStore":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def _run(self, query: str, **params: Any):
        return self.driver.execute_query(query, database_=self.database, **params).records

    def _batched(self, query: str, rows: list[dict], **params: Any) -> int:
        """Returns rows ACTUALLY written, not rows attempted.

        These differ, and the gap is where data silently disappears: an edge
        whose endpoint node does not exist fails its MATCH and produces no row,
        so the write is a no-op. Counting attempts instead of effects reports
        success for data that never landed. Every write query therefore ends in
        `RETURN count(...) AS n`, and the caller compares.
        """
        written = 0
        for i in range(0, len(rows), self.batch_size):
            records = self._run(query, rows=rows[i:i + self.batch_size], **params)
            written += records[0]["n"] if records else 0
        return written

    # -- schema ------------------------------------------------------------- #

    def ensure_schema(self, node_types=None, edge_types=None) -> None:
        """Composite (ns, id) uniqueness per node type, plus the indexes the
        hot paths actually use. Cheap to re-run.

        Only for the types being WRITTEN, when they are given: an index on a
        label nothing writes is still maintained on every write. Pass None
        for every type the contract knows."""
        wanted_nodes = NODE_TYPES if node_types is None else [t for t in NODE_TYPES if t in node_types]
        wanted_edges = EDGE_TYPES if edge_types is None else [t for t in EDGE_TYPES if t in edge_types]
        wanted_external = (EXTERNAL_TYPES if node_types is None
                           else [t for t in EXTERNAL_TYPES if t in node_types])
        # A bare `id IS UNIQUE` constraint from any earlier schema makes
        # (ns, id) unreachable: the first namespace to claim an id locks every
        # other namespace out of it, which is the opposite of what namespacing
        # is for.
        #
        # Found by asking the database, not by guessing names: a constraint
        # left by any earlier schema, whatever it was called, is dropped.
        for name in self._legacy_id_constraints():
            self._run(f"DROP CONSTRAINT {name} IF EXISTS")

        for node_type in wanted_nodes:
            self._run(f"CREATE CONSTRAINT {node_type.lower()}_ns_id IF NOT EXISTS "
                      f"FOR (n:{node_type}) REQUIRE (n.ns, n.id) IS UNIQUE")
            self._run(f"CREATE INDEX {node_type.lower()}_hash IF NOT EXISTS "
                      f"FOR (n:{node_type}) ON (n.hash)")
            self._run(f"CREATE INDEX {node_type.lower()}_vis IF NOT EXISTS "
                      f"FOR (n:{node_type}) ON (n.visibility)")
        for edge_type in wanted_edges:
            self._run(f"CREATE INDEX {edge_type.lower()}_live IF NOT EXISTS "
                      f"FOR ()-[r:{edge_type}]-() ON (r.supersededAt)")
        # External types keep whatever indexes their owner made. Creating an
        # id index here is still worth it: every edge to one of them matches
        # on id, and without it that MATCH is a label scan.
        for node_type in wanted_external:
            self._run(f"CREATE INDEX {node_type.lower()}_id IF NOT EXISTS "
                      f"FOR (n:{node_type}) ON (n.id)")
        # Live-provider lookups (forge) find a node by its SourceRef URI.
        for node_type in ("Commit", "Issue", "Document"):
            if node_type in wanted_nodes:
                self._run(f"CREATE INDEX {node_type.lower()}_ref IF NOT EXISTS "
                          f"FOR (n:{node_type}) ON (n.ref)")
        # Keyword search over prose. Without it, "which documents mention
        # kafka" is a CONTAINS scan of every text property in the graph.
        text_labels = [t for t in ("Commit", "Issue", "Document", "Claim") if t in wanted_nodes]
        if text_labels:
            existing = {r["name"] for r in self._run("SHOW INDEXES YIELD name RETURN name")}
            if "knowledgeSearch" not in existing:
                self._run("CREATE FULLTEXT INDEX knowledgeSearch IF NOT EXISTS "
                          "FOR (n:Commit|Issue|Document|Claim) ON EACH [n.title, n.body, n.text]")

    def _legacy_id_constraints(self) -> list[str]:
        """Single-property `id` uniqueness constraints on labels this door
        owns. Constraints on labels it does not own are left alone - they
        belong to whoever made them."""
        rows = self._run("SHOW CONSTRAINTS YIELD name, labelsOrTypes, properties, type "
                         "RETURN name, labelsOrTypes, properties, type")
        out = []
        for row in rows:
            labels = row.get("labelsOrTypes") or []
            props = row.get("properties") or []
            if (len(labels) == 1 and labels[0] in NODE_TYPES
                    and props == ["id"] and "UNIQUE" in (row.get("type") or "")):
                out.append(row["name"])
        return out

    # -- writes ------------------------------------------------------------- #

    @staticmethod
    def _endpoint(node_type: str, id_field: str) -> str:
        """The MATCH pattern for one end of an edge.

        Nodes this door owns are found by (ns, id), which is the guarantee
        that keeps two producers' "Person" apart. Nodes another producer owns
        have no ns to match on, so they are found by id alone - weaker, and
        deliberately visible here rather than hidden behind a nullable
        property that silently matches across namespaces.
        """
        if node_type in EXTERNAL_TYPES:
            return f"id: row.{id_field}"
        return f"ns: row.ns, id: row.{id_field}"

    def upsert_nodes(self, node_type: str, rows: list[dict]) -> int:
        if node_type in EXTERNAL_TYPES:
            raise ValueError(f"{node_type} is owned by build_graph.py - "
                             f"this store links to it, it does not create it")
        if node_type not in NODE_TYPES:
            raise ValueError(f"{node_type} not in ontology")
        return self._batched(f"""
            UNWIND $rows AS row
            MERGE (n:{node_type} {{ns: row.ns, id: row.id}})
            SET n += row.props,
                n.hash = row.hash, n.visibility = row.visibility,
                n.aclGroups = row.aclGroups,
                n.source = row.prov_source, n.producer = row.prov_producer,
                n.observedAt = row.prov_observed_at, n.updatedAt = row.prov_ingested_at
            RETURN count(n) AS n
        """, rows)

    def upsert_edges(self, edge_type: str, src_type: str, dst_type: str,
                     rows: list[dict]) -> int:
        if edge_type not in EDGE_TYPES:
            raise ValueError(f"{edge_type} not in ontology")
        # Endpoint labels are required, not optional: the (ns, id) constraint is
        # per-label, so an unlabelled MATCH cannot use it and degrades to a scan
        # of every node in the database once the graph is non-trivial.
        # Keyed on hash as well as endpoints: a changed fact becomes a NEW
        # relationship rather than silently mutating the old one, which is what
        # makes supersede meaningful.
        return self._batched(f"""
            UNWIND $rows AS row
            MATCH (a:{src_type} {{{self._endpoint(src_type, "srcId")}}})
            MATCH (b:{dst_type} {{{self._endpoint(dst_type, "dstId")}}})
            MERGE (a)-[r:{edge_type} {{hash: row.hash}}]->(b)
            SET r += row.props,
                r.source = row.prov_source, r.sourceId = row.prov_source_id,
                r.producer = row.prov_producer, r.confidence = row.prov_confidence,
                r.quote = row.prov_quote, r.observedAt = row.prov_observed_at,
                r.ingestedAt = row.prov_ingested_at, r.decision = row.decision
            RETURN count(r) AS n
        """, rows)

    def supersede_edges(self, edge_type: str, src_type: str, dst_type: str,
                        rows: list[dict], at: str) -> int:
        """Mark the previous version of a fact superseded. Never deleted - the
        old edge stays queryable, tagged with when and by what replaced it."""
        return self._batched(f"""
            UNWIND $rows AS row
            MATCH (a:{src_type} {{{self._endpoint(src_type, "srcId")}}})-[r:{edge_type}]->(b:{dst_type} {{{self._endpoint(dst_type, "dstId")}}})
            WHERE r.hash <> row.hash AND r.supersededAt IS NULL
            SET r.supersededAt = datetime($at), r.supersededBy = row.hash
            RETURN count(r) AS n
        """, rows, at=at)

    # -- reads / admin ------------------------------------------------------- #

    def existing_hashes(self, ns: str) -> set[str]:
        """Every hash already in this namespace, for cheap idempotency: a
        proposal whose hash is present needs no write at all."""
        nodes = self._run("MATCH (n {ns: $ns}) WHERE n.hash IS NOT NULL RETURN n.hash AS h", ns=ns)
        edges = self._run("MATCH ({ns: $ns})-[r]->() WHERE r.hash IS NOT NULL RETURN r.hash AS h", ns=ns)
        return {r["h"] for r in nodes} | {r["h"] for r in edges}

    def counts(self, ns: str) -> dict[str, int]:
        nodes = self._run(
            "MATCH (n {ns: $ns}) RETURN labels(n)[0] AS k, count(*) AS n", ns=ns)
        edges = self._run(
            "MATCH ({ns: $ns})-[r]->() RETURN type(r) AS k, count(*) AS n", ns=ns)
        live = self._run(
            "MATCH ({ns: $ns})-[r]->() WHERE r.supersededAt IS NOT NULL RETURN count(r) AS n", ns=ns)
        out = {f"node:{r['k']}": r["n"] for r in nodes}
        out.update({f"edge:{r['k']}": r["n"] for r in edges})
        out["superseded"] = live[0]["n"] if live else 0
        return out

    def live_counts(self, ns: str) -> dict[str, int]:
        """Counts excluding superseded edges - what a reader would see."""
        out = self.counts(ns)
        return {k: v for k, v in out.items() if k != "superseded"}

    def wipe_namespace(self, ns: str) -> None:
        """Teardown scoped to ONE namespace. The bug this prevents: a pipeline
        that deletes by label wipes another producer's nodes that happen to
        share it - which is exactly how the tutorial graph's people vanished."""
        with self.driver.session(database=self.database) as session:
            session.run("""
                MATCH (n {ns: $ns})
                CALL (n) { DETACH DELETE n } IN TRANSACTIONS OF 10000 ROWS
            """, ns=ns).consume()
