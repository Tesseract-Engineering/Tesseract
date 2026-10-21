"""
records.py - record schema v2, the swarm's only output.

The record contract is the product boundary: everything upstream of it can
change freely, and it changes only by versioned agreement with the graph
engineer. These models are the contract in code; swarm/schemas/*.json is the
same contract generated for the graph side (`python -m swarm.schema`).

Five record types, one per line of a JSONL part file:

  item         a normalized source object with its deterministic facts
  extraction   entities and relations the LLM inferred from one item version
  tombstone    an item no longer exists or is out of scope
  acl_update   an item's permissions changed with no content change
  identity     a cluster of source identities that are the same person

plus the run manifest, written last, which is what makes a run readable.

Every check here is mechanical. Nothing asks whether a fact is true, only
whether it is well-formed: IDs match the ID formats, types are in the closed
ontology, ACLs are present, and evidence points at real text.
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

# A principal names who may read: one user, one group, or a whole domain.
# Source-native groups keep their source prefix, e.g.
# "group:slack:T01:channel:C42" for a channel's members.
PRINCIPAL_PATTERN = r"^(user|group|domain):\S+$"
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
        unknown = set(value) - ontology.load().person_roles
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
    """A quoted span of an item's text: text[start:end] == quote."""
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
    version: str = Field(min_length=1, description="Source version marker: updated_at, revision id or edit ts")
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
        local = [e.local_id for e in self.entities]
        if len(local) != len(set(local)):
            raise ValueError("entity local_ids must be unique within a record")
        for rel in self.relations:
            for end in (rel.source, rel.target):
                if end not in local and not ids.is_valid(end):
                    raise ValueError(f"relation endpoint {end!r} is neither a local_id here nor a valid ID")
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
    source: Source
    handle: str
    email: str | None = None
    name: str | None = None


class IdentityRecord(_Record):
    record_type: Literal["identity"] = "identity"
    person_id: str = Field(description="person:c:{cluster_hash}, stable across runs")
    members: list[IdentityMember] = Field(min_length=1)
    method: Literal["email", "llm", "manual"]
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str = ""

    @field_validator("person_id")
    @classmethod
    def _person_id(cls, value: str) -> str:
        if ids.kind_of(value) != "person_cluster":
            raise ValueError(f"person_id {value!r} must be person:c:{{cluster_hash}}")
        return value


Record = Annotated[
    Union[ItemRecord, ExtractionRecord, TombstoneRecord, AclUpdateRecord, IdentityRecord],
    Field(discriminator="record_type"),
]
RECORD_ADAPTER: TypeAdapter[Record] = TypeAdapter(Record)

# Which part folder each record type is written under.
FOLDER = {"item": "items", "extraction": "extractions", "tombstone": "changes",
          "acl_update": "changes", "identity": "identities"}


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
    counts: dict[Literal["items", "extractions", "changes", "identities", "rejected"], int]
    files: list[str] = Field(description="Part files of this run, relative to runs/{source}/{run_id}/")
    extractor_versions: list[str] = Field(default_factory=list)
    llm_budget_hit: bool = False
    tokens: Tokens = Field(default_factory=Tokens)

    @model_validator(mode="after")
    def _ordered(self) -> Manifest:
        if self.finished_at < self.started_at:
            raise ValueError("finished_at is before started_at")
        return self
