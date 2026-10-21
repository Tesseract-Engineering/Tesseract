"""
interfaces.py - what a source has to provide, and nothing more.

Each source is a thin fetcher behind Connector plus a Normalizer to item
records. Redaction, ACL checks, extraction and writing are shared stages, so
adding a source means writing these two classes and nothing else.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

from .records import Acl, ItemRecord


@dataclass(frozen=True)
class RawContainer:
    """Something in scope that holds items: a repo, project, channel or drive."""
    source: str
    id: str                                   # the container's ID, swarm/ids.py format
    name: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class RawItem:
    """One object as the source returned it, after redaction. `payload` is
    what the raw/ prefix keeps for 14 days."""
    source: str
    container: RawContainer
    native_id: str                            # the source's own id, before ID building
    payload: dict[str, Any]
    fetched_at: str


@dataclass(frozen=True)
class Checkpoint:
    """Where the last complete run of one container got to. Advanced only
    after that run's manifest is written."""
    container_id: str
    cursor: str                               # source-specific: an updated_at, a ts, a page token
    updated_at: str


class Connector(Protocol):
    source: str

    def enumerate(self) -> Iterator[RawContainer]: ...
    def fetch(self, container: RawContainer, since: Checkpoint | None) -> Iterator[RawItem]: ...
    def list_ids(self, container: RawContainer) -> Iterator[str]: ...   # for reconciliation
    def acl(self, item: RawItem) -> Acl: ...


class Normalizer(Protocol):
    def to_item(self, raw: RawItem, acl: Acl) -> ItemRecord: ...
