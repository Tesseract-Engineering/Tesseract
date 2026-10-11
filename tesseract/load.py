"""
load.py - turn connector output into PROPOSALS, and hand them to the door.

There is one writer: contract.py -> ingest.py -> store.py. This file only
builds proposals, so there is one place that decides what a fact looks like,
one teardown strategy, and content hashing on everything.

Teardown is namespaced: a rebuild deletes only its own namespace, never
another company's nodes that happen to share a label. That only works if every
write is namespaced too, which is this file's job.

Nothing here executes a query. It builds NodeProposal and EdgeProposal, and
the door decides.

Escalated edges (low-confidence inferences flagged for review) are admitted
and tagged (ingest.admit_escalated=True); the receipt prints how many edges a
stricter setting would have refused.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from .contract import EdgeProposal, NodeProposal, Provenance
from .ingest import Ingestor
from .store import Neo4jStore

# Labels this pipeline owns. Used only for the one-time sweep of pre-door
# nodes that carry no namespace; steady-state teardown is by namespace.
OWNED_LABELS = ["Person", "Account", "Commit", "Issue", "Document", "Claim", "ReviewItem"]

KIND_TYPE = {"commit": "Commit", "issue": "Issue", "document": "Document"}


def _when(value: str | None) -> datetime | None:
    """ISO string -> datetime, so the driver stores a real temporal type.

    Passing the string through would leave Commit.authoredAt as text, and
    every query that orders or filters by date would go on working while
    quietly comparing strings.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _clean(props: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in props.items() if v is not None}


def _prov(record, producer: str, confidence: float = 1.0, quote: str = "") -> Provenance:
    return Provenance(source=record.source, source_id=record.id, producer=producer,
                      confidence=confidence, quote=quote,
                      observed_at=record.created_at or None)


# --------------------------------------------------------------------------- #
# people
# --------------------------------------------------------------------------- #

# Facts about a person that come from a roster rather than from resolution.
# resolve.py can only say "these accounts are one human"; a directory says
# what that human does. Optional, so the Django pipeline - which has no
# directory - passes none of them and stores none of them.
PERSON_EXTRAS = ("title", "department", "status", "empId", "location", "departedAt",
                 "profileSource")

# Ticket fields beyond the common ones: property name -> connector meta key.
ISSUE_EXTRAS = {"key": "key", "type": "ticketType", "priority": "priority",
                "component": "component", "storyPoints": "storyPoints",
                "resolution": "resolution", "folder": "folder",
                "customerTier": "customerTier", "csat": "csat", "merchantId": "merchantId",
                "branch": "branch", "accountExecutive": "accountExecutive",
                "deviceModel": "deviceModel", "serialNumber": "serialNumber",
                "defectCategory": "defectCategory"}


def people(rows: Iterable[dict], ns: str) -> list[NodeProposal]:
    out = []
    for row in rows:
        props = {"name": row["name"], "accountCount": row["accountCount"],
                 "sources": row["sources"], "isBot": row["isBot"]}
        props.update({k: row[k] for k in PERSON_EXTRAS if row.get(k) is not None})
        out.append(NodeProposal(
            ns=ns, type="Person", id=row["id"], props=_clean(props),
            provenance=Provenance(source="derived", source_id=row["id"],
                                  producer="resolve:ladder", confidence=1.0),
        ))
    return out


def accounts(links: Iterable[dict], ns: str) -> list[NodeProposal | EdgeProposal]:
    """One Account node per identity, plus the HAS_ACCOUNT edge to its Person.

    The edge carries the resolution's confidence, not 1.0. Identity resolution
    is inference - two accounts are the same human because a rule said so -
    and an edge that claims certainty it does not have is the thing that makes
    a wrong merge unauditable later.
    """
    out: list[NodeProposal | EdgeProposal] = []
    for link in links:
        account = link["account"]
        out.append(NodeProposal(
            ns=ns, type="Account", id=account["id"],
            props=_clean({"source": account["source"], "handle": account["handle"],
                          "name": account.get("name"), "email": account.get("email")}),
            provenance=Provenance(source=account["source"], source_id=account["id"],
                                  producer="connector", confidence=1.0),
        ))
        out.append(EdgeProposal(
            ns=ns, type="HAS_ACCOUNT", src_id=link["personId"], dst_id=account["id"],
            src_type="Person", dst_type="Account", props={},
            provenance=Provenance(source="derived", source_id=link["personId"],
                                  producer="resolve:ladder",
                                  confidence=float(link.get("confidence", 1.0))),
        ))
    return out


def review_items(rows: Iterable[dict], ns: str) -> list[NodeProposal]:
    return [NodeProposal(
        ns=ns, type="ReviewItem", id=row["id"],
        props=_clean({"kind": row["kind"], "a": row["a"], "b": row["b"],
                      "aName": row["aName"], "bName": row["bName"],
                      "score": row["score"], "status": row["status"],
                      "reason": row["reason"]}),
        provenance=Provenance(source="derived", source_id=row["id"],
                              producer="resolve:ladder", confidence=1.0),
    ) for row in rows]


# --------------------------------------------------------------------------- #
# artifacts
# --------------------------------------------------------------------------- #

def artifacts(records: Iterable, ns: str) -> list[NodeProposal]:
    """Commit / Issue / Document nodes, straight from the connector records."""
    out = []
    for record in records:
        node_type = KIND_TYPE[record.kind]
        meta = record.meta or {}
        props: dict[str, Any] = {"title": record.title}
        if node_type == "Commit":
            props.update(sha=meta.get("sha"), shortSha=meta.get("shortSha"),
                         body=record.text, authoredAt=_when(record.created_at),
                         filesChanged=len(record.refs.get("files", [])))
        elif node_type == "Issue":
            props.update(number=meta.get("number"), body=record.text, url=record.url,
                         state=meta.get("state"), isPullRequest=meta.get("isPullRequest"),
                         labels=meta.get("labels"), createdAt=_when(record.created_at),
                         source=record.source)
            # Whatever else the source declared about the ticket. Each is
            # optional - four Jira formats and three Zendesk ones disagree on
            # which fields exist - so a missing one is just not written.
            props.update({prop: meta.get(key) for prop, key in ISSUE_EXTRAS.items()})
            if meta.get("closedAt"):
                props["closedAt"] = _when(meta["closedAt"])
        else:
            props.update(path=meta.get("path"), text=record.text,
                         bytes=meta.get("bytes"), format=meta.get("format"),
                         createdAt=_when(record.created_at))
        out.append(NodeProposal(
            ns=ns, type=node_type, id=record.id, props=_clean(props),
            visibility=record.visibility, acl_groups=record.acl_groups,
            provenance=_prov(record, f"connector:{record.source}"),
        ))
    return out


def authored(records: Iterable, account_of: dict[str, str], ns: str) -> list[EdgeProposal]:
    return [EdgeProposal(
        ns=ns, type="AUTHORED", src_id=account_of[r.id], dst_id=r.id,
        src_type="Account", dst_type=KIND_TYPE[r.kind], props={},
        provenance=_prov(r, f"connector:{r.source}"),
    ) for r in records if r.id in account_of]


def touched(commits: Iterable, ns: str) -> list[EdgeProposal]:
    """Commit -> Module, from git's own file list.

    A parsed fact: git says which paths the commit changed. It carries
    confidence 1.0 and no quote, which is what distinguishes it from anything
    an extractor inferred out of prose.
    """
    return [EdgeProposal(
        ns=ns, type="TOUCHED", src_id=record.id, dst_id=module,
        src_type="Commit", dst_type="Module", props={},
        provenance=_prov(record, "parser:git-name-only"),
    ) for record in commits for module in record.refs.get("modules", [])]


def mentions(hits: Iterable[dict], ns: str) -> list[EdgeProposal]:
    """Extracted references. The evidence travels with the edge."""
    return [EdgeProposal(
        ns=ns, type=hit["rel"], src_id=hit["srcId"], dst_id=hit["dstId"],
        src_type=hit["srcLabel"], dst_type=hit["dstLabel"],
        # `surface` is the text that actually matched. Without it a mention
        # says a service was named but not HOW, and "Billing" and
        # "billing-service" are very different amounts of evidence.
        props=_clean({"count": hit.get("count", 1), "inTitle": hit.get("inTitle", False),
                      "surface": hit.get("surface")}),
        provenance=Provenance(source=hit["source"], source_id=hit["sourceId"],
                              producer=hit["extractor"], confidence=hit["confidence"],
                              quote=hit.get("quote", ""),
                              ingested_at=hit.get("extractedAt")),
    ) for hit in hits]


def claims(rows: Iterable[dict], ns: str) -> list[NodeProposal | EdgeProposal]:
    out: list[NodeProposal | EdgeProposal] = []
    for row in rows:
        provenance = Provenance(source=row["source"], source_id=row["sourceId"],
                                producer=row["extractor"], confidence=row["confidence"],
                                quote=row["quote"], ingested_at=row["extractedAt"])
        out.append(NodeProposal(
            ns=ns, type="Claim", id=row["id"],
            props=_clean({"text": row["text"], "kind": row["kind"], "quote": row["quote"],
                          "confidence": row["confidence"]}),
            provenance=provenance,
        ))
        out.append(EdgeProposal(
            ns=ns, type="FROM", src_id=row["id"], dst_id=row["sourceId"],
            src_type="Claim", dst_type=row.get("sourceType", "Issue"), props={},
            provenance=provenance,
        ))
    return out


# --------------------------------------------------------------------------- #
# the door, held open
# --------------------------------------------------------------------------- #

class Loader:
    """Owns the store and the ingestor for one namespace."""

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

    def wipe(self) -> dict[str, int]:
        """Teardown scoped to this namespace, plus a one-time legacy sweep.

        The sweep exists because nodes written before the door have no `ns` at
        all, so a namespaced wipe cannot see them and MERGE on (ns, id) would
        happily create a second copy of every one of them. It deletes by
        label, which is the exact operation this refactor exists to abolish -
        so it reports what it removed, and it stops firing as soon as there is
        nothing left from before.
        """
        self.store.wipe_namespace(self.ns)
        legacy = {}
        for label in OWNED_LABELS:
            rows = self.store._run(
                f"MATCH (n:{label}) WHERE n.ns IS NULL "
                f"WITH n LIMIT 100000 DETACH DELETE n RETURN count(n) AS n")
            removed = rows[0]["n"] if rows else 0
            if removed:
                legacy[label] = removed
        return legacy

    def ingest(self, proposals):
        # Schema for exactly the types in this batch - see ensure_schema().
        proposals = list(proposals)
        node_types = {p.type for p in proposals if isinstance(p, NodeProposal)}
        edge_types = {p.type for p in proposals if isinstance(p, EdgeProposal)}
        node_types |= {t for p in proposals if isinstance(p, EdgeProposal)
                       for t in (p.src_type, p.dst_type)}
        self.store.ensure_schema(node_types, edge_types)
        return self.ingestor.ingest(proposals, ns=self.ns)
