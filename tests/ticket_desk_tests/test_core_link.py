"""The desk as the core's ticketing surface: an approved brief becomes a ticket, and the thread hears back."""

from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

import anyio
import pytest

pytest.importorskip("neo4j")
pytest.importorskip("starlette")
pytest.importorskip("mcp")

from core.api import handler  # noqa: E402
from core.ledger import Ledger  # noqa: E402
from core.service import Core  # noqa: E402
from core.threads import Threads  # noqa: E402
from ticket_desk import app  # noqa: E402
from ticket_desk.core_link import CoreLink  # noqa: E402

ISHAN, KARTHIK = "ishan.tesseract@gmail.com", "karthik.tesseract@gmail.com"
KEYS = {"zoom": "z" * 24, "docs": "d" * 24, "slack": "s" * 24, "tickets": "t" * 24}


@pytest.fixture
def core(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(app, "VAR", tmp_path)
    c = Core(store=object(), threads=Threads(tmp_path / "t.db"), ledger=Ledger(tmp_path / "l.db"), model=False)
    c.shared_records = lambda t: []
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(c, KEYS))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield c, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def test_an_approved_brief_becomes_a_ticket_and_the_thread_hears_back(core):
    c, url = core
    t = c.open_thread(ISHAN, "Per-model shadow gate thresholds", [KARTHIK])
    c.append(t["id"], ISHAN, "zoom", "decision", text="Threshold becomes per-model config", proposed=False)
    a = c.propose_ticket(t["id"], KARTHIK)

    async def main():
        desk = app.Desk(forge=None, link=CoreLink(url, KEYS["tickets"]))
        assert await desk.sync() == []                          # proposed is not approved
        c.approve(a["id"], ISHAN, a["hash"])
        assert await desk.sync() == ["TD-201"]
        assert await desk.sync() == []                          # once
        ticket = desk.tickets["TD-201"]
        assert ticket["thread"] == t["id"] and ticket["reporter"]["email"] == ISHAN
        assert "Threshold becomes per-model config" in ticket["body"]

        pid = "TD-201-triage-1"
        desk.proposals[pid] = {"id": pid, "ticket": "TD-201", "kind": "triage", "status": "pending",
                               "created": "now", "content": {}, "sources": {}}
        await desk.decide(pid, "approve", None)
        await desk.update("TD-201", {"status": "Resolved"})
        return desk

    desk = anyio.run(main)
    assert c.threads.approval(a["id"])["status"] == "done"
    trail = c.threads.get(t["id"], ISHAN)
    done = next(e for e in trail["entries"] if e["kind"] == "action")
    assert done["surface"] == "tickets" and done["url"].endswith("#/t/TD-201") and done["approval"] == a["hash"]
    notes = [e["text"] for e in trail["entries"] if e["kind"] == "note"]
    assert any("Posted to the Platform triage log" in n for n in notes) and "TD-201 resolved" in notes
    assert trail["status"] == "closed"
    assert app.Desk(forge=None).tickets["TD-201"]["subject"] == "Per-model shadow gate thresholds"   # kept
    assert len(desk.linked) == 1


def test_seed_tickets_never_talk_to_the_core(core):
    c, url = core

    async def main():
        desk = app.Desk(forge=None, link=CoreLink(url, "wrong-key"))
        await desk.update("TD-101", {"status": "Resolved"})     # no thread: nothing is sent
        return desk

    desk = anyio.run(main)
    assert not any("thread" in entry["what"] for entry in desk.log)
