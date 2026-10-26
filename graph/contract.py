"""
contract.py - the only shape anything may write into the graph.

Agents, connectors and scripts do not write. They PROPOSE, and the door in
ingest.py decides. That split exists because of a result from the Agentic-Graph
project: told to "omit pairs with no stated relation", their model linked 1,897
of 1,860 candidates. Instruction-following is not a filter. Admission has to be
structural - checked in code, deterministically, with non-conforming proposals
dropped and counted.

Three properties this file enforces:

  NAMESPACING   every node carries `ns`, and uniqueness is (ns, id).
                Two producers can both have a "Person" without one's teardown
                deleting the other's. This is not hypothetical: kg/pipeline.py
                silently deleted the tutorial graph's people because both used
                the bare label :Person.

  PROVENANCE    a fact with no source is not admissible. Required on every
                edge: where it came from, what produced it, and how sure.

  IDENTITY      content_hash() makes re-ingestion cheap and safe. Same content,
                same hash, no write. Changed content supersedes rather than
                overwrites, so history survives.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Literal

SCHEMA_VERSION = "1.0"

# The ontology. Adding to it is a deliberate edit here, never a runtime decision
# by a producer - that is what stops 400 relationship types meaning one thing.
#
# Two ontologies live here, and they are not the same thing.
#
# The REAL one is what kg/ and repo-graph/ actually put in the database. The
# door could not be wired to those pipelines until it was able to say their
# names: it shipped describing Ticket, Message, CodeUnit, System and Concept,
# which are the shapes kg/synth.py generates for the benchmark and appear
# nowhere in the graph. A door that cannot express the facts you have is a
# door nobody can use, which is most of why this one stayed shut.
#
# The SYNTHETIC one is kept because bench.py measures against it, and a
# benchmark you rewrite at the same time as the thing it measures is not a
# benchmark. They coexist; nothing mixes them.
NODE_TYPES: dict[str, set[str]] = {
    # -- real: written by kg/pipeline.py and kg/domains.py ------------------ #
    "Person":     {"name"},
    "Account":    {"handle"},
    "Commit":     {"title"},
    "Issue":      {"title"},
    "Document":   {"title"},
    "Claim":      {"text"},
    "ReviewItem": {"kind"},
    "Domain":     {"name"},
    # A company graph's unit of ownership is a SERVICE, not a Python module.
    # The code-domain machinery derives a domain per module because that is
    # what a single repository gives you; a monorepo of nine services with an
    # org chart on top gives you something better, and declared.
    "Service":    {"name"},
    "Team":       {"name"},
    # A paying customer, named on support tickets. Separate from Person: a
    # customer is an account the company serves, not a human who works there.
    "Customer":   {"name"},
    # Karthik's swarm (schema v2) writes items that hold other items: a repo,
    # a Slack channel, a Drive folder. They are where work happens rather than
    # work itself, so they get their own label instead of passing as Documents.
    "Container":  {"title"},
    # A swarm `group` record: who may read restricted items (repo readers,
    # private-channel members, Drive groups). Grants nothing when empty.
    "Group":      {"name"},
    # What the swarm's LLM step extracts from an item (ontology.yaml). One node
    # per thing, however many items name it: build_swarm.py merges by type and
    # normalised name, and every item that names it keeps a MENTIONS edge with
    # the quote. System, Team and Customer above are reused for the same purpose.
    "Decision":   {"name"},
    "Project":    {"name"},
    "ActionItem": {"name"},
    # -- synthetic: kg/synth.py, for bench.py ------------------------------- #
    "Ticket":     {"title"},
    "Message":    {"text"},
    "CodeUnit":   {"name"},
    "System":     {"name"},
    "Concept":    {"name"},
}

# Node types this door does NOT own. repo-graph/build_graph.py parses them
# straight out of the source tree and writes them itself, so they have an id
# but no `ns` yet, and the door must be able to attach an edge to one without
# pretending to have created it.
#
# This is a seam for incremental adoption, not an exemption. An edge to an
# external endpoint gets every other guarantee - validation, provenance,
# content hash, supersede - but its endpoint is matched on id alone, so the
# (ns, id) uniqueness that protects everything else does not protect it. The
# fix is to route build_graph.py through here too, at which point these move
# up into NODE_TYPES and this set empties.
EXTERNAL_TYPES: set[str] = {"Repository", "Module", "Class", "Function", "ExternalPackage"}

# edge type -> (allowed source types, allowed target types)
_ARTIFACTS = {"Commit", "Issue", "Document"}
_CODE = {"Module", "Class", "Function"}
_ORG = {"Service", "Team"}

EDGE_TYPES: dict[str, tuple[set[str], set[str]]] = {
    # -- real ---------------------------------------------------------------- #
    "HAS_ACCOUNT": ({"Person"}, {"Account"}),
    "AUTHORED":    ({"Account", "Person"}, _ARTIFACTS | {"CodeUnit", "Ticket", "Message", "Document"}),
    # -- swarm (schema v2): the people roles and links its items carry -------- #
    # Where an item lives: a PR in its repo, a thread in its channel, a file in
    # its folder (and a folder in its parent folder).
    "IN":          (_ARTIFACTS | {"Container"}, {"Container"}),
    # A pull-request review. Separate from COMMENTED because approving or
    # blocking a change is a stronger claim of knowledge than a comment.
    "REVIEWED":    ({"Account"}, {"Issue", "Document"}),
    # Took part without leading: a thread participant, a Drive last editor, a
    # commenter on something that is not an Issue.
    "PARTICIPATED_IN": ({"Account"}, _ARTIFACTS | {"Container"}),
    # "Fixes #12" in a PR: a stronger link than REFERENCES, it says the target is done.
    "CLOSES":      ({"Issue"}, {"Issue"}),
    # The same closing keywords in a commit message.
    "IMPLEMENTS":  ({"Commit", "Issue"}, {"Issue"}),
    # -- swarm (schema v2): what the LLM step extracts ------------------------ #
    # Aggregates such as "how much has this person done in this repo" are NOT
    # stored as edges: summed over items with different ACLs, the total itself
    # would reveal private work. They are computed per viewer at query time,
    # from the role edges that viewer can read (swarm_queries.py).
    "DECIDED_IN":  ({"Decision"}, _ARTIFACTS | {"Container"}),
    "DECIDED_BY":  ({"Decision"}, {"Account", "Person", "Team"}),
    "PART_OF":     (_ARTIFACTS | {"Decision", "ActionItem", "System"}, {"Project", "Issue", "System"}),
    "WORKS_ON":    ({"Account", "Person", "Team"}, {"Project", "System"}),
    "SUPERSEDES":  ({"Decision", "Document"}, {"Decision", "Document"}),
    "BLOCKS":      ({"Issue"}, {"Issue"}),
    # Jira's own Duplicate link (ontology v2): the source ticket repeats the target.
    "DUPLICATES":  ({"Issue"}, {"Issue"}),
    "TOUCHED":     ({"Commit"}, {"Module"}),
    "REFERENCES":  (_ARTIFACTS | {"Container"}, _ARTIFACTS | {"Container"}),
    "FROM":        ({"Claim"}, _ARTIFACTS),
    "MEMBER_OF":   (_CODE | _ARTIFACTS | {"Person", "Service", "Account"}, {"Domain", "Team", "Group"}),
    "OWNS":        ({"Person", "Team", "Account"}, {"Domain", "System", "CodeUnit", "Service", "Document",
                                                    "Container", "Project"}),
    # -- shared between the two ontologies ----------------------------------- #
    "MENTIONS":    (_ARTIFACTS | {"Ticket", "Message"},
                    _CODE | _ORG | {"Issue", "CodeUnit", "System", "Concept", "Ticket", "Person",
                                    "Customer", "Account", "Decision", "Project", "ActionItem", "Team",
                                    "Container", "Document", "Commit"}),
    # ABOUT is the DECLARED subject - a Jira ticket's own `service` field -
    # while MENTIONS is what an extractor found in the prose. Keeping them as
    # separate edge types is the whole parsed-vs-inferred distinction applied
    # to this corpus, and it is what makes the declared field usable as
    # ground truth to score the extractor against.
    "ABOUT":       (_ARTIFACTS | {"Ticket", "Message", "Claim"}, _CODE | _ORG | {"Concept"}),
    # Who a ticket is assigned to, from the ticket's own field. Distinct from
    # AUTHORED (who filed it) because "who raised this" and "who is on the
    # hook for it" are different questions with different answers.
    "ASSIGNED_TO": ({"Issue", "Ticket", "ActionItem"}, {"Person", "Account"}),
    # A document's own header saying who stands behind it: "**Author**:",
    # "_Owner: ..._", "**Incident Commander**:", "Last updated ... by ...".
    # Carries `role`. Separate from MENTIONS because being named in a
    # handbook's contact list and having last updated the handbook are
    # different facts, and a single MENTIONS edge made them indistinguishable.
    "CREDITS":     ({"Document"}, {"Person"}),
    # Took part in a ticket's discussion - a Jira comment, a Zendesk internal
    # note, a pull-request review - as opposed to filing it (AUTHORED) or
    # owning it (ASSIGNED_TO). Carries `as` and a count.
    "COMMENTED":   ({"Account"}, {"Issue"}),
    # Which customer a support ticket was raised for. Declared by the ticket.
    "FOR_CUSTOMER": ({"Issue"}, {"Customer"}),
    # -- synthetic ------------------------------------------------------------ #
    "DEPENDS_ON":  ({"CodeUnit", "System", "Module", "Service", "Project", "Issue"},
                    {"CodeUnit", "System", "Module", "ExternalPackage", "Service", "Project", "Issue"}),
    "RESOLVES":    ({"Ticket", "Document"}, {"Ticket"}),
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
    same as when we ingested it. Agentic-Graph found this distinction is what
    makes recency-based superseding work at all: judging freshness by ingestion
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
            return (f"{proposal.type} is owned by repo-graph/build_graph.py - "
                    f"this door may link to it, not create it")
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
