"""
records.py - record schema v2, the swarm's only output.

The record contract is the product boundary: everything upstream of it can
change freely, and it changes only by versioned agreement with the graph
engineer. These models are the contract in code; swarm/schemas/*.json is the
same contract generated for the graph side (`python -m swarm.schema`).

Six record types, one per line of a JSONL part file:

  item         a normalized source object with its deterministic facts
  extraction   entities and relations the LLM inferred from one item version
  tombstone    an item no longer exists or is out of scope
  acl_update   an item's permissions changed with no content change
  group        who is in a group that ACLs name
  identity     a cluster of source identities that are the same person

plus the run manifest, written last, which is what makes a run readable.

Every check here is mechanical. Nothing asks whether a fact is true, only
whether it is well-formed: IDs match the ID formats, types are in the closed
ontology, relations join the types they allow, ACLs are present, and evidence
points at real text.

Reading rules (the graph side's half of the contract; docs/SWARM.md):

  1. Read a run only once _manifest.json exists with status complete, and
     only the files it lists. Apply runs in run_id order, identity runs after
     the source runs of the same night.
  2. Newest content wins: of two item records with one id, the one with the
     later updated_at (then the later fetched_at) is current. `version` says
     whether two records are the same version, not which is newer.
  3. Newest permissions win: an item's acl is the one from its latest item or
     acl_update record, by fetched_at / observed_at. A group's members are
     those of its latest group record, by observed_at.
  4. A ref may point at an id not ingested yet; keep it and resolve it when
     that item arrives. A tombstone or acl_update for an id never ingested is
     a no-op.
  5. A tombstone retires the item and every extraction with that item_id.
  6. A source identity belongs to exactly one cluster: the one in the latest
     identity record that lists it. A record with merged_into retires its
     cluster.
"""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Any, Literal, Union

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from . import SCHEMA_VERSION, ids, ontology

Source = Literal["github", "jira", "slack", "gdrive"]
ItemKind = Literal["repo", "commit", "pull_request", "issue", "thread", "channel", "document", "folder"]
Visibility = Literal["public", "internal", "restricted"]

# A principal names who may read:
#   user:{email}                one person, by the email they sign in with
#   group:{source}:{immutable}  a source-native group, named by immutable ids
#                               only; its members arrive as a group record
#   domain:{domain}             everyone with an email in that domain
USER_PRINCIPAL = r"user:[^@\s]+@[^@\s]+"
GROUP_PRINCIPAL = r"group:(github|jira|slack|gdrive):\S+"
PRINCIPAL_PATTERN = rf"^({USER_PRINCIPAL}|{GROUP_PRINCIPAL}|domain:[a-z0-9.-]+)$"
CONTENT_HASH_PATTERN = r"^sha256:[0-9a-f]{64}$"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _check_id(value: str, what: str) -> str:
    if not ids.is_valid(value):
        raise ValueError(f"{what} {value!r} does not match any ID format in swarm/ids.py")
    return value


# --------------------------------------------------------------------------- #
# shared parts
# --------------------------------------------------------------------------- #

class Acl(_Model):
    """Who may read an item, carried from the source.

    public      anyone (a public repo)
    internal    anyone in the customer's organisation
    restricted  only the listed principals
    """
    visibility: Visibility
    principals: list[Annotated[str, Field(pattern=PRINCIPAL_PATTERN)]] = Field(default_factory=list)

    @model_validator(mode="after")
    def _restricted_names_someone(self) -> Acl:
        if self.visibility == "restricted" and not self.principals:
            raise ValueError("a restricted acl must name at least one principal")
        return self


class Person(_Model):
    """One raw source identity on an item, not yet resolved across sources."""
    id: str = Field(description="person:{source}:{handle}")
    source: Source
    handle: str
    name: str | None = None
    email: str | None = None
    roles: list[str] = Field(min_length=1)

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        if ids.kind_of(value) != "person_source":
            raise ValueError(f"person id {value!r} must be person:{{source}}:{{handle}}")
        return value

    @field_validator("roles")
    @classmethod
    def _roles(cls, value: list[str]) -> list[str]:
        unknown = set(value) - set(ontology.load().person_roles)
        if unknown:
            raise ValueError(f"unknown person roles {sorted(unknown)} (not in ontology.yaml)")
        return value


class Ref(_Model):
    """A deterministic, typed link from this item to another object."""
    type: str = Field(description="A relation type from ontology.yaml")
    target: str = Field(description="An ID in one of the formats in swarm/ids.py")
    via: Literal["field", "url", "key", "closing_keyword", "mention"] = Field(
        description="How the link was found: a structured source field, a URL or key in the text, "
                    "a closing keyword such as 'Fixes #12', or an @-mention")

    @field_validator("type")
    @classmethod
    def _type(cls, value: str) -> str:
        onto = ontology.load()
        if value not in onto.relations:
            raise ValueError(f"unknown relation type {value!r} (not in ontology.yaml)")
        if value not in onto.deterministic_relations:
            raise ValueError(f"relation {value!r} is LLM-only and cannot be a ref")
        return value

    @field_validator("target")
    @classmethod
    def _target(cls, value: str) -> str:
        return _check_id(value, "ref target")


class Redaction(_Model):
    """What the redact stage stripped, never the secret itself."""
    field: str
    type: str = Field(description="Detector name, e.g. 'GitHub Token'")
    count: int = Field(ge=1)


class Span(_Model):
    """A quoted span of an item's text: text[start:end] == quote.

    Offsets count Unicode code points (Python str indexing), not UTF-16 code
    units or bytes. A JavaScript reader converts with Array.from(text).
    """
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    quote: str = Field(min_length=1)

    @model_validator(mode="after")
    def _ordered(self) -> Span:
        if self.end <= self.start:
            raise ValueError("span end must be after start")
        if self.end - self.start != len(self.quote):
            raise ValueError("span length does not match quote length")
        return self


def content_hash(*, title: str, text: str, container: str | None, people: list[Any],
                 refs: list[Any], meta: dict[str, Any]) -> str:
    """Stable digest of an item's MEANING.

    The ACL is excluded on purpose: a permission change with no content change
    is an acl_update, not a new version. run_id and fetched_at are excluded so
    that a rerun of the same window produces the same hash and writes nothing.
    """
    def plain(values: list[Any]) -> list[Any]:
        return [v.model_dump(mode="json") if isinstance(v, BaseModel) else v for v in values]

    payload = {"title": title, "text": text, "container": container,
               "people": plain(people), "refs": plain(refs), "meta": meta}
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# record types
# --------------------------------------------------------------------------- #

class _Record(_Model):
    schema_version: Literal["2"] = SCHEMA_VERSION
    run_id: str = Field(min_length=1)


class ItemRecord(_Record):
    record_type: Literal["item"] = "item"
    id: str = Field(description="Stable ID from immutable source IDs; see swarm/ids.py")
    aliases: list[str] = Field(default_factory=list, description="Mutable names: repo slug, Jira key, channel name")
    kind: ItemKind
    source: Source
    container: str | None = Field(description="ID of the repo, channel, project or folder holding this item")
    title: str
    text: str = Field(description="Redacted text; comments and replies folded in")
    url: str
    created_at: AwareDatetime
    updated_at: AwareDatetime
    version: str = Field(min_length=1, description="Source version marker: updated_at, revision id or edit ts. "
                                                    "Equality only; order versions by updated_at")
    content_hash: str = Field(pattern=CONTENT_HASH_PATTERN)
    people: list[Person] = Field(default_factory=list)
    refs: list[Ref] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    acl: Acl
    redactions: list[Redaction] = Field(default_factory=list)
    fetched_at: AwareDatetime

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _check_id(value, "item id")

    @field_validator("container")
    @classmethod
    def _container(cls, value: str | None) -> str | None:
        return None if value is None else _check_id(value, "container")

    @model_validator(mode="after")
    def _consistent(self) -> ItemRecord:
        if not self.id.startswith(f"{self.source}:"):
            raise ValueError(f"item id {self.id!r} does not belong to source {self.source!r}")
        onto = ontology.load()
        for ref in self.refs:
            if ref.type == "part_of" and ref.target == self.container:
                raise ValueError("the container goes in `container` only, not also as a part_of ref")
            if not onto.allows(ref.type, {self.kind}, ids.entity_types(ref.target)):
                raise ValueError(f"{ref.type} cannot run from a {self.kind} to {ref.target!r} (ontology.yaml)")
        expected = content_hash(title=self.title, text=self.text, container=self.container,
                                people=self.people, refs=self.refs, meta=self.meta)
        if self.content_hash != expected:
            raise ValueError("content_hash does not match the item's content")
        return self


class Extractor(_Model):
    model: str = Field(description="Model id, e.g. claude-sonnet-5-5")
    prompt_version: str = Field(description="Versioned prompt name, e.g. decisions-v3")


class Entity(_Model):
    local_id: str = Field(pattern=r"^e\d+$", description="Unique within this record: e1, e2, ...")
    type: str = Field(description="An LLM entity type from ontology.yaml")
    name: str = Field(min_length=1)
    attributes: dict[str, Any] = Field(default_factory=dict, description="Open extensions live only here")
    evidence: list[Span] = Field(min_length=1)

    @field_validator("type")
    @classmethod
    def _type(cls, value: str) -> str:
        onto = ontology.load()
        if value in onto.item_backed or value == "person":
            raise ValueError(f"{value!r} entities come from item records, never from the LLM")
        if value not in onto.llm_entities:
            raise ValueError(f"unknown entity type {value!r} (not in ontology.yaml)")
        return value


class Relation(_Model):
    type: str = Field(description="An LLM relation type from ontology.yaml")
    source: str = Field(description="An entity local_id in this record, or an ID from swarm/ids.py")
    target: str = Field(description="An entity local_id in this record, or an ID from swarm/ids.py")
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[Span] = Field(min_length=1)

    @field_validator("type")
    @classmethod
    def _type(cls, value: str) -> str:
        if value not in ontology.load().llm_relations:
            raise ValueError(f"relation {value!r} is not an LLM relation type in ontology.yaml")
        return value


class ExtractionRecord(_Record):
    record_type: Literal["extraction"] = "extraction"
    item_id: str
    item_version: str = Field(min_length=1)
    source: Source
    extractor: Extractor
    entities: list[Entity] = Field(default_factory=list)
    relations: list[Relation] = Field(default_factory=list)
    extracted_at: AwareDatetime

    @field_validator("item_id")
    @classmethod
    def _item_id(cls, value: str) -> str:
        return _check_id(value, "item_id")

    @model_validator(mode="after")
    def _endpoints(self) -> ExtractionRecord:
        local = {e.local_id: e.type for e in self.entities}
        if len(local) != len(self.entities):
            raise ValueError("entity local_ids must be unique within a record")
        onto = ontology.load()

        def types_of(end: str) -> frozenset[str]:
            if end in local:
                return frozenset({local[end]})
            if not ids.is_valid(end):
                raise ValueError(f"relation endpoint {end!r} is neither a local_id here nor a valid ID")
            return ids.entity_types(end)

        for rel in self.relations:
            if not onto.allows(rel.type, types_of(rel.source), types_of(rel.target)):
                raise ValueError(f"{rel.type} cannot run from {rel.source!r} to {rel.target!r} (ontology.yaml)")
        return self


class TombstoneRecord(_Record):
    record_type: Literal["tombstone"] = "tombstone"
    id: str
    source: Source
    reason: Literal["deleted", "out_of_scope", "access_revoked"]
    detected_at: AwareDatetime

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _check_id(value, "tombstone id")


class AclUpdateRecord(_Record):
    record_type: Literal["acl_update"] = "acl_update"
    id: str
    source: Source
    acl: Acl
    observed_at: AwareDatetime

    @field_validator("id")
    @classmethod
    def _id(cls, value: str) -> str:
        return _check_id(value, "acl_update id")


class IdentityMember(_Model):
    id: str = Field(description="person:{source}:{handle}, as on item records")
    source: Source
    handle: str
    email: str | None = None
    name: str | None = None

    @model_validator(mode="after")
    def _id_matches(self) -> IdentityMember:
        if self.id != ids.person(self.source, self.handle):
            raise ValueError(f"member id {self.id!r} must be {ids.person(self.source, self.handle)!r}")
        return self


class IdentityRecord(_Record):
    record_type: Literal["identity"] = "identity"
    person_id: str = Field(description="person:c:{cluster_hash}, stable across runs")
    members: list[IdentityMember] = Field(default_factory=list)
    method: Literal["email", "llm", "manual"]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = ""
    merged_into: str | None = Field(default=None, description="Set when this cluster was merged into another; "
                                                              "the cluster is retired and lists no members")

    @field_validator("person_id", "merged_into")
    @classmethod
    def _person_id(cls, value: str | None) -> str | None:
        if value is not None and ids.kind_of(value) != "person_cluster":
            raise ValueError(f"{value!r} must be person:c:{{cluster_hash}}")
        return value

    @model_validator(mode="after")
    def _members_or_merged(self) -> IdentityRecord:
        if self.merged_into is None and not self.members:
            raise ValueError("an identity record lists members unless it is merged_into another cluster")
        if self.merged_into is not None and self.members:
            raise ValueError("a merged cluster lists no members; they appear on the surviving cluster")
        if self.merged_into == self.person_id:
            raise ValueError("a cluster cannot be merged into itself")
        return self


class GroupRecord(_Record):
    """Who is in a group that item ACLs name. The graph side checks a viewer
    against an item by expanding its group principals through these."""
    record_type: Literal["group"] = "group"
    id: str = Field(pattern=rf"^{GROUP_PRINCIPAL}$", description="The group principal, e.g. "
                    "group:slack:T01ACME:channel:C42AUTH:members")
    source: Source
    name: str = Field(default="", description="Display name; mutable, never used for matching")
    members: list[Annotated[str, Field(pattern=rf"^({USER_PRINCIPAL}|{GROUP_PRINCIPAL})$")]] = Field(
        default_factory=list, description="Users and nested groups. Members with no known email are left out "
                                          "and counted in unresolved")
    unresolved: int = Field(default=0, ge=0, description="Members the source listed without an email")
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def _own_source(self) -> GroupRecord:
        if not self.id.startswith(f"group:{self.source}:"):
            raise ValueError(f"group {self.id!r} does not belong to source {self.source!r}")
        if self.id in self.members:
            raise ValueError("a group cannot contain itself")
        return self


Record = Annotated[
    Union[ItemRecord, ExtractionRecord, TombstoneRecord, AclUpdateRecord, GroupRecord, IdentityRecord],
    Field(discriminator="record_type"),
]
RECORD_ADAPTER: TypeAdapter[Record] = TypeAdapter(Record)

# Which part folder each record type is written under.
FOLDER = {"item": "items", "extraction": "extractions", "tombstone": "changes",
          "acl_update": "changes", "group": "groups", "identity": "identities"}


def parse_line(line: str) -> Record:
    """Validate one JSONL line into its record type."""
    return RECORD_ADAPTER.validate_json(line)


def dump_line(record: BaseModel) -> str:
    return record.model_dump_json(exclude_none=False)


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #

class Tokens(_Model):
    input: int = Field(default=0, ge=0)
    output: int = Field(default=0, ge=0)


class Manifest(_Model):
    """Written last. The graph side reads a run only once this exists and
    status is complete; a failed run writes none and is retried."""
    schema_version: Literal["2"] = SCHEMA_VERSION
    source: Literal["github", "jira", "slack", "gdrive", "identity"]
    run_id: str = Field(min_length=1)
    status: Literal["complete"] = "complete"
    started_at: AwareDatetime
    finished_at: AwareDatetime
    counts: dict[Literal["items", "extractions", "changes", "groups", "identities", "rejected"], int]
    files: list[str] = Field(description="Part files of this run, relative to runs/{source}/{run_id}/")
    extractor_versions: list[str] = Field(default_factory=list)
    llm_budget_hit: bool = False
    tokens: Tokens = Field(default_factory=Tokens)

    @model_validator(mode="after")
    def _ordered(self) -> Manifest:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at is before started_at")
        return self
