"""
swarm_records.py - read the swarm's output (record schema v2) into the current state of the company.

The swarm (github.com/Tesseract-Engineering/Tesseract, branch `swarm`, docs/SWARM.md) writes runs into a bucket:

    runs/{source}/{run_id}/_manifest.json            written last; status "complete"
    runs/{source}/{run_id}/items/part-00001.jsonl    item records
    runs/{source}/{run_id}/extractions/...           LLM extraction records
    runs/{source}/{run_id}/changes/...               tombstone and acl_update records
    runs/{source}/{run_id}/groups/...                group records
    runs/identity/{run_id}/identities/...            identity records

This module applies SWARM.md's reading rules and hands back plain Python: the current version of
every live item with its current ACL, every group, which person each source identity belongs to, and
the extractions that still apply. It decides nothing about the graph; build_swarm.py does that.

The reading rules, as implemented here:
  1. only runs whose manifest says complete, only the files it lists; source runs in run_id order,
     each night's identity run after that night's source runs
  2. newest content wins: later updated_at, then fetched_at. `version` only says "same content"
  3. newest permissions win: latest item or acl_update, by fetched_at / observed_at
  4. a ref to an unknown id is kept (build_swarm resolves it later); a tombstone or acl_update for an
     id never ingested is a no-op
  5. a tombstone retires the item and every extraction of it
  6. a source identity belongs to the cluster of the latest identity record listing it;
     a record with merged_into retires its cluster
  7. a group member written as a source identity is that person (build_swarm resolves it)
  8. evidence offsets count Unicode code points - which is what Python string slicing counts

Where the runs come from is a Store. LocalStore reads a folder (a downloaded run, or the fixtures);
an S3 store slots in once there is a bucket we can reach. Folder names are never trusted for meaning:
run ids come from the manifests, because a run id holds ':' and Windows folders cannot.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Protocol

SCHEMA_VERSION = "2"


class Store(Protocol):
    def manifests(self) -> Iterator[tuple[str, dict]]:
        """(run folder key, manifest) for every _manifest.json."""

    def lines(self, run_key: str, rel: str) -> Iterator[dict]:
        """Records of one file the manifest lists, relative to its run folder."""


class LocalStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def manifests(self):
        for path in sorted(self.root.glob("runs/*/*/_manifest.json")):
            yield str(path.parent), json.loads(path.read_text(encoding="utf-8"))

    def lines(self, run_key, rel):
        for line in (Path(run_key) / rel).read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line)


class S3Store:
    """The swarm's bucket itself: S3, GCS through its S3 API, or RustFS in local dev. Reads only;
    nothing touches the disk, so run ids with ':' in them are fine on Windows. Credentials as in
    the swarm's .env: S3_ENDPOINT (empty for AWS), S3_BUCKET, STORAGE_ACCESS_KEY, STORAGE_SECRET_KEY."""

    def __init__(self, bucket: str, endpoint: str | None = None, access_key: str | None = None,
                 secret_key: str | None = None, region: str | None = None) -> None:
        import boto3   # only needed when reading from a bucket
        self.bucket = bucket
        self.s3 = boto3.client("s3", endpoint_url=endpoint or None, aws_access_key_id=access_key,
                               aws_secret_access_key=secret_key, region_name=region or "us-east-1")

    @classmethod
    def from_env(cls, env: dict) -> "S3Store":
        return cls(env["S3_BUCKET"], env.get("S3_ENDPOINT"), env.get("STORAGE_ACCESS_KEY"),
                   env.get("STORAGE_SECRET_KEY"), env.get("AWS_REGION"))

    def manifests(self):
        keys = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix="runs/"):
            keys += [o["Key"] for o in page.get("Contents", []) if o["Key"].endswith("/_manifest.json")
                     and o["Key"].count("/") == 3]
        for key in sorted(keys):
            yield key.rsplit("/", 1)[0], json.loads(self._get(key))

    def lines(self, run_key, rel):
        for line in self._get(f"{run_key}/{rel}").splitlines():
            if line.strip():
                yield json.loads(line)

    def _get(self, key: str) -> str:
        return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read().decode("utf-8")


class ZipStore:
    """A zip of the bucket's runs/ folder, read in place: unzipping on Windows fails on ':' names."""

    def __init__(self, path: Path) -> None:
        import zipfile
        self.zip = zipfile.ZipFile(path)
        names = self.zip.namelist()
        # the zip may hold runs/ at its root or inside one top folder
        self.prefix = next((n[: n.index("runs/")] for n in names if "runs/" in n), "")

    def manifests(self):
        for name in sorted(self.zip.namelist()):
            rel = name[len(self.prefix):]
            if rel.startswith("runs/") and rel.endswith("/_manifest.json") and rel.count("/") == 3:
                yield name.rsplit("/", 1)[0], json.loads(self.zip.read(name).decode("utf-8"))

    def lines(self, run_key, rel):
        for line in self.zip.read(f"{run_key}/{rel}").decode("utf-8").splitlines():
            if line.strip():
                yield json.loads(line)


def open_store(source: str, env: dict | None = None) -> Store:
    """'s3' (bucket from env), a .zip of runs/, or a folder holding runs/."""
    if source == "s3":
        return S3Store.from_env(env or {})
    if source.endswith(".zip"):
        return ZipStore(Path(source))
    return LocalStore(Path(source))


@dataclass
class State:
    items: dict[str, dict] = field(default_factory=dict)          # live items by id, ACL already current
    groups: dict[str, dict] = field(default_factory=dict)
    clusters: dict[str, dict] = field(default_factory=dict)       # person_id -> identity record
    person_of: dict[str, str] = field(default_factory=dict)       # source identity id -> person_id
    extractions: dict[str, dict] = field(default_factory=dict)    # item_id -> latest extraction
    runs: list[str] = field(default_factory=list)                 # run ids applied, in order
    skipped_runs: list[str] = field(default_factory=list)         # manifests that are not complete
    tombstoned: dict[str, tuple[str, str]] = field(default_factory=dict)  # id -> (reason, detected_at)
    counts: Counter = field(default_factory=Counter)              # what happened, for the report
    problems: Counter = field(default_factory=Counter)
    as_of: str = ""                                               # latest finished_at seen


def _run_order(manifest: dict) -> tuple:
    """Rule 1. Run ids start with an ISO time; identity goes after the same night's sources."""
    rid = manifest["run_id"]
    return (rid[:10], manifest.get("source") == "identity", rid)


def _newer(a: dict, b: dict) -> bool:
    """Rule 2: is item record `a` newer than `b`?"""
    key = lambda r: (r.get("updated_at") or "", r.get("fetched_at") or "")
    return key(a) > key(b)


def load(store: Store) -> State:
    state = State()
    runs = []
    for key, manifest in store.manifests():
        if manifest.get("status") != "complete":
            state.skipped_runs.append(manifest.get("run_id", key))
            continue
        if str(manifest.get("schema_version")) != SCHEMA_VERSION:
            state.problems[f"run {manifest.get('run_id')}: schema_version {manifest.get('schema_version')}"] += 1
            continue
        runs.append((key, manifest))
    runs.sort(key=lambda kv: _run_order(kv[1]))

    acl_seen: dict[str, str] = {}   # id -> timestamp of the ACL now held (rule 3)
    for key, manifest in runs:
        state.runs.append(manifest["run_id"])
        state.as_of = max(state.as_of, manifest.get("finished_at") or "")
        for rel in manifest.get("files", []):
            for rec in store.lines(key, rel):
                apply(state, rec, acl_seen)
    return state


def apply(state: State, rec: dict, acl_seen: dict[str, str]) -> None:
    kind = rec.get("record_type")
    state.counts[f"{kind} read"] += 1

    if kind == "item":
        rid = rec["id"]
        if rid in state.tombstoned:          # a later run may bring it back; a newer record wins
            if (rec.get("updated_at") or "") <= state.tombstoned[rid][1]:
                return
            state.tombstoned.pop(rid)
        old = state.items.get(rid)
        if old is None or _newer(rec, old):
            if old is not None and old.get("version") != rec.get("version"):
                state.counts["item superseded by a newer version"] += 1
            stamp = rec.get("fetched_at") or ""
            if old is not None and acl_seen.get(rid, "") > stamp:
                rec = {**rec, "acl": old["acl"]}   # an acl_update newer than this fetch still stands
            else:
                acl_seen[rid] = stamp
            state.items[rid] = rec

    elif kind == "acl_update":
        rid = rec["id"]
        if rid not in state.items:
            state.counts["acl_update for an unknown id (no-op)"] += 1
            return
        stamp = rec.get("observed_at") or ""
        if stamp >= acl_seen.get(rid, ""):
            state.items[rid] = {**state.items[rid], "acl": rec["acl"]}
            acl_seen[rid] = stamp
            state.counts["acl updated"] += 1

    elif kind == "tombstone":
        rid = rec["id"]
        if rid not in state.items:
            state.counts["tombstone for an unknown id (no-op)"] += 1
            return
        state.tombstoned[rid] = (rec.get("reason", ""), rec.get("detected_at") or "")
        state.items.pop(rid)
        state.extractions.pop(rid, None)      # rule 5
        state.counts[f"tombstoned ({rec.get('reason')})"] += 1

    elif kind == "group":
        old = state.groups.get(rec["id"])
        if old is None or (rec.get("observed_at") or "") >= (old.get("observed_at") or ""):
            state.groups[rec["id"]] = rec

    elif kind == "identity":
        pid = rec["person_id"]
        if rec.get("merged_into"):
            state.clusters.pop(pid, None)
            state.counts["identity cluster merged away"] += 1
            return
        state.clusters[pid] = rec
        for member in rec.get("members", []):
            state.person_of[member["id"]] = pid       # rule 6: the latest record listing it

    elif kind == "extraction":
        iid = rec["item_id"]
        old = state.extractions.get(iid)
        # One per item: the one for the item's current version if there is one, else the newest.
        state.extractions[iid] = rec if old is None or rec.get("item_version") != old.get("item_version") else old

    else:
        state.problems[f"unknown record_type {kind!r}"] += 1


def current_extractions(state: State) -> tuple[list[dict], int]:
    """Extractions whose item is live. The second value counts the ones made from an older
    version of the item than the one now held (still used, flagged as stale)."""
    out, stale = [], 0
    for iid, ex in state.extractions.items():
        item = state.items.get(iid)
        if item is None:
            continue
        if ex.get("item_version") != item.get("version"):
            stale += 1
        out.append(ex)
    return out, stale
