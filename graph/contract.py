"""
contract.py - the only shape anything may write into the graph.

Producers do not write. They PROPOSE, and the door in ingest.py decides. That
split exists because instruction-following is not a filter: a model told to
"omit pairs with no stated relation" will still link most of them. Admission has
to be structural - checked in code, deterministically, with non-conforming
proposals dropped and counted.

Three properties this file enforces:

  NAMESPACING   every node carries `ns`, and uniqueness is (ns, id).
                Two builds can both have a "Person" without one's teardown
                deleting the other's.

  PROVENANCE    a fact with no source is not admissible, node or edge: where
                it came from, what produced it, and how sure.

  IDENTITY      content_hash() makes re-ingestion cheap and safe. Same content,
                same hash, no write. Changed content supersedes rather than
                overwrites, so history survives.

The types below are the swarm's ontology (ontology.yaml at the repo root) as
graph labels. build_swarm.py maps item kinds onto them: repos, channels, folders
and Jira projects are Containers; pull requests and issues are Issues; threads
and documents are Documents.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

SCHEMA_VERSION = "2.0"

# Node type -> properties it must carry. Adding a type is a deliberate edit here,
# never a runtime decision by a producer.
NODE_TYPES: dict[str, set[str]] = {
    # -- people ------------------------------------------------------------- #
    # A Person is a resolved human (a swarm identity cluster); an Account is
    # one source identity (a Slack user, a GitHub login, a Jira account).
    "Person":     {"name"},
    "Account":    {"handle"},
    # A swarm `group` record: who may read restricted items (repo readers,
    # private-channel members, Drive groups). Grants nothing when empty.
    "Group":      {"name"},
    # -- items (deterministic, one per source item) -------------------------- #
    "Container":  {"title"},     # repo, channel, folder, Jira project
    "Issue":      {"title"},     # pull request, issue
    "Commit":     {"title"},
    "Document":   {"title"},     # thread, document
    # -- what the swarm's LLM step extracts ---------------------------------- #
    # One node per thing and access set (build_swarm.py says why); every item
    # that names it keeps a MENTIONS edge with the quote.
    "Decision":   {"name"},
    "Project":    {"name"},
    "ActionItem": {"name"},
    "System":     {"name"},
    "Team":       {"name"},
    "Customer":   {"name"},
    # -- for a person to settle ---------------------------------------------- #
    # A match too weak to merge on its own (two accounts with the same name):
    # written down for review, never acted on.
    "ReviewItem": {"kind"},
}

_ITEMS = {"Container", "Issue", "Commit", "Document"}
_EXTRACTED = {"Decision", "Project", "ActionItem", "System", "Team", "Customer"}
_WHO = {"Person", "Account"}

# edge type -> (allowed source types, allowed target types)
EDGE_TYPES: dict[str, tuple[set[str], set[str]]] = {
    "HAS_ACCOUNT":     ({"Person"}, {"Account"}),
    # A group can be a member of another group (a Google group inside a Drive
    # group); readers follow MEMBER_OF transitively.
    "MEMBER_OF":       (_WHO | {"Group"}, {"Group", "Team"}),
    # -- people on items, from the items' own fields ------------------------- #
    "AUTHORED":        (_WHO, _ITEMS),
    # Approving or blocking a change is a stronger claim of knowledge than a comment.
    "REVIEWED":        (_WHO, {"Issue", "Document"}),
    # Took part without leading: a thread participant, a commenter, a last editor.
    "PARTICIPATED_IN": (_WHO, _ITEMS),
    "ASSIGNED_TO":     ({"Issue", "ActionItem"}, _WHO),
    "OWNS":            (_WHO | {"Team"}, {"System", "Project", "Container", "Document"}),
    # -- links between items ------------------------------------------------- #
    # Where an item lives: a PR in its repo, a thread in its channel, a file in
    # its folder, a ticket in its Jira project.
    "IN":              (_ITEMS, {"Container"}),
    "REFERENCES":      (_ITEMS, _ITEMS),
    # "Fixes #12" in a PR: stronger than REFERENCES, it says the target is done.
    "CLOSES":          ({"Issue"}, {"Issue"}),
    # The same closing keywords in a commit message.
    "IMPLEMENTS":      ({"Commit", "Issue"}, {"Issue"}),
    "BLOCKS":          ({"Issue"}, {"Issue"}),
    # Jira's own Duplicate link: the source ticket repeats the target.
    "DUPLICATES":      ({"Issue"}, {"Issue"}),
    # -- what the LLM step extracts ------------------------------------------ #
    # Aggregates such as "how much has this person done in this repo" are NOT
    # stored: summed over items with different ACLs, the total itself would
    # reveal private work. They are computed per viewer at query time.
    "MENTIONS":        (_ITEMS, _ITEMS | _EXTRACTED | _WHO),
    "DECIDED_IN":      ({"Decision"}, _ITEMS),
    "DECIDED_BY":      ({"Decision"}, _WHO | {"Team"}),
    "PART_OF":         (_ITEMS | {"Decision", "ActionItem", "System"}, {"Project", "Issue", "System", "Container"}),
    "WORKS_ON":        (_WHO | {"Team"}, {"Project", "System", "Container"}),
    "SUPERSEDES":      ({"Decision", "Document"}, {"Decision", "Document"}),
    "DEPENDS_ON":      ({"Project", "System", "Issue", "Container"}, {"Project", "System", "Issue", "Container"}),
}

VISIBILITIES = {"public", "internal", "restricted"}
Decision = Literal["auto_approve", "approve", "revise", "escalate", "reject"]


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class Provenance:
    """Where a fact came from. Without this a proposal is inadmissible.

    observed_at is the real-world date the content is ABOUT, which is not the
    same as when we ingested it: judging freshness by ingestion time tells you
    which crawler ran last, not which fact is newer.
    """
    source: str                      # 'github' | 'jira' | 'slack' | 'gdrive' | 'swarm'
    source_id: str                   # stable id within that source
    producer: str                    # who proposed it: 'swarm:ref:url', 'swarm:llm:<model>:<prompt>'
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


def _provenance_problem(provenance: Provenance | None, what: str) -> str | None:
    if provenance is None:
        return f"{what} without provenance is inadmissible"
    if not provenance.source or not provenance.producer:
        return "provenance must name a source and a producer"
    if not 0.0 <= provenance.confidence <= 1.0:
        return f"confidence {provenance.confidence} outside [0,1]"
    return None


def validate(proposal: NodeProposal | EdgeProposal) -> str | None:
    """Return a rejection reason, or None if admissible.

    Every check is mechanical. Nothing here asks whether a fact is *true* -
    only whether it is well-formed enough to be admitted and later audited.
    """
    if not proposal.ns or not proposal.ns.strip():
        return "missing namespace"

    if isinstance(proposal, NodeProposal):
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
        return _provenance_problem(proposal.provenance, "node")

    if proposal.type not in EDGE_TYPES:
        return f"unknown edge type '{proposal.type}' (not in ontology)"
    allowed_src, allowed_dst = EDGE_TYPES[proposal.type]
    if proposal.src_type not in allowed_src:
        return f"{proposal.type} cannot start at {proposal.src_type}"
    if proposal.dst_type not in allowed_dst:
        return f"{proposal.type} cannot end at {proposal.dst_type}"
    return _provenance_problem(proposal.provenance, "edge")


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
