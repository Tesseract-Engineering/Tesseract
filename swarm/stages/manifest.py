"""
manifest.py - the last two stages of every run.

  10. write the manifest, after every part file is flushed
  11. advance checkpoints and seen-state, after the manifest exists

The order is the whole point. The graph side reads a run only once its
manifest exists, so a run that dies before step 10 is invisible and is
simply retried; its partial files are overwritten. A run that dies between
10 and 11 is visible and complete, and the next run redoes its window, which
the graph side absorbs because records are keyed by id and version.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from ..interfaces import Checkpoint
from ..records import Manifest, Tokens
from .write import RunWriter, state_key


def read_checkpoints(writer: RunWriter) -> dict[str, Checkpoint]:
    raw = writer.store.get(state_key(writer.source, "checkpoints"))
    data = json.loads(raw) if raw else {}
    return {k: Checkpoint(**v) for k, v in data.items()}


def finish_run(writer: RunWriter, *, started_at: datetime,
               checkpoints: dict[str, Checkpoint] | None = None,
               extractor_versions: list[str] | None = None,
               llm_budget_hit: bool = False, tokens: Tokens | None = None,
               finished_at: datetime | None = None, published: dict[str, object] | None = None,
               acl_coverage: float = 1.0) -> Manifest:
    """`published` is lookup state other sources may read, written as
    state/{source}/{name}.json alongside the checkpoints."""
    writer.flush()
    manifest = Manifest(
        source=writer.source, run_id=writer.run_id,
        started_at=started_at, finished_at=finished_at or datetime.now(timezone.utc),
        counts=dict(writer.counts), rejections=dict(sorted(writer.rejections.items())),
        acl_coverage=acl_coverage, files=sorted(writer.files),
        extractor_versions=sorted(set(extractor_versions or [])),
        llm_budget_hit=llm_budget_hit, tokens=tokens or Tokens(),
    )
    writer.store.put(writer.prefix + "_manifest.json", manifest.model_dump_json(indent=2).encode("utf-8"))

    # Only now does state move.
    store, source = writer.store, writer.source
    store.put(state_key(source, "seen"), json.dumps(writer.seen_state(), sort_keys=True).encode("utf-8"))
    store.put(state_key(source, "backlog"), json.dumps(writer.backlog, sort_keys=True).encode("utf-8"))
    if checkpoints:
        merged = {k: vars(v) for k, v in read_checkpoints(writer).items()}
        merged.update({k: vars(v) for k, v in checkpoints.items()})
        store.put(state_key(source, "checkpoints"), json.dumps(merged, sort_keys=True, indent=2).encode("utf-8"))
    for name, value in (published or {}).items():
        store.put(state_key(source, name), json.dumps(value, sort_keys=True).encode("utf-8"))
    return manifest


def read_manifest(store, source: str, run_id: str) -> Manifest | None:
    """What the graph side does first: no manifest, no run."""
    raw = store.get(f"runs/{source}/{run_id}/_manifest.json")
    return Manifest.model_validate_json(raw) if raw else None
