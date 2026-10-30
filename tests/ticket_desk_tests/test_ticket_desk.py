"""Ticket Desk: the agent reaches the graph only through MCP, cites only what the graph returned,
and nothing leaves without an approval. No Neo4j and no model: a fake forge runs in process (as
docs/FORGE.md suggests) and the model is scripted."""

import json
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent

from forge_stub import server as stub
from ticket_desk import agent, app, fix
from ticket_desk.forge import Forge, Session

ROOT = Path(__file__).resolve().parents[2]
PR = {"id": "github:repo:1:pr:7", "labels": ["Issue"], "title": "Cache JWKS for 10 minutes", "kind": "pull_request",
      "source": "github", "url": "https://github.com/acme/auth/pull/7", "sourceUpdatedAt": "2026-10-22T20:44:23Z"}


def fake_forge() -> MCPServer:
    f = MCPServer("forge")

    def ok(data: dict) -> CallToolResult:
        return CallToolResult(content=[TextContent(type="text", text=json.dumps(data))], structured_content=data)

    @f.tool()
    def search_brain(query: str, labels: list[str] | None = None, sources: list[str] | None = None,
                     limit: int = 10) -> stub.Result:
        hits = [{**PR, "snippet": "caches the JWKS", "score": 1.0}] if "jwks" in query.lower() else []
        return ok({"hits": hits, "trust": "untrusted", "provenance": {"node_id": None}})

    @f.tool()
    def get_entity(id_or_ref: str) -> stub.Result:
        if id_or_ref != PR["id"]:
            return stub._error("not_found", "no such node")
        return ok({"entity": PR, "stale": None, "live": None, "trust": "untrusted", "provenance": {"node_id": PR["id"]}})

    @f.tool()
    def read_content(id_or_ref: str, handle: str | None = None, max_chars: int = 8000) -> stub.Result:
        return ok({"text": "Cache JWKS for 10 minutes.", "next_handle": None, "origin": "index",
                   "provenance": {"node_id": PR["id"], "url": PR["url"]}})

    return f


def block(**kw):
    return SimpleNamespace(**kw)


class ScriptedModel:
    """Answers from a script, and remembers every request it was sent."""

    def __init__(self, turns):
        self.turns, self.requests = list(turns), []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    async def create(self, **kwargs):
        self.requests.append(kwargs)
        return self.turns.pop(0)


def tool_turn(*uses):
    return block(stop_reason="tool_use", content=[block(type="tool_use", id=f"t{i}", name=n, input=a)
                                                  for i, (n, a) in enumerate(uses)])


def answer_turn(answer):
    return block(stop_reason="end_turn", content=[block(type="text", text=json.dumps(answer))])


def answer(cite: list[str]) -> dict:
    c = {"text": "x", "sources": cite}
    return {"reply": {"subject": "Re: 401s", "paragraphs": [c]},
            "triage": {"classification": "repeat", "summary": "s", "root_cause": c, "history": [c],
                       "owners": [{"name": "Ishan Sinha", "why": "owns it", "sources": cite}],
                       "next_steps": [c], "confidence": "high"},
            "known_fix": {"found": True, "resolved_before": "CS-6", "steps": ["Map the claim"], "snippet": "",
                          "sources": cite},
            "fix": {"warranted": True, "repo": "auth-service", "plan": c}}


TICKET = {"id": "TD-1", "customer": "Initech", "reporter": {"name": "Peter", "email": "peter@initech.example"},
          "service": "auth-service", "severity": "high", "created_at": "2026-10-29T16:00:00Z",
          "subject": "401s after rotation", "body": "Tokens fail for ten minutes."}


def test_seed_tickets_are_well_formed():
    tickets = json.loads((ROOT / "ticket_desk" / "tickets.json").read_text())["tickets"]
    assert len({t["id"] for t in tickets}) == len(tickets) >= 10
    kinds = {t["expected"]["kind"] for t in tickets}
    assert kinds == {"repeat", "near_repeat", "new"}
    for t in tickets:
        assert {"customer", "reporter", "service", "severity", "subject", "body", "created_at"} <= set(t)


def test_graph_run_cites_only_what_forge_returned():
    model = ScriptedModel([tool_turn(("search_brain", {"query": "jwks 401"})),
                           answer_turn(answer([PR["id"], "jira:made-up:issue:1"]))])
    events = []

    async def emit(e):
        events.append(e)

    async def main():
        async with Forge(fake_forge()) as forge:
            return await agent.work(TICKET, "graph", Session(forge), emit, llm=model)

    out = anyio.run(main)
    assert out["answer"]["triage"]["root_cause"]["sources"] == [PR["id"]]       # the invented id is gone
    assert out["dropped"] == 7                                                  # one per claim
    assert set(out["sources"]) == {PR["id"]} and out["sources"][PR["id"]]["url"] == PR["url"]
    assert [e["type"] for e in events if e["type"].startswith("tool")] == ["tool_call", "tool_result"]
    assert {t["name"] for t in model.requests[0]["tools"]} == {"search_brain", "get_entity", "read_content"}


def test_baseline_has_no_tools_and_no_sources():
    model = ScriptedModel([answer_turn(answer([PR["id"]]))])

    async def emit(e):
        pass

    async def main():
        async with Forge(fake_forge()) as forge:
            return await agent.work(TICKET, "baseline", Session(forge), emit, llm=model)

    out = anyio.run(main)
    assert "tools" not in model.requests[0]
    assert out["sources"] == {} and out["answer"]["reply"]["paragraphs"][0]["sources"] == []
    assert out["answer"]["known_fix"]["found"] is False          # nothing to trace it to, so not shown
    assert "get_neighbors" not in model.requests[0]["system"][0]["text"]      # the prompt never mentions tools


def test_read_content_nodes_are_named_for_provenance():
    async def main():
        async with Forge(fake_forge()) as forge:
            s = Session(forge)
            await s.call("read_content", {"id_or_ref": PR["id"]})
            return s

    s = anyio.run(main)
    assert s.seen[PR["id"]].title == PR["title"] and s.calls[0]["nodes"] == [PR["id"]]


def test_the_model_must_answer_after_its_tool_budget():
    model = ScriptedModel([tool_turn(("search_brain", {"query": "jwks"})),
                           tool_turn(("search_brain", {"query": "jwks"})),
                           answer_turn(answer([]))])

    async def execute(name, args):
        return "nothing", False

    async def emit(e):
        pass

    anyio.run(lambda: agent.loop(model, "sys", "ticket", [{"name": "search_brain"}], execute, agent.SCHEMA,
                                 emit, max_calls=2))
    assert "tool_choice" not in model.requests[1]
    assert model.requests[2]["tool_choice"] == {"type": "none"}


def test_nothing_leaves_without_approval(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "STATE", tmp_path / "state.json")
    monkeypatch.setattr(app, "VAR", tmp_path)

    async def fake_work(ticket, mode, session, emit, llm=None):
        return {"answer": answer([PR["id"]]), "sources": {PR["id"]: {"id": PR["id"], "title": PR["title"]}},
                "calls": [], "dropped": 0}

    monkeypatch.setattr(app, "work", fake_work)

    async def main():
        desk = app.Desk(forge=None)
        tid = next(iter(desk.tickets))
        run = desk.start(tid, "graph")
        async for _ in run.follow():
            pass
        pending = [p for p in desk.proposals.values() if p["ticket"] == tid]
        assert {p["kind"] for p in pending} == {"reply", "triage"}
        assert all(p["status"] == "pending" for p in pending) and desk.outbox == []

        reply = next(p for p in pending if p["kind"] == "reply")
        triage = next(p for p in pending if p["kind"] == "triage")
        await desk.decide(triage["id"], "reject", None)
        assert desk.outbox == []
        await desk.decide(reply["id"], "approve", "Edited by a person.")
        assert [m["body"] for m in desk.outbox] == ["Edited by a person."]
        assert desk.outbox[0]["to"] == desk.tickets[tid]["reporter"]["email"]
        with pytest.raises(ValueError):
            await desk.decide(reply["id"], "approve", None)                  # once only
        return desk

    desk = anyio.run(main)
    saved = json.loads((tmp_path / "state.json").read_text())
    assert len(saved["outbox"]) == 1 and len(desk.log) == 2


def test_fix_writes_stay_in_src_and_tests(tmp_path):
    (tmp_path / "src").mkdir()
    assert fix.inside(tmp_path, "src/auth_service/jwks.py", write=True).name == "jwks.py"
    assert fix.inside(tmp_path, "README.md").name == "README.md"                # reading is fine
    for bad in ("../outside.py", "/etc/passwd", "README.md", ".git/config", "src/../setup.py"):
        with pytest.raises(ValueError):
            fix.inside(tmp_path, bad, write=True)


def test_pr_body_numbers_and_links_its_sources():
    proposal = {"description": [{"text": "Refetch on an unknown kid.", "sources": [PR["id"]]},
                                {"text": "Rate limited.", "sources": []}],
                "tests": {"after": "...\n2 passed in 0.02s"}}
    body = fix.pr_body(proposal, TICKET, {PR["id"]: PR}, "Ishan Sinha")
    assert "Refetch on an unknown kid. [1]" in body
    assert f"1. [Cache JWKS for 10 minutes]({PR['url']})" in body
    assert "`2 passed in 0.02s`" in body and "approved by Ishan Sinha" in body


def test_stub_search_cannot_pass_lucene_syntax():
    assert stub._lucene('title:"x" OR body:* AND (jwks~ NOT 401)') == "title x body jwks 401"


def test_stub_paging_handles_are_signed():
    h = stub._sign({"node_id": "n", "version": "v", "offset": 10, "exp": 9e12})
    assert stub._unsign(h)["offset"] == 10
    b64, mac = h.rsplit(".", 1)
    assert stub._unsign(b64 + "." + "0" * len(mac)) is None
    assert stub._unsign("garbage") is None
