"""
checks.py - the hard-fail checks every run must pass (docs/SWARM.md,
"Evaluation and cost controls").

  ACL coverage     every written item carries an acl          (100%)
  Secret leakage   no secret pattern anywhere in the run      (zero hits)
"""

from __future__ import annotations

from ..records import ItemRecord, parse_line
from ..store import ObjectStore
from .redact import redact


class CheckFailed(RuntimeError):
    pass


def check_run(store: ObjectStore, source: str, run_id: str) -> dict[str, float | int]:
    prefix = f"runs/{source}/{run_id}/"
    items = with_acl = hits = 0
    for key in store.list(prefix):
        if not key.endswith(".jsonl"):
            continue
        for line in (store.get(key) or b"").decode("utf-8").splitlines():
            _, found = redact(line)
            hits += sum(r.count for r in found)
            record = parse_line(line)
            if isinstance(record, ItemRecord):
                items += 1
                with_acl += record.acl is not None
    result = {"items": items, "acl_coverage": 1.0 if items == 0 else with_acl / items, "secret_hits": hits}
    if result["acl_coverage"] < 1.0 or hits:
        raise CheckFailed(f"{prefix}: {result}")
    return result
