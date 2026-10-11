"""
adapters/generic.py - the fallback for data no adapter recognises.

Reads every document it can (.md .rst .txt .docx .pdf) and nothing else: no
people, no services, no tickets. That is deliberately thin. Guessing that a
JSON file is a ticket, or that a folder name is a team, is exactly the silent
interpretation an adapter is supposed to make explicitly and reviewably.

What it is for: pointing the builder at new data on day one, getting
Document nodes, and reading the coverage report - which lists every folder of
files it did NOT read, i.e. the work a real adapter for this company needs.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .. import connectors
from ..bundle import Bundle

NAME = "generic"
SUFFIXES = (".md", ".rst", ".txt", ".docx", ".pdf")


def looks_like(root: Path) -> bool:
    return root.is_dir()


def load(root: Path) -> Bundle:
    def report(path: Path, exc: Exception) -> None:
        print(f"  could not read {path.relative_to(root)}: {exc}", file=sys.stderr)

    bundle = Bundle(name=NAME, root=root)
    bundle.records.extend(connectors.documents(root, suffixes=SUFFIXES, on_error=report))
    return bundle
