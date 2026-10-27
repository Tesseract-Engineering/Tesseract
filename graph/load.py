"""
load.py - hand a build's proposals to the door, for one namespace.

Nothing here executes a query of its own except through the store: build_swarm.py
makes NodeProposals and EdgeProposals, the Ingestor decides, the store writes.

Teardown is by namespace only. Deleting by label (every :Person in the database)
is how one pipeline once destroyed another's people, so no code here does it.
"""

from __future__ import annotations

from .contract import EdgeProposal, NodeProposal
from .ingest import Ingestor
from .store import Neo4jStore


class Loader:
    """Owns the store and the ingestor for one namespace.

    Escalated edges (confidence under ingest.APPROVE_AT) are admitted and stored
    with decision="escalate", so readers can apply their own floor; see
    swarm_queries.py."""

    def __init__(self, ns: str, *, admit_escalated: bool = True) -> None:
        self.ns = ns
        self.store = Neo4jStore()
        self.ingestor = Ingestor(self.store, admit_escalated=admit_escalated)

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "Loader":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def wipe(self) -> None:
        """Delete this namespace and nothing else."""
        self.store.wipe_namespace(self.ns)

    def ingest(self, proposals):
        # Schema for exactly the types in this batch - see ensure_schema().
        proposals = list(proposals)
        node_types = {p.type for p in proposals if isinstance(p, NodeProposal)}
        edge_types = {p.type for p in proposals if isinstance(p, EdgeProposal)}
        node_types |= {t for p in proposals if isinstance(p, EdgeProposal)
                       for t in (p.src_type, p.dst_type)}
        self.store.ensure_schema(node_types, edge_types)
        return self.ingestor.ingest(proposals, ns=self.ns)
