"""jsonpath.py - the small JSONPath subset that provider specs may use.

    $               the document
    .name  ['name'] a key (the quoted form allows any characters)
    [0]   [-1]      a list index
    [*]             every element of a list (or every value of a dict)

Nothing else: no filters, slices, recursive descent or scripts. A spec field
either parses under this grammar or the static validator rejects the spec, so
what a spec can reach is easy to audit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

WILDCARD = object()

_STEP = re.compile(r"""
    \.(?P<name>[A-Za-z_][A-Za-z0-9_\-]*)
  | \[(?P<index>-?\d+)\]
  | \[(?P<star>\*)\]
  | \['(?P<quoted>[^'\\]*)'\]
""", re.VERBOSE)


@dataclass(frozen=True)
class Path:
    text: str
    steps: tuple  # str key | int index | WILDCARD

    def find(self, doc: Any) -> list:
        """Every match, in document order. A missing key or index matches nothing."""
        nodes = [doc]
        for step in self.steps:
            out = []
            for node in nodes:
                if step is WILDCARD:
                    if isinstance(node, list):
                        out.extend(node)
                    elif isinstance(node, dict):
                        out.extend(node.values())
                elif isinstance(step, int):
                    if isinstance(node, list) and -len(node) <= step < len(node):
                        out.append(node[step])
                elif isinstance(node, dict) and step in node:
                    out.append(node[step])
            nodes = out
        return nodes

    def first(self, doc: Any, default: Any = None) -> Any:
        found = self.find(doc)
        return found[0] if found else default


@lru_cache(maxsize=1024)
def parse(text: str) -> Path:
    """Parse a path, or raise ValueError naming the offset that does not parse."""
    if not text.startswith("$"):
        raise ValueError(f"JSONPath must start with '$': {text!r}")
    steps, pos = [], 1
    while pos < len(text):
        m = _STEP.match(text, pos)
        if not m:
            raise ValueError(f"JSONPath {text!r} does not parse at offset {pos}")
        if m["name"] is not None:
            steps.append(m["name"])
        elif m["index"] is not None:
            steps.append(int(m["index"]))
        elif m["star"]:
            steps.append(WILDCARD)
        else:
            steps.append(m["quoted"])
        pos = m.end()
    return Path(text, tuple(steps))


def find(text: str, doc: Any) -> list:
    return parse(text).find(doc)


def first(text: str, doc: Any, default: Any = None) -> Any:
    return parse(text).first(doc, default)
