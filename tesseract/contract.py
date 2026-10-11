"""
contract.py - the only shape anything may write into the graph.

Agents, connectors and scripts do not write. They PROPOSE, and the door in
ingest.py decides. Instruction-following is not a filter: a model told to
"omit pairs with no stated relation" will still link most of them. Admission
has to be structural - checked in code, deterministically, with
non-conforming proposals dropped and counted.

Three properties this file enforces:

  NAMESPACING   every node carries `ns`, and uniqueness is (ns, id).
                Two companies (or two producers) can both have a "Person"
                without one's rebuild deleting the other's.

  PROVENANCE    a fact with no source is not admissible. Required on every
                edge: where it came from, what produced it, and how sure.

  IDENTITY      content_hash() makes re-ingestion cheap and safe. Same content,
                same hash, no write. Changed content supersedes rather than
                overwrites, so history survives.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

SCHEMA_VERSION = "1.0"

# The ontology. Adding to it is a deliberate edit here, never a runtime decision
# by a producer - that is what stops 400 relationship types meaning one thing.
NODE_TYPES: dict[str, set[str]] = {
    "Person":     {"name"},
    "Account":    {"handle"},
    "Commit":     {"title"},
    "Issue":      {"title"},
    "Document":   {"title"},
    "Claim":      {"text"},
    "ReviewItem": {"kind"},
    # A company's unit of ownership is a SERVICE: a monorepo of services with
    # an org chart on top says who owns what, and says it explicitly.
    "Service":    {"name"},
    "Team":       {"name"},
    # A paying customer, named on support tickets. Separate from Person: a
    # customer is an account the company serves, not a human who works there.
    "Customer":   {"name"},
}

# Node types this door does NOT create. A commit TOUCHES a code module that
# git names by path; the module has an id but is not a company record, so an
# edge may point at it without the door pretending to have created it. The
# edge still gets every other guarantee - validation, provenance, content
# hash, supersede - but its endpoint is matched on id alone.
EXTERNAL_TYPES: set[str] = {"Module"}

# edge type -> (allowed source types, allowed target types)
_ARTIFACTS = {"Commit", "Issue", "Document"}
_ORG = {"Service", "Team"}

EDGE_TYPES: dict[str, tuple[set[str], set[str]]] = {
    "HAS_ACCOUNT": ({"Person"}, {"Account"}),
    "AUTHORED":    ({"Account", "Person"}, _ARTIFACTS),
    "TOUCHED":     ({"Commit"}, {"Module"}),
    "REFERENCES":  (_ARTIFACTS, {"Issue"}),
    "FROM":        ({"Claim"}, _ARTIFACTS),
    "MEMBER_OF":   ({"Person", "Service"}, {"Team"}),
    "OWNS":        ({"Person", "Team"}, {"Service"}),
    # What an extractor found named in the prose.
    "MENTIONS":    (_ARTIFACTS, _ORG | {"Module", "Issue", "Person", "Customer"}),
    # ABOUT is the DECLARED subject - a ticket's own `service` field - while
    # MENTIONS is what an extractor found in the prose. Keeping them apart is
    # the parsed-vs-inferred distinction, and it makes the declared field
    # usable as ground truth to score the extractor against.
    "ABOUT":       (_ARTIFACTS | {"Claim"}, _ORG | {"Module"}),
    # Who a ticket is assigned to, from the ticket's own field. Distinct from
    # AUTHORED (who filed it): "who raised this" and "who is on the hook for
    # it" are different questions with different answers.
    "ASSIGNED_TO": ({"Issue"}, {"Person"}),
    # A document's own header saying who stands behind it: "**Author**:",
    # "_Owner: ..._", "**Incident Commander**:", "Last updated ... by ...".
    # Carries `role`. Being named in a contact list and having written the
    # document are different facts, so this is not a MENTIONS.
    "CREDITS":     ({"Document"}, {"Person"}),
    # Took part in a ticket's discussion - a comment, an internal note, a
    # pull-request review - as opposed to filing it or owning it. Carries
    # `as` and a count.
    "COMMENTED":   ({"Account"}, {"Issue"}),
    # Which customer a support ticket was raised for. Declared by the ticket.
    "FOR_CUSTOMER": ({"Issue"}, {"Customer"}),
}


def is_external(node_type: str) -> bool:
    """True for a type another producer owns. Endpoints of this type are
    matched on id alone, because they have no namespace to match on yet."""
    return node_type in EXTERNAL_TYPES

VISIBILITIES = {"public", "internal", "restricted", "private"}
Decision = Literal["auto_approve", "approve", "revise", "escalate", "reject"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class Provenance:
    """Where a fact came from. Without this a proposal is inadmissible.

    observed_at is the real-world date the content is ABOUT, which is not the
    same as when we ingested it. This distinction is what makes recency-based
    superseding work at all: judging freshness by ingestion
    time tells you which crawler ran last, not which fact is newer.
    """
    source: str                      # 'git' | 'jira' | 'slack' | 'warehouse:orders' ...
    source_id: str                   # stable id within that source
    producer: str                    # who proposed it: 'rule:issue-ref', 'agent:extractor-3'
    confidence: float = 1.0
    quote: str = ""                  # verbatim supporting span, when the source is text
    observed_at: str | None = None   # real-world date of the content
    ingested_at: str = field(default_factory=now_iso)


@dataclass(slots=True)
class NodeProposal:
    ns: str
    type: str
    id: str
    props: dict[str, Any] = field(default_factory=dict)
    visibility: str = "internal"
    acl_groups: list[str] = field(default_factory=list)
    provenance: Provenance | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.ns, self.id)


@dataclass(slots=True)
class EdgeProposal:
    ns: str
    type: str
    src_id: str
    dst_id: str
    src_type: str
    dst_type: str
    props: dict[str, Any] = field(default_factory=dict)
    provenance: Provenance | None = None

    @property
    def key(self) -> tuple[str, str, str, str]:
        return (self.ns, self.type, self.src_id, self.dst_id)


def content_hash(proposal: NodeProposal | EdgeProposal) -> str:
    """Stable digest of the MEANING of a proposal.

    Provenance is excluded on purpose: the same fact seen twice in two places
    is one fact with two sources, not two facts. Ingestion timestamps are
    excluded for the same reason - including them would make every re-run look
    like a change and defeat idempotency entirely.
    """
    if isinstance(proposal, NodeProposal):
        payload = {"ns": proposal.ns, "type": proposal.type, "id": proposal.id,
                   "props": proposal.props, "visibility": proposal.visibility,
                   "acl": sorted(proposal.acl_groups)}
    else:
        payload = {"ns": proposal.ns, "type": proposal.type, "src": proposal.src_id,
                   "dst": proposal.dst_id, "props": proposal.props}
    blob = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def validate(proposal: NodeProposal | EdgeProposal) -> str | None:
    """Return a rejection reason, or None if admissible.

    Every check is mechanical. Nothing here asks whether a fact is *true* -
    only whether it is well-formed enough to be admitted and later audited.
    """
    if not proposal.ns or not proposal.ns.strip():
        return "missing namespace"

    if isinstance(proposal, NodeProposal):
        if proposal.type in EXTERNAL_TYPES:
            return f"{proposal.type} is external - this door may link to it, not create it"
        if proposal.type not in NODE_TYPES:
            return f"unknown node type '{proposal.type}' (not in ontology)"
        if not proposal.id:
            return "missing id"
        missing = NODE_TYPES[proposal.type] - set(proposal.props)
        if missing:
            return f"{proposal.type} requires {sorted(missing)}"
        if proposal.visibility not in VISIBILITIES:
            return f"unknown visibility '{proposal.visibility}'"
        if proposal.visibility == "restricted" and not proposal.acl_groups:
            return "restricted nodes must name at least one acl group"
        return None

    if proposal.type not in EDGE_TYPES:
        return f"unknown edge type '{proposal.type}' (not in ontology)"
    allowed_src, allowed_dst = EDGE_TYPES[proposal.type]
    if proposal.src_type not in allowed_src:
        return f"{proposal.type} cannot start at {proposal.src_type}"
    if proposal.dst_type not in allowed_dst:
        return f"{proposal.type} cannot end at {proposal.dst_type}"
    # An endpoint type must be SOMETHING this system knows about. External
    # types are known; unknown ones are a producer inventing a label, which is
    # the failure this file exists to stop.
    for role, node_type in (("source", proposal.src_type), ("target", proposal.dst_type)):
        if node_type not in NODE_TYPES and node_type not in EXTERNAL_TYPES:
            return f"unknown {role} type '{node_type}' (not in ontology)"
    if proposal.provenance is None:
        return "edge without provenance is inadmissible"
    if not proposal.provenance.source or not proposal.provenance.producer:
        return "provenance must name a source and a producer"
    if not 0.0 <= proposal.provenance.confidence <= 1.0:
        return f"confidence {proposal.provenance.confidence} outside [0,1]"
    return None


def as_row(proposal: NodeProposal | EdgeProposal) -> dict[str, Any]:
    """Flatten to a driver-friendly row. Storage never sees the dataclasses,
    which is what keeps the backend swappable."""
    provenance = asdict(proposal.provenance) if proposal.provenance else {}
    base = {"hash": content_hash(proposal), "ns": proposal.ns, "type": proposal.type,
            "props": proposal.props, **{f"prov_{k}": v for k, v in provenance.items()}}
    if isinstance(proposal, NodeProposal):
        base.update(id=proposal.id, visibility=proposal.visibility,
                    aclGroups=proposal.acl_groups)
    else:
        base.update(srcId=proposal.src_id, dstId=proposal.dst_id,
                    srcType=proposal.src_type, dstType=proposal.dst_type)
    return base
