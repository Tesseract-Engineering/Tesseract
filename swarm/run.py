"""
run.py - one run of one source, the same stages for every source.

     1-2  enumerate containers, fetch items changed since each checkpoint
     3-4  redact each payload in memory, then keep it under raw/
     5-7  normalize to an item record, with its ACL and deterministic refs;
          then the items earlier runs left with unresolved links, resolved
          again against what the other sources have published since
       8  LLM extraction, when a model is given: the backlog first, then
          every item this run writes or sees again (stages/extract.py); with
          batches, sent as one Message Batch once fetching is done. With no
          model, those items wait in the backlog for a run with one
       9  write records (and the source's group records)
          check the run: every written item has an ACL, zero secret hits
   10-11  write the manifest, then advance checkpoints (and any lookup state
          the connector publishes for other sources)

An item that fails to normalize or carries no ACL is counted as rejected and
the run goes on; the manifest records how many per reason, and the share of
fetched items that had an ACL. A source or storage failure ends the run before the
manifest, so the run stays invisible and is retried.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from pydantic import ValidationError

from .interfaces import Checkpoint, Connector, Normalizer
from .model import Model
from .records import ItemRecord, Manifest
from .stages.acl import MissingAcl, coverage, require_acl
from .stages.checks import check_run
from .stages.extract import BATCH_WAIT, ExtractStage, defer
from .stages.manifest import finish_run, read_checkpoints
from .stages.redact import redact_payload
from .stages.write import RunWriter, new_run_id
from .store import ObjectStore
from .timeutil import iso

log = logging.getLogger("swarm.run")
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def run_source(connector: Connector, normalizer: Normalizer, store: ObjectStore, *,
               now: datetime | None = None, run_id: str | None = None,
               model: Model | None = None, llm_budget: int | None = None,
               batch_wait: float = BATCH_WAIT) -> tuple[Manifest, dict]:
    started = now or datetime.now(timezone.utc)
    source = connector.source
    writer = RunWriter(store, source, run_id or new_run_id(source, started))
    normalizer.run_id = writer.run_id
    previous = read_checkpoints(writer)
    advanced: dict[str, Checkpoint] = {}
    groups = getattr(connector, "groups", None)
    checkpoint = getattr(connector, "checkpoint", None)
    relink = getattr(normalizer, "relink", None)
    fetched: set[str] = set()
    with_acl = 0
    extract = (ExtractStage(model, writer, budget=llm_budget, now=started, batch_wait=batch_wait)
               if model is not None else None)

    def accept(item: ItemRecord) -> None:
        writer.add_item(item)
        if extract is not None:
            extract.offer(item)
        else:
            defer(writer, item)

    if extract is not None:
        extract.work_backlog()

    for container in connector.enumerate():
        for raw in connector.fetch(container, previous.get(container.id)):
            raw.payload, _ = redact_payload(raw.payload)
            writer.write_raw(_SAFE.sub("_", raw.native_id), raw.payload)
            try:
                acl = require_acl(raw.native_id, connector.acl(raw))
                with_acl += 1
                item = normalizer.to_item(raw, acl)
            except (MissingAcl, ValidationError, KeyError, ValueError, TypeError) as e:
                # Counted, never logged with content: the reason is the exception type.
                log.warning("rejected %s: %s", raw.native_id, type(e).__name__)
                writer.reject(f"{type(e).__name__}")
                continue
            fetched.add(item.id)
            accept(item)
        if groups is not None:
            for group in groups(container, run_id=writer.run_id):
                writer.add(group)
        if checkpoint is not None and (cp := checkpoint(container)) is not None:
            advanced[container.id] = cp

    if relink is not None:
        for item_id, line in sorted(writer.unresolved.items()):
            if item_id in fetched:
                continue
            seen = writer.seen_items.get(item_id)
            if not seen or seen[0] != json.loads(line).get("content_hash"):
                del writer.unresolved[item_id]                # replaced since by a version this did not see
                continue
            stored = ItemRecord.model_validate_json(line)
            if (linked := relink(stored, run_id=writer.run_id, fetched_at=iso(started))) is not None:
                accept(linked)

    if extract is not None:
        extract.send_batch()
    writer.flush()
    checks = check_run(store, source, writer.run_id)         # raises before any manifest exists
    published = getattr(connector, "published_state", None)
    manifest = finish_run(writer, started_at=started, checkpoints=advanced,
                          acl_coverage=coverage(with_acl, writer.rejections.get("MissingAcl", 0)),
                          published=published() if published is not None else None,
                          **(extract.finish() if extract is not None else {}))
    return manifest, checks
