"""
write.py - records to JSONL part files, in the bucket layout.

    runs/{source}/{run_id}/items/part-00001.jsonl
    runs/{source}/{run_id}/extractions/part-00001.jsonl
    runs/{source}/{run_id}/changes/part-00001.jsonl      tombstones and acl_updates
    runs/{source}/{run_id}/groups/part-00001.jsonl       group memberships
    runs/identity/{run_id}/identities/part-00001.jsonl
    raw/{source}/{run_id}/...                            redacted payloads, expire after 14 days
    state/{source}/...                                   what earlier complete runs wrote, the
                                                         extraction backlog and batches in flight

A writer only ever touches its own source's prefixes. It is idempotent: an
item whose content_hash matches what an earlier complete run wrote is
skipped, and if only its ACL moved, an acl_update is written instead. So
rerunning a window writes zero new records.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from ..records import (
    FOLDER,
    AclUpdateRecord,
    ExtractionRecord,
    GroupRecord,
    IdentityRecord,
    ItemRecord,
    TombstoneRecord,
    dump_line,
)
from ..store import ObjectStore
from ..timeutil import iso

PART_SIZE = 5000


def new_run_id(source: str, now: datetime | None = None) -> str:
    stamp = iso(now or datetime.now(timezone.utc))
    return f"{stamp}-{source}"


def run_prefix(source: str, run_id: str) -> str:
    return f"runs/{source}/{run_id}/"


def state_key(source: str, name: str) -> str:
    return f"state/{source}/{name}.json"


def _digest(value: Any) -> str:
    blob = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _acl_digest(item: ItemRecord) -> str:
    return _digest(item.acl.model_dump(mode="json"))


def _extraction_key(item_id: str, item_version: str, prompt_version: str) -> str:
    return f"{item_id}@{item_version}#{prompt_version}"


class RunWriter:
    """Buffers one run's records and writes them as part files.

    Nothing here writes the manifest or advances state; manifest.finish_run
    does both, in that order, once every part is flushed.
    """

    def __init__(self, store: ObjectStore, source: str, run_id: str, *, part_size: int = PART_SIZE):
        self.store = store
        self.source = source
        self.run_id = run_id
        self.part_size = part_size
        self.prefix = run_prefix(source, run_id)
        self.files: list[str] = []
        self.counts = {"items": 0, "extractions": 0, "changes": 0, "groups": 0, "identities": 0, "rejected": 0}
        self.rejections: dict[str, int] = {}
        self._buffers: dict[str, list[str]] = {folder: [] for folder in set(FOLDER.values())}
        self._parts: dict[str, int] = {folder: 0 for folder in self._buffers}
        self._keys_this_run: set[tuple[str, str]] = set()
        # What earlier complete runs wrote, so a rerun writes nothing new.
        seen = self._load_state("seen", {})
        self.seen_items: dict[str, list[str]] = seen.get("items", {})        # id -> [content_hash, acl digest]
        self.seen_extractions: set[str] = set(seen.get("extractions", []))
        self.seen_tombstones: set[str] = set(seen.get("tombstones", []))
        self.seen_identities: dict[str, str] = seen.get("identities", {})  # person_id -> cluster digest
        self.seen_groups: dict[str, str] = seen.get("groups", {})          # group id -> members digest
        # Items due an extraction that did not get one (over budget, model
        # down): item id -> the item record as JSON. See extract.py.
        self.backlog: dict[str, str] = self._load_state("backlog", {})
        # Message Batches submitted but not yet collected: batch id ->
        # {custom_id: [item id, prompt version, item version]}. Their items
        # stay in the backlog until the answers are read.
        self.batches: dict[str, dict[str, list[str]]] = self._load_state("batches", {})
        # Items that name a GitHub link or Jira key no ref stands for yet:
        # item id -> the item record as JSON. Each run resolves them again
        # (swarm/sources/links.py, relink).
        self.unresolved: dict[str, str] = self._load_state("unresolved", {})

    # -- state ------------------------------------------------------------ #

    def _load_state(self, name: str, default: Any) -> Any:
        raw = self.store.get(state_key(self.source, name))
        return json.loads(raw) if raw else default

    def seen_state(self) -> dict[str, Any]:
        return {"items": self.seen_items, "extractions": sorted(self.seen_extractions),
                "tombstones": sorted(self.seen_tombstones), "identities": self.seen_identities,
                "groups": self.seen_groups}

    def is_unchanged(self, item_id: str, content_hash: str) -> bool:
        """True when an earlier run already wrote this exact content, which is
        also the content-hash check that skips re-extraction."""
        seen = self.seen_items.get(item_id)
        return bool(seen) and seen[0] == content_hash

    def has_extraction(self, item_id: str, item_version: str, prompt_version: str) -> bool:
        """True when this item version was already extracted with this prompt
        version, by an earlier run or this one."""
        return _extraction_key(item_id, item_version, prompt_version) in self.seen_extractions

    def settle_extraction(self, item_id: str, item_version: str, prompt_version: str) -> None:
        """Count this item version as extracted without a record: the model
        gave no answer it would give differently if asked again."""
        self.seen_extractions.add(_extraction_key(item_id, item_version, prompt_version))

    # -- records ---------------------------------------------------------- #

    def add_item(self, item: ItemRecord) -> str:
        """Write an item, an acl_update, or nothing. Returns which."""
        if item.meta.get("jira_keys") or item.meta.get("github_links"):
            self.unresolved[item.id] = item.model_dump_json()
        else:
            self.unresolved.pop(item.id, None)
        acl = _acl_digest(item)
        seen = self.seen_items.get(item.id)
        if seen and seen[0] == item.content_hash:
            if seen[1] == acl:
                return "unchanged"
            self.add(AclUpdateRecord(id=item.id, source=item.source, acl=item.acl,
                                     observed_at=item.fetched_at, run_id=self.run_id))
            return "acl_update"
        if not self._once(("item", f"{item.id}@{item.version}")):
            return "duplicate"
        self._append("items", item)
        self.seen_items[item.id] = [item.content_hash, acl]
        return "item"

    def add_extraction(self, record: ExtractionRecord) -> bool:
        key = _extraction_key(record.item_id, record.item_version, record.extractor.prompt_version)
        if key in self.seen_extractions or not self._once(("extraction", key)):
            return False
        self._append("extractions", record)
        self.seen_extractions.add(key)
        return True

    def add(self, record: TombstoneRecord | AclUpdateRecord | GroupRecord | IdentityRecord) -> bool:
        """Write a change or identity record unless an earlier run already
        said the same thing."""
        if isinstance(record, TombstoneRecord):
            if record.id in self.seen_tombstones or not self._once(("tombstone", record.id)):
                return False
            self.seen_tombstones.add(record.id)
        elif isinstance(record, AclUpdateRecord):
            digest = _digest(record.acl.model_dump(mode="json"))
            seen = self.seen_items.get(record.id)
            if (seen and seen[1] == digest) or not self._once(("acl_update", record.id)):
                return False
            self.seen_items[record.id] = [seen[0] if seen else "", digest]
        elif isinstance(record, GroupRecord):
            digest = _digest({"name": record.name, "members": sorted(record.members),
                              "unresolved": record.unresolved})
            if self.seen_groups.get(record.id) == digest or not self._once(("group", record.id)):
                return False
            self.seen_groups[record.id] = digest
        else:
            digest = _digest({"members": [m.model_dump(mode="json") for m in record.members],
                              "method": record.method, "merged_into": record.merged_into})
            if self.seen_identities.get(record.person_id) == digest or not self._once(("identity", record.person_id)):
                return False
            self.seen_identities[record.person_id] = digest
        self._append(FOLDER[record.record_type], record)
        return True

    def reject(self, reason: str) -> None:
        """Count something that failed validation and was not written."""
        self.counts["rejected"] += 1
        self.rejections[reason] = self.rejections.get(reason, 0) + 1

    def write_raw(self, name: str, payload: dict[str, Any]) -> None:
        """Keep a redacted source payload under raw/, for 14 days (bucket lifecycle)."""
        key = f"raw/{self.source}/{self.run_id}/{name}.json"
        self.store.put(key, json.dumps(payload, sort_keys=True, default=str).encode("utf-8"))

    # -- internals -------------------------------------------------------- #

    def _once(self, key: tuple[str, str]) -> bool:
        if key in self._keys_this_run:
            return False
        self._keys_this_run.add(key)
        return True

    def _append(self, folder: str, record: Any) -> None:
        self._buffers[folder].append(dump_line(record))
        self.counts[folder] += 1
        if len(self._buffers[folder]) >= self.part_size:
            self._flush(folder)

    def _flush(self, folder: str) -> None:
        lines = self._buffers[folder]
        if not lines:
            return
        self._parts[folder] += 1
        name = f"{folder}/part-{self._parts[folder]:05d}.jsonl"
        self.store.put(self.prefix + name, ("\n".join(lines) + "\n").encode("utf-8"))
        self.files.append(name)
        self._buffers[folder] = []

    def flush(self) -> None:
        for folder in sorted(self._buffers):
            self._flush(folder)
