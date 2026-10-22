"""
cli.py - what every pipeline's main.py shares: logging, settings, the store,
and the one-line JSON summary a run prints.
"""

from __future__ import annotations

import json
import logging
import os

from .store import LocalStore, ObjectStore, S3Store


def setup_logging() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s %(message)s")
    # httpx logs every request URL at INFO; swarm.http already logs retries.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def listed(name: str) -> list[str]:
    """A comma-separated setting as a list, blanks dropped."""
    return [v.strip() for v in os.environ.get(name, "").split(",") if v.strip()]


def backfill_days() -> int:
    return int(os.environ.get("SWARM_BACKFILL_DAYS", "730"))


def llm() -> dict:
    """run_source's LLM settings from the environment: the model
    (swarm/model.py) and SWARM_LLM_TOKEN_BUDGET, the run's token budget
    (unset means no limit)."""
    from .model import from_env

    budget = os.environ.get("SWARM_LLM_TOKEN_BUDGET", "").strip()
    return {"model": from_env(), "llm_budget": int(budget) if budget else None}


def store_for(local: str | None) -> ObjectStore:
    return LocalStore(local) if local else S3Store()


def report(manifest, checks) -> None:
    print(json.dumps({"run_id": manifest.run_id, "counts": manifest.counts, "checks": checks}))
