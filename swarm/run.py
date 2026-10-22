"""
run.py - one run of one source, the same stages for every source.

     1-2  enumerate containers, fetch items changed since each checkpoint
     3-4  redact each payload in memory, then keep it under raw/
     5-7  normalize to an item record, with its ACL and deterministic refs
       9  write records (and the source's group records)
          check the run: ACL coverage 100%, zero secret hits
   10-11  write the manifest, then advance checkpoints (and any lookup state
          the connector publishes for other sources)

Step 8, LLM extraction, arrives in Phase 3.

An item that fails to normalize or carries no ACL is counted as rejected and
the run goes on. A source or storage failure ends the run before the
manifest, so the run stays invisible and is retried.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from pydantic import ValidationError

from .interfaces import Checkpoint, Connector, Normalizer
from .records import Manifest
from .stages.acl import MissingAcl, require_acl
from .stages.checks import check_run
from .stages.manifest import finish_run, read_checkpoints
from .stages.redact import redact_payload
from .stages.write import RunWriter, new_run_id
from .store import ObjectStore

log = logging.getLogger("swarm.run")
_SAFE = re.compile(r"[^A-Za-z0-9._-]")


def run_source(connector: Connector, normalizer: Normalizer, store: ObjectStore, *,
               now: datetime | None = None, run_id: str | None = None) -> tuple[Manifest, dict]:
    started = now or datetime.now(timezone.utc)
    source = connector.source
    writer = RunWriter(store, source, run_id or new_run_id(source, started))
    normalizer.run_id = writer.run_id
    previous = read_checkpoints(writer)
    advanced: dict[str, Checkpoint] = {}
    groups = getattr(connector, "groups", None)
    checkpoint = getattr(connector, "checkpoint", None)

    for container in connector.enumerate():
        for raw in connector.fetch(container, previous.get(container.id)):
            raw.payload, _ = redact_payload(raw.payload)
            writer.write_raw(_SAFE.sub("_", raw.native_id), raw.payload)
            try:
                acl = require_acl(raw.native_id, connector.acl(raw))
                item = normalizer.to_item(raw, acl)
            except (MissingAcl, ValidationError, KeyError, ValueError, TypeError) as e:
                # Counted, never logged with content: the reason is the exception type.
                log.warning("rejected %s: %s", raw.native_id, type(e).__name__)
                writer.reject(f"{type(e).__name__}")
                continue
            writer.add_item(item)
        if groups is not None:
            for group in groups(container, run_id=writer.run_id):
                writer.add(group)
        if checkpoint is not None and (cp := checkpoint(container)) is not None:
            advanced[container.id] = cp

    writer.flush()
    checks = check_run(store, source, writer.run_id)         # raises before any manifest exists
    published = getattr(connector, "published_state", None)
    manifest = finish_run(writer, started_at=started, checkpoints=advanced,
                          published=published() if published is not None else None)
    return manifest, checks
