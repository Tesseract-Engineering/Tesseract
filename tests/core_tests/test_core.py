"""The Tesseract core: threads, approvals and the HTTP API, with no database and no model."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from datetime import timedelta
from http.server import ThreadingHTTPServer

import pytest

pytest.importorskip("neo4j")

from core.api import handler  # noqa: E402
from core.ledger import Ledger  # noqa: E402
from core.service import REVIEW_INTENT, TICKET_INTENT, Core  # noqa: E402
from core.threads import Denied, Threads, context_text, now  # noqa: E402

ISHAN, KARTHIK, HAMILTON = "ishan@x.com", "karthik@x.com", "hamilton@x.com"


@pytest.fixture
def threads(tmp_path):
    return Threads(tmp_path / "threads.db")


def test_only_the_audience_reads_a_thread(threads):
    t = threads.create(ISHAN, "Per-model thresholds", [KARTHIK])
    assert threads.get(t["id"], KARTHIK)["audience"] == [ISHAN, KARTHIK]
    with pytest.raises(Denied):
        threads.get(t["id"], HAMILTON)
    with pytest.raises(Denied):
        threads.append(t["id"], HAMILTON, "slack", "note", text="hi")
    assert [x["id"] for x in threads.mine(KARTHIK)] == [t["id"]]
    assert threads.mine(KARTHIK, owned_only=True) == []


def test_a_proposed_decision_counts_only_once_the_owner_accepts_it(threads):
    t = threads.create(ISHAN, "Thresholds", [KARTHIK])
    e = threads.append(t["id"], KARTHIK, "zoom", "decision", text="Threshold becomes per-model config")
    assert e["status"] == "proposed"
    assert "per-model" not in context_text(threads.get(t["id"], ISHAN))
    with pytest.raises(Denied):
        threads.decide(t["id"], e["id"], KARTHIK, keep=True)
    threads.decide(t["id"], e["id"], ISHAN, keep=True)
    assert "Decided: Threshold becomes per-model config" in context_text(threads.get(t["id"], ISHAN))
    with pytest.raises(Denied):                 # once
        threads.decide(t["id"], e["id"], ISHAN, keep=False)


def test_the_owner_approves_the_exact_payload_and_the_surface_reports_it(threads):
    t = threads.create(ISHAN, "Thresholds", [KARTHIK])
    a = threads.propose(KARTHIK, "slack", "post_message", {"channel": "C1", "text": "review please"}, t["id"])
    with pytest.raises(Denied):
        threads.approve(a["id"], KARTHIK, a["hash"])          # not the owner
    with pytest.raises(Denied):
        threads.approve(a["id"], ISHAN, "000000")             # a stale card
    with pytest.raises(Denied):
        threads.done(a["id"], "slack", {})                    # not approved yet
    threads.approve(a["id"], ISHAN, a["hash"])
    with pytest.raises(Denied):
        threads.done(a["id"], "tickets", {})                  # another surface
    threads.done(a["id"], "slack", {"text": "Review request posted", "url": "https://slack/p1"})
    trail = threads.get(t["id"], ISHAN)["entries"]
    assert trail[-1]["kind"] == "action" and trail[-1]["approval"] == a["hash"] and trail[-1]["by"] == ISHAN
    with pytest.raises(Denied):
        threads.done(a["id"], "slack", {})                    # once


def test_a_different_payload_is_a_different_hash(threads):
    a = threads.propose(ISHAN, "slack", "post_message", {"text": "a"})
    b = threads.propose(ISHAN, "slack", "post_message", {"text": "b"})
    assert a["hash"] != b["hash"]
    assert threads.approve(a["id"], ISHAN, a["hash"])["status"] == "approved"   # no thread: the proposer


def test_idle_threads_close_and_closed_ones_go(threads):
    t = threads.create(ISHAN, "Old")
    old = (now() - timedelta(days=15)).isoformat()
    threads.db.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (old, t["id"]))
    assert threads.mine(ISHAN) == []
    gone = (now() - timedelta(days=31)).isoformat()
    threads.db.execute("UPDATE threads SET closed_at = ? WHERE id = ?", (gone, t["id"]))
    with pytest.raises(Denied):
        threads.get(t["id"], ISHAN)


def test_the_active_thread_is_per_person_and_only_while_open(threads):
    t = threads.create(ISHAN, "Thresholds", [KARTHIK])
    assert threads.active(KARTHIK) is None
    threads.activate(t["id"], KARTHIK)
    assert threads.active(KARTHIK)["id"] == t["id"]
    threads.close(t["id"], ISHAN)
    assert threads.active(KARTHIK) is None
    mine = threads.create(ISHAN, "Another")
    other = threads.create(ISHAN, "Side quest")
    threads.activate(other["id"], ISHAN)
    threads.close(other["id"], ISHAN)
    assert threads.active(ISHAN)["id"] == mine["id"]       # the open one they own, not none


def test_thread_context_says_what_was_decided_and_never_what_was_proposed(threads):
    t = threads.create(ISHAN, "Shadow gate", [KARTHIK])
    threads.append(t["id"], ISHAN, "zoom", "transcript", text="Karthik Valluri: should the threshold be per model?")
    threads.append(t["id"], ISHAN, "zoom", "question", text="Default with no shadow traffic?", proposed=False)
    threads.append(t["id"], ISHAN, "docs", "link", url="https://docs/d1", title="Design: thresholds")
    text = context_text(threads.get(t["id"], ISHAN))
    assert "Working on: Shadow gate" in text and "Open question: Default" in text
    assert "https://docs/d1" in text and "per model?" in text


def test_intents():
    assert REVIEW_INTENT.search("draft a review request for my doc")
    assert TICKET_INTENT.search("make a ticket for this")
    assert not REVIEW_INTENT.search("who reviewed the shadow gate?")
    assert not TICKET_INTENT.search("which ticket covers the gate?")


def test_docs_prompts_carry_the_thread_only_when_there_is_one():
    from docs_addin.answer import context_prompt, prompt
    from docs_addin.retrieve import Context, Locked

    ctx = Context(viewer={"email": ISHAN, "member": "p1", "principals": []}, sources=[], locked=Locked())
    plain = prompt("who owns it?", ctx, "Ishan", "2026-10-28")
    assert "Working context" not in plain                       # recordings made without a thread still replay
    assert plain == prompt("who owns it?", ctx, "Ishan", "2026-10-28", working=None)
    assert "Working context" in prompt("who owns it?", ctx, "Ishan", "2026-10-28", working="Working on: X")
    assert "Working context" not in context_prompt("p", "s", "d", ctx, "Ishan", "2026-10-28")


# -- the API ------------------------------------------------------------------------------------ #

KEYS = {"zoom": "z" * 24, "docs": "d" * 24, "slack": "s" * 24, "tickets": "t" * 24}


class FakeDocs:
    def ask(self, question, viewer, about=None, working=None):
        return {"status": "answered", "answer": "ADR-014 set it [1].", "sources": [{"n": 1, "label": "ADR-014", "url": "u"}],
                "read": 3, "working": working, "viewer": {"email": viewer}}

    def context(self, paragraph, section, doc, viewer, working=None):
        return {"notes": [], "sources": [], "read": 0, "working": working}


@pytest.fixture
def api(tmp_path):
    core = Core(store=object(), threads=Threads(tmp_path / "t.db"), ledger=Ledger(tmp_path / "l.db"),
                model=False, docs=FakeDocs())
    core.shared_records = lambda t: []
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(core, KEYS))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"

    def call(path, body=None, key=KEYS["docs"]):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"X-Tesseract-Key": key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    yield call, core
    server.shutdown()


def test_every_route_but_health_needs_a_bot_key(api):
    call, _ = api
    assert call("/health", key="")[0] == 200
    assert call("/threads", {"owner": ISHAN, "title": "x"}, key="nope")[0] == 401
    assert call("/threads?member=" + ISHAN, key="")[0] == 401


def test_the_keyless_dev_page_is_never_served_through_the_tunnel(tmp_path):
    core = Core(store=object(), threads=Threads(tmp_path / "t.db"), ledger=Ledger(tmp_path / "l.db"),
                model=False, docs=FakeDocs())
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(core, KEYS, dev=True))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/dev/threads?member={ISHAN}"
    with urllib.request.urlopen(url) as r:
        assert r.status == 200
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(urllib.request.Request(url, headers={"Cf-Connecting-Ip": "203.0.113.9"}))
    assert e.value.code == 401
    server.shutdown()


def test_a_bot_adds_entries_and_finishes_approvals_only_as_itself(api):
    call, _ = api
    _, t = call("/threads", {"owner": ISHAN, "title": "Shadow gate", "audience": [KARTHIK]}, key=KEYS["zoom"])
    code, e = call(f"/threads/{t['id']}/entries", {"by": KARTHIK, "kind": "note", "text": "hi", "surface": "zoom"})
    assert code == 200 and e["surface"] == "docs"               # the key decides, not the body
    _, a = call(f"/threads/{t['id']}/ticket", {"by": KARTHIK}, key=KEYS["slack"])
    assert a["surface"] == "tickets" and "## Shadow gate" in a["payload"]["description"]
    assert call(f"/approvals/{a['id']}/approve", {"by": KARTHIK, "hash": a["hash"]})[0] == 403
    assert call(f"/approvals/{a['id']}/approve", {"by": ISHAN, "hash": a["hash"]})[0] == 200
    _, todo = call("/approvals?surface=tickets&status=approved", key=KEYS["tickets"])
    assert [x["id"] for x in todo] == [a["id"]]
    assert call(f"/approvals/{a['id']}/done", {"result": {"url": "https://jira/ML-21"}}, key=KEYS["slack"])[0] == 403
    code, done = call(f"/approvals/{a['id']}/done", {"result": {"url": "https://jira/ML-21", "text": "ML-21"}},
                      key=KEYS["tickets"])
    assert code == 200 and done["status"] == "done"
    _, trail = call(f"/threads/{t['id']}?as={ISHAN}")
    assert trail["entries"][-1]["url"] == "https://jira/ML-21" and trail["entries"][-1]["surface"] == "tickets"
    assert call(f"/threads/{t['id']}?as={HAMILTON}")[0] == 403


def test_docs_routes_pass_the_thread_as_working_context(api):
    call, core = api
    t = core.open_thread(ISHAN, "Shadow gate", [KARTHIK])
    core.append(t["id"], ISHAN, "zoom", "decision", text="Per-model thresholds", proposed=False)
    _, out = call("/docs/answer", {"viewer": KARTHIK, "question": "who owns the gate?", "thread": t["id"]})
    assert "Decided: Per-model thresholds" in out["working"]
    _, out = call("/docs/answer", {"viewer": KARTHIK, "question": "who owns the gate?"})
    assert out["working"] is None
    assert call("/docs/answer", {"viewer": HAMILTON, "question": "q", "thread": t["id"]})[0] == 403


def test_a_drafted_section_inserts_only_with_the_owners_approval(api):
    call, core = api
    t = core.open_thread(ISHAN, "Shadow gate", [KARTHIK])
    core.append(t["id"], ISHAN, "zoom", "question", text="Default with no shadow traffic?", proposed=False)
    _, d = call("/docs/draft", {"viewer": ISHAN, "thread": t["id"], "url": "https://docs/d1", "doc": "Design"})
    a = d["approval"]
    assert a["payload"]["questions"] == ["Default with no shadow traffic?"] and a["payload"]["sources"][0]["label"] == "ADR-014"
    assert call(f"/approvals/{a['id']}/approve", {"by": ISHAN, "hash": a["hash"]})[0] == 200
    assert call(f"/approvals/{a['id']}/done", {"result": {"text": "Inserted"}})[1]["status"] == "done"


def test_handoff_tells_the_surface_it_goes_to(api):
    call, core = api
    seen = []
    core.subscribe(seen.append)
    core.slack = {"team": "T1", "bot_user": "U1"}
    t = core.open_thread(ISHAN, "Shadow gate")
    _, out = call(f"/threads/{t['id']}/handoff", {"by": ISHAN, "to": "slack"})
    assert out["slack"] == {"team": "T1", "bot_user": "U1"}
    assert any(e["type"] == "handoff" and e["to"] == "slack" and e["by"] == ISHAN for e in seen)
    assert core.threads.active(ISHAN)["id"] == t["id"]
