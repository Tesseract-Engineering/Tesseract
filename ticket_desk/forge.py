"""
forge.py - the ticket desk's only way to the graph: an MCP client to forge.

Until forge's server ships (F8), the server is forge_stub, started over stdio with the same
contract; when F8 merges, FORGE_COMMAND points at `python -m forge serve --stdio` and nothing else
changes. The desk never imports graph/ or forge.

A Session remembers every node a tool returned during one agent run, with the call that found it,
so a citation can be checked (a model may only cite what the graph actually showed it) and the
provenance panel can say where each claim came from.
"""

from __future__ import annotations

import os
import shlex
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parent.parent
VIEWER = os.environ.get("TICKET_DESK_VIEWER", "ishan.tesseract@gmail.com")
NS = os.environ.get("TICKET_DESK_NS", "kg:sandbox")


def server_params() -> StdioServerParameters:
    command = shlex.split(os.environ.get("FORGE_COMMAND", f"{sys.executable} -m forge_stub serve --stdio"))
    env = {**os.environ, "FORGE_VIEWER_EMAIL": VIEWER, "FORGE_GRAPH_NS": NS,
           "PYTHONPATH": os.pathsep.join(p for p in (str(ROOT), os.environ.get("PYTHONPATH", "")) if p)}
    return StdioServerParameters(command=command[0], args=command[1:], env=env, cwd=str(ROOT))


class Forge:
    """One MCP connection, opened for the app's lifetime and shared by every run."""

    def __init__(self, target: Any = None):
        self.target = target if target is not None else server_params()
        self._stack: AsyncExitStack | None = None
        self.client: Client | None = None
        self.tools: list[dict] = []

    async def __aenter__(self) -> "Forge":
        self._stack = AsyncExitStack()
        self.client = await self._stack.enter_async_context(Client(self.target))
        listed = await self.client.list_tools()
        self.tools = [{"name": t.name, "description": t.description or "", "input_schema": t.input_schema}
                      for t in listed.tools]
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._stack:
            await self._stack.aclose()

    async def call(self, name: str, args: dict) -> tuple[str, dict, bool]:
        """(text for the model, structured result, is_error)."""
        result = await self.client.call_tool(name, args)
        text = "\n".join(getattr(c, "text", "") for c in result.content)
        return text, result.structured_content or {}, bool(result.is_error)


@dataclass
class Seen:
    """A node the graph returned in this run: enough to show and link it, and how it was found."""
    id: str
    title: str
    labels: list[str]
    source: str | None
    url: str | None
    key: str | None = None
    kind: str | None = None
    status: str | None = None
    updated: str | None = None
    snippet: str | None = None
    found_by: list[int] = field(default_factory=list)       # tool call numbers

    def public(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Session:
    """The tool calls of one agent run, and every node they returned."""
    forge: Forge
    calls: list[dict] = field(default_factory=list)
    seen: dict[str, Seen] = field(default_factory=dict)

    async def call(self, name: str, args: dict) -> tuple[str, dict, bool]:
        text, data, is_error = await self.forge.call(name, args)
        n = len(self.calls) + 1
        found = [] if is_error else self._remember(n, data)
        for nid in found:
            # read_content names its node only by id; look the rest up so it can be shown and linked.
            if self.seen[nid].title == nid:
                _, entity, missing = await self.forge.call("get_entity", {"id_or_ref": nid})
                if not missing:
                    self._remember(n, {"hits": [entity["entity"]]})
        self.calls.append({"n": n, "name": name, "input": args, "is_error": is_error,
                           "error": (data.get("error") or {}).get("code") if is_error else None,
                           "nodes": found})
        return text, data, is_error

    def _remember(self, n: int, data: dict) -> list[str]:
        nodes = list(data.get("hits") or []) + list(data.get("nodes") or [])
        if data.get("entity"):
            nodes.append(data["entity"])
        prov = data.get("provenance") or {}
        if prov.get("node_id") and not data.get("entity"):
            # read_content and get_neighbors are about one node; keep it even if not in the list.
            nodes.append({"id": prov["node_id"], "url": prov.get("url")})
        ids = []
        for node in nodes:
            nid = node.get("id")
            if not nid or "Account" in (node.get("labels") or []) or "Group" in (node.get("labels") or []):
                continue
            s = self.seen.get(nid)
            if s is None:
                s = self.seen[nid] = Seen(id=nid, title="", labels=[], source=None, url=None)
            if not s.title or s.title == nid:
                s.title = node.get("title") or node.get("name") or nid
            s.labels = s.labels or node.get("labels") or []
            s.source = s.source or node.get("source")
            s.url = s.url or node.get("url")
            s.key = s.key or node.get("key")
            s.kind = s.kind or node.get("kind")
            s.status = s.status or node.get("status") or node.get("state")
            s.updated = s.updated or node.get("sourceUpdatedAt")
            snippet = node.get("snippet") or (node.get("text") or node.get("body") or "")[:300]
            s.snippet = s.snippet or snippet or None
            if n not in s.found_by:
                s.found_by.append(n)
            ids.append(nid)
        return ids
