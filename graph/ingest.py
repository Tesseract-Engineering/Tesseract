"""
ingest.py - the one door.

Every write to the graph passes through here: agents, connectors, backfills,
one-off scripts. Nothing else calls the store. That is the whole point - a
swarm of producers is safe only if there is exactly one place that decides what
becomes a fact.

    propose  ->  validate  ->  dedupe  ->  govern  ->  write  ->  receipt

  validate   mechanical checks from contract.py. Unknown type, missing
             provenance, bad endpoint types: rejected with a reason.
  dedupe     content hash. Seen before with the same meaning? No write at all.
  govern     an explicit decision on every edge, recorded on the edge.
  write      batched upserts; changed facts supersede rather than overwrite.
  receipt    counts and reasons, returned to the caller. A producer always
             learns what happened to its proposals.

The receipt matters as much as the write. Silent success on an empty batch is,
per Agentic-Graph's lesson 5.9, the deadliest failure mode: "exit code 0 means
nothing; check the artifact."
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from contract import (Decision, EdgeProposal, NodeProposal, content_hash, now_iso,
                      as_row, validate)
from store import Neo4jStore

# Thresholds for the default policy. Deliberately crude and inspectable - and
# note the open question: these confidences have never been calibrated. Until
# the frontier is shown to bend, treat this as bookkeeping, not a quality gate.
AUTO_APPROVE_AT = 0.95
APPROVE_AT = 0.80


@dataclass
class Receipt:
    accepted: int = 0
    unchanged: int = 0
    superseded: int = 0
    rejected: int = 0
    dropped: int = 0        # passed validation but did not land - always a bug
    decisions: Counter = field(default_factory=Counter)
    reasons: Counter = field(default_factory=Counter)
    by_type: Counter = field(default_factory=Counter)

    def __str__(self) -> str:
        parts = [f"accepted={self.accepted}", f"unchanged={self.unchanged}",
                 f"superseded={self.superseded}", f"rejected={self.rejected}"]
        if self.dropped:
            parts.append(f"DROPPED={self.dropped}")
        if self.decisions:
            parts.append("decisions=" + ",".join(f"{k}:{v}" for k, v in self.decisions.most_common()))
        return " ".join(parts)


def decide(proposal: EdgeProposal) -> Decision:
    """The governance function. One decision per edge, always recorded."""
    confidence = proposal.provenance.confidence if proposal.provenance else 0.0
    if confidence >= AUTO_APPROVE_AT:
        return "auto_approve"
    if confidence >= APPROVE_AT:
        return "approve"
    return "escalate"


class Ingestor:
    def __init__(self, store: Neo4jStore, *, admit_escalated: bool = False):
        self.store = store
        # Escalated facts are NOT admitted by default. The paper's gamma only
        # lets approve/revise/auto_approve into the graph; our earlier build
        # wrote escalations in anyway and merely tagged them, which quietly
        # made "escalate" mean nothing.
        self.admit_escalated = admit_escalated

    def ingest(self, proposals: Iterable[NodeProposal | EdgeProposal], *,
               ns: str, dry_run: bool = False) -> Receipt:
        receipt = Receipt()
        nodes: dict[str, list[dict]] = defaultdict(list)
        edges: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        escalations: list[dict] = []

        known = self.store.existing_hashes(ns) if not dry_run else set()

        for proposal in proposals:
            if proposal.ns != ns:
                receipt.rejected += 1
                receipt.reasons[f"namespace mismatch: {proposal.ns} != {ns}"] += 1
                continue

            reason = validate(proposal)
            if reason:
                receipt.rejected += 1
                receipt.reasons[reason] += 1
                continue

            digest = content_hash(proposal)
            if digest in known:
                receipt.unchanged += 1          # same meaning, already stored
                continue

            row = as_row(proposal)
            if isinstance(proposal, NodeProposal):
                nodes[proposal.type].append(row)
                receipt.accepted += 1
                receipt.by_type[f"node:{proposal.type}"] += 1
                continue

            decision = decide(proposal)
            receipt.decisions[decision] += 1
            if decision == "escalate" and not self.admit_escalated:
                escalations.append(row)
                receipt.rejected += 1
                receipt.reasons["escalated for review - not admitted"] += 1
                continue

            row["decision"] = decision
            # Grouped by endpoint labels too, so the write can use the
            # per-label (ns, id) constraint instead of scanning.
            edges[(proposal.type, proposal.src_type, proposal.dst_type)].append(row)
            receipt.accepted += 1
            receipt.by_type[f"edge:{proposal.type}"] += 1

        if dry_run:
            return receipt

        # Nodes first: an edge cannot attach to an endpoint that is not there.
        for node_type, rows in nodes.items():
            written = self.store.upsert_nodes(node_type, rows)
            self._account_for_drops(receipt, len(rows), written, f"node:{node_type}")

        stamp = now_iso()
        for (edge_type, src_type, dst_type), rows in edges.items():
            receipt.superseded += self.store.supersede_edges(
                edge_type, src_type, dst_type, rows, stamp)
            written = self.store.upsert_edges(edge_type, src_type, dst_type, rows)
            self._account_for_drops(
                receipt, len(rows), written,
                f"{edge_type}: endpoint missing ({src_type} or {dst_type})")

        return receipt

    @staticmethod
    def _account_for_drops(receipt: Receipt, attempted: int, written: int, what: str) -> None:
        """A write that lands nothing must never be reported as acceptance.

        Silent emptiness is the failure mode that hides longest: the run exits
        0, the receipt says accepted, and the fact is simply absent.
        """
        dropped = attempted - written
        if dropped > 0:
            receipt.accepted -= dropped
            receipt.dropped += dropped
            receipt.reasons[f"dropped, {what}"] += dropped
