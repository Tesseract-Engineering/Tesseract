"""
adapters - one module per data layout. See bundle.py for the contract.

An adapter module provides:

    NAME: str
    looks_like(root: Path) -> bool     cheap check used by --adapter auto
    load(root: Path) -> bundle.Bundle

    workspace   the standard Tesseract export layout (slack/, jira/, sharepoint/,
                outlook/, teams/, docs/, people/directory.json)
    generic     the fallback: every readable document under the root, no people
                or tickets, plus a coverage report of what a real adapter would
                need to read

To support a company whose exports are laid out differently, write a module
like workspace.py that points the connectors at its files, and register it
below. Anything the company does not have (no service catalog, no org chart)
is left empty in the Bundle; the builder copes with absence.
"""

from __future__ import annotations

import importlib
from pathlib import Path

# Order matters for auto-detection: specific layouts first, generic last.
REGISTRY = ["workspace", "generic"]


def get(name: str):
    if name not in REGISTRY:
        raise SystemExit(f"Unknown adapter {name!r}. Known: {', '.join(REGISTRY)}")
    return importlib.import_module(f"{__name__}.{name}")


def detect(root: Path):
    for name in REGISTRY:
        module = get(name)
        if module.looks_like(root):
            return module
    raise SystemExit(f"No adapter recognises {root}")
