"""
core_link.py - the desk as the Tesseract core's ticketing surface (core/README.md, "For the ticketing bot").

    Slack "Make a ticket" ──▶ core approval (the thread's owner approves the brief by its hash)
                                   │  GET /approvals?surface=tickets&status=approved, every few seconds
                                   ▼
    the desk: a ticket TD-2xx from the brief ──▶ POST /approvals/{id}/done {url, text}
                                                   (the core logs it on the thread, the call screen
                                                    shows it, the Slack bot DMs the owner the link)
    approved reply / triage note / draft PR ──▶ an entry on the thread
    ticket resolved ──▶ the thread closes

Only tickets made from a thread talk to the core; the seed tickets never do. The desk never reads the
graph through the core either: its reads stay on forge over MCP. The key is the core's `tickets` key,
from TESSERACT_TICKETS_KEY or data/core-keys.json; with neither, the desk runs on its own.
"""

from __future__ import annotations

import asyncio
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class CoreError(Exception):
    pass


class CoreLink:
    def __init__(self, url: str, key: str):
        self.url, self.key = url.rstrip("/"), key

    @classmethod
    def from_env(cls) -> CoreLink | None:
        key = os.environ.get("TESSERACT_TICKETS_KEY", "").strip()
        if not key:
            keys = Path(os.environ.get("TESSERACT_KEYS", ROOT / "data" / "core-keys.json"))
            if keys.exists():
                key = json.loads(keys.read_text(encoding="utf-8")).get("tickets", "")
        if not key:
            return None
        return cls(os.environ.get("TESSERACT_URL", "http://127.0.0.1:8787"), key)

    def _call(self, path: str, body: dict | None = None):
        req = urllib.request.Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-Tesseract-Key": self.key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                why = json.loads(e.read()).get("error")
            except ValueError:
                why = None
            raise CoreError(f"core said {e.code}: {why or e.reason}") from e
        except (urllib.error.URLError, OSError) as e:
            raise CoreError(f"core unreachable at {self.url}: {e}") from e

    async def call(self, path: str, body: dict | None = None):
        return await asyncio.to_thread(self._call, path, body)

    async def approved(self) -> list[dict]:
        """Tickets a thread's owner approved, waiting for the desk."""
        return await self.call("/approvals?surface=tickets&status=approved")

    async def done(self, approval: str, url: str | None, text: str, ok: bool = True) -> dict:
        return await self.call(f"/approvals/{approval}/done", {"result": {"url": url, "text": text}, "ok": ok})

    async def entry(self, thread: str, by: str, kind: str, **data) -> dict:
        return await self.call(f"/threads/{thread}/entries", {"by": by, "kind": kind, **data})

    async def close(self, thread: str, by: str) -> dict:
        return await self.call(f"/threads/{thread}/close", {"by": by})


def ticket_from(approval: dict, tid: str, now: str) -> dict:
    """A desk ticket from an approved brief: the thread's owner is the requester, the brief is the
    body, and the thread and approval come along so the desk can report back."""
    p = approval["payload"]
    owner = p.get("owner") or approval.get("approved_by") or approval["by"]
    name = " ".join(w.capitalize() for w in owner.split("@")[0].replace(".tesseract", "").split(".") if w)
    return {"id": tid, "customer": "Internal (Tesseract thread)", "reporter": {"name": name or owner, "email": owner},
            "service": "from a thread", "severity": "medium", "created_at": now,
            "subject": p.get("summary") or "Ticket from a thread", "body": p.get("description") or "",
            "thread": approval.get("thread_id"), "approval": approval["id"], "hash": approval["hash"]}
