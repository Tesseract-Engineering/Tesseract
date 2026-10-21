"""
schema.py - the record contract as JSON Schema, for the graph side.

    python -m swarm.schema            write swarm/schemas/*.json
    python -m swarm.schema --check    exit 1 if the committed files are stale

The ontology's type lists are copied into the schemas as enums, so a reader
without Python still sees the closed vocabulary.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from . import ontology
from .records import (
    AclUpdateRecord,
    ExtractionRecord,
    IdentityRecord,
    ItemRecord,
    Manifest,
    TombstoneRecord,
)

OUT = Path(__file__).resolve().parent / "schemas"

MODELS = {
    "item": ItemRecord,
    "extraction": ExtractionRecord,
    "tombstone": TombstoneRecord,
    "acl_update": AclUpdateRecord,
    "identity": IdentityRecord,
    "manifest": Manifest,
}


def _with_vocabulary(name: str, schema: dict[str, Any]) -> dict[str, Any]:
    onto = ontology.load()
    defs = schema.get("$defs", {})
    enums = {
        ("Ref", "type"): onto.deterministic_relations,
        ("Person", "roles"): onto.person_roles,
        ("Entity", "type"): onto.llm_entities,
        ("Relation", "type"): onto.llm_relations,
    }
    for (model, field), values in enums.items():
        prop = defs.get(model, {}).get("properties", {}).get(field)
        if prop is None:
            continue
        target = prop["items"] if prop.get("type") == "array" else prop
        target["enum"] = sorted(values)
    schema["$id"] = f"{name}.json"
    schema["x-ontology-version"] = onto.version
    return schema


def render() -> dict[str, str]:
    return {name: json.dumps(_with_vocabulary(name, model.model_json_schema()), indent=2, sort_keys=True) + "\n"
            for name, model in MODELS.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="fail if swarm/schemas is out of date")
    args = parser.parse_args(argv)

    stale = []
    for name, text in render().items():
        path = OUT / f"{name}.json"
        if args.check:
            if not path.is_file() or path.read_text(encoding="utf-8") != text:
                stale.append(path.name)
        else:
            OUT.mkdir(exist_ok=True)
            path.write_text(text, encoding="utf-8")
    if stale:
        print(f"stale schemas: {', '.join(stale)}; run python -m swarm.schema", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
