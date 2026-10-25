"""
bucket.py - the bucket as the graph side would read it, for checks and reviews.

Follows the reading rules in docs/SWARM.md: only runs whose manifest says
complete, only the files it lists; of two item records with one id, the
later updated_at (then fetched_at) is current. Read-only.

    from eval.bucket import Bucket
    b = Bucket.from_env()          # S3_BUCKET, S3_ENDPOINT and AWS credentials, as the pipelines use
    b.jira("SBX-4")                # an item by alias; also b.github("tesseract-sandbox/auth-service#4")
"""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field

from swarm.records import ExtractionRecord, GroupRecord, IdentityRecord, ItemRecord, parse_line
from swarm.store import ObjectStore, S3Store

SOURCES = ("github", "jira", "slack", "gdrive", "identity")


@dataclass
class Bucket:
    items: dict[str, ItemRecord] = field(default_factory=dict)
    extractions: dict[str, list[ExtractionRecord]] = field(default_factory=lambda: defaultdict(list))
    groups: dict[str, GroupRecord] = field(default_factory=dict)
    identities: dict[str, IdentityRecord] = field(default_factory=dict)
    aliases: dict[tuple[str, str], str] = field(default_factory=dict)      # (source, alias) -> item id
    runs: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    store: ObjectStore | None = None

    @classmethod
    def from_env(cls) -> Bucket:
        return cls.load(S3Store())

    @classmethod
    def load(cls, store: ObjectStore) -> Bucket:
        b = cls(store=store)
        for source in SOURCES:
            for key in sorted(k for k in store.list(f"runs/{source}/") if k.endswith("/_manifest.json")):
                manifest = json.loads(store.get(key))
                if manifest.get("status") != "complete":
                    continue
                b.runs[source].append(manifest["run_id"])
                prefix = key.rsplit("/", 1)[0] + "/"
                for name in manifest["files"]:
                    for line in store.get(prefix + name).decode("utf-8").splitlines():
                        if line.strip():
                            b._add(parse_line(line))
        for item_id, item in b.items.items():
            for alias in item.aliases:
                b.aliases[(item.source, alias)] = item_id
        return b

    def _add(self, record) -> None:
        if isinstance(record, ItemRecord):
            current = self.items.get(record.id)
            if current is None or (record.updated_at, record.fetched_at) >= (current.updated_at, current.fetched_at):
                self.items[record.id] = record
        elif isinstance(record, ExtractionRecord):
            self.extractions[record.item_id].append(record)
        elif isinstance(record, GroupRecord):
            current = self.groups.get(record.id)
            if current is None or record.observed_at >= current.observed_at:
                self.groups[record.id] = record
        elif isinstance(record, IdentityRecord):
            self.identities[record.person_id] = record

    # -- lookups ------------------------------------------------------------ #

    def alias(self, source: str, name: str) -> ItemRecord | None:
        item_id = self.aliases.get((source, name))
        return self.items.get(item_id) if item_id else None

    def jira(self, key: str) -> ItemRecord | None:
        return self.alias("jira", key)

    def github(self, name: str) -> ItemRecord | None:
        return self.alias("github", name)

    def drive(self, title: str) -> ItemRecord | None:
        found = [i for i in self.items.values() if i.source == "gdrive" and i.title == title]
        return found[0] if len(found) == 1 else None

    def of(self, source: str, kind: str | None = None) -> list[ItemRecord]:
        return [i for i in self.items.values() if i.source == source and (kind is None or i.kind == kind)]

    def every_object(self) -> list[str]:
        """Every record of every complete run, superseded ones included, and
        every raw payload: what a leak check has to look through."""
        assert self.store is not None
        out = []
        for source, run_ids in self.runs.items():
            for run_id in run_ids:
                out += [self.store.get(k).decode("utf-8", "replace") for k in self.store.list(f"runs/{source}/{run_id}/")]
        out += [self.store.get(k).decode("utf-8", "replace") for k in self.store.list("raw/")]
        return out

    def extraction(self, item: ItemRecord) -> ExtractionRecord | None:
        """The newest extraction of the item's current version."""
        found = [e for e in self.extractions.get(item.id, []) if e.item_version == item.version]
        return max(found, key=lambda e: e.extracted_at) if found else None
