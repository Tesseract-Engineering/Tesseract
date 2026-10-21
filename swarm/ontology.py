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
    # relation -> (entity types it may start at, entity types it may end at)
    endpoints: dict[str, tuple[frozenset[str], frozenset[str]]]
    # person role -> the relation it means
    person_roles: dict[str, str]

    def allows(self, relation: str, source_types: set[str], target_types: set[str]) -> bool:
        """True when some reading of the two endpoints fits the relation. An
        endpoint can have several possible types: a Drive id is a document or
        a folder."""
        allowed_from, allowed_to = self.endpoints[relation]
        return bool(source_types & allowed_from) and bool(target_types & allowed_to)


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
    endpoints = {k: (frozenset(v["from"]), frozenset(v["to"])) for k, v in relations.items()}
    for name, (allowed_from, allowed_to) in endpoints.items():
        unknown = (allowed_from | allowed_to) - set(entities)
        if unknown:
            raise ValueError(f"ontology.yaml: relation {name} names unknown entity types {sorted(unknown)}")
    roles = dict(data["person_roles"])
    unknown_roles = set(roles.values()) - set(relations)
    if unknown_roles:
        raise ValueError(f"ontology.yaml: person roles map to unknown relations {sorted(unknown_roles)}")
    return Ontology(
        version=int(data["version"]),
        entities=frozenset(entities),
        item_backed=frozenset(k for k, v in entities.items() if v.get("item_backed")),
        llm_entities=frozenset(k for k, v in entities.items() if _llm(v) and not v.get("item_backed")),
        relations=frozenset(relations),
        llm_relations=frozenset(k for k, v in relations.items() if _llm(v)),
        deterministic_relations=frozenset(k for k, v in relations.items() if _deterministic(v)),
        endpoints=endpoints,
        person_roles=roles,
    )
