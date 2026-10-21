"""
ontology.py - the closed vocabulary, loaded from ontology.yaml.

The file is the single source: extraction prompts render it, and the record
validators in records.py check against it. Nothing here hardcodes a type.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

DEFAULT_PATH = Path(__file__).resolve().parents[1] / "ontology.yaml"


@dataclass(frozen=True)
class Ontology:
    version: int
    entities: frozenset[str]
    item_backed: frozenset[str]
    llm_entities: frozenset[str]
    relations: frozenset[str]
    llm_relations: frozenset[str]
    deterministic_relations: frozenset[str]
    person_roles: frozenset[str]


def _llm(spec: dict) -> bool:
    return spec.get("origin") in {"llm", "both"}


def _deterministic(spec: dict) -> bool:
    return spec.get("origin") in {"deterministic", "both"}


@lru_cache(maxsize=1)
def load(path: str | None = None) -> Ontology:
    """Read ontology.yaml once. SWARM_ONTOLOGY overrides the path, which is how
    the container image points at its copy."""
    source = Path(path or os.environ.get("SWARM_ONTOLOGY") or DEFAULT_PATH)
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    entities = data["entities"]
    relations = data["relations"]
    return Ontology(
        version=int(data["version"]),
        entities=frozenset(entities),
        item_backed=frozenset(k for k, v in entities.items() if v.get("item_backed")),
        llm_entities=frozenset(k for k, v in entities.items() if _llm(v) and not v.get("item_backed")),
        relations=frozenset(relations),
        llm_relations=frozenset(k for k, v in relations.items() if _llm(v)),
        deterministic_relations=frozenset(k for k, v in relations.items() if _deterministic(v)),
        person_roles=frozenset(data["person_roles"]),
    )
