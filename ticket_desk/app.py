"""
app.py - the Ticket Desk: an inbox, the agent working a ticket live, and every output as a
proposal that waits for a person.

    GET  /                               the single-page UI (static/index.html)
    GET  /api/state                      tickets, runs, proposals, the outbox and the action log
    POST /api/tickets/{id}/run           {"mode": "graph"|"baseline", "fresh": bool}: start a run
    POST /api/tickets/{id}/fix           start the code-fix run from the graph run's triage
    GET  /api/tickets/{id}/stream/{mode} the run's events as server-sent events, from the start
    POST /api/tickets/{id}               {"status": ..., "assignee": ...}: a person updates the ticket
    POST /api/proposals/{pid}            {"action": "approve"|"reject", "content": edited text?}
    GET  /api/node?id=...                one graph node through forge's get_entity, for provenance

The approval gate is here: the agent's runs only ever create proposals. A reply goes to the outbox,
a triage note to the team's log, and a fix to GitHub as a draft PR, each only from POST
/api/proposals with "approve". State is kept in var/ticket_desk/state.json, so finished runs show
again after a restart without calling the model.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from . import fix as fixes
from .agent import check_sources, work
from .forge import VIEWER, Forge, Session

HERE = Path(__file__).resolve().parent
VAR = Path(os.environ.get("TICKET_DESK_VAR", HERE.parent / "var" / "ticket_desk"))
STATE = VAR / "state.json"
APPROVER = os.environ.get("TICKET_DESK_APPROVER", "Ishan Sinha")
STATUSES = ["Open", "In progress", "Waiting on customer", "Resolved"]
PEOPLE = [APPROVER, "Dhruv Pendharkar", "Karthik Valluri", "Hamilton Park"]   # the sandbox's Platform team


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_tickets() -> list[dict]:
    return json.loads((HERE / "tickets.json").read_text())["tickets"]


class Run:
    """One agent run: its events so far, and a way to wait for the next one."""

    def __init__(self, ticket: str, mode: str, saved: dict | None = None):
        self.ticket, self.mode = ticket, mode
        saved = saved or {}
        self.events: list[dict] = saved.get("events", [])
        self.result: dict | None = saved.get("result")
        self.error: str | None = saved.get("error")
        self.started = saved.get("started") or _now()
        self.finished = saved.get("finished")
        self._t0 = time.monotonic()
        self._changed = asyncio.Event()

    @property
    def done(self) -> bool:
        return self.finished is not None

    async def emit(self, event: dict) -> None:
        self.events.append({**event, "t": round(time.monotonic() - self._t0, 1)})
        self._changed.set()
        self._changed = asyncio.Event()

    def finish(self, result: dict | None = None, error: str | None = None) -> None:
        self.result, self.error, self.finished = result, error, _now()
        self._changed.set()

    async def follow(self):
        i = 0
        while True:
            changed = self._changed
            while i < len(self.events):
                yield self.events[i]
                i += 1
            if self.done:
                return
            await changed.wait()

    def saved(self) -> dict:
        return {"mode": self.mode, "events": self.events, "result": self.result, "error": self.error,
                "started": self.started, "finished": self.finished}


class Desk:
    def __init__(self, forge: Forge):
        self.forge = forge
        self.tickets = {t["id"]: t for t in load_tickets()}
        state = json.loads(STATE.read_text()) if STATE.exists() else {}
        self.runs: dict[str, dict[str, Run]] = {
            tid: {mode: Run(tid, mode, saved) for mode, saved in modes.items() if saved.get("finished")}
            for tid, modes in state.get("runs", {}).items()}
        self.proposals: dict[str, dict] = state.get("proposals", {})
        self.outbox: list[dict] = state.get("outbox", [])
        self.log: list[dict] = state.get("log", [])
        self.meta: dict[str, dict] = state.get("meta", {})      # per ticket: status, assignee
        self.tasks: set[asyncio.Task] = set()

    def save(self) -> None:
        VAR.mkdir(parents=True, exist_ok=True)
        state = {"runs": {tid: {m: r.saved() for m, r in modes.items() if r.done} for tid, modes in self.runs.items()},
                 "proposals": self.proposals, "outbox": self.outbox, "log": self.log, "meta": self.meta}
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1, default=str))
        tmp.replace(STATE)

    def public(self) -> dict:
        tickets = [{k: v for k, v in t.items() if k != "expected"} for t in self.tickets.values()]
        runs = {tid: {m: {"done": r.done, "error": r.error, "started": r.started, "finished": r.finished,
                          "result": r.result} for m, r in modes.items()} for tid, modes in self.runs.items()}
        for t in tickets:
            t.update({"status": "Open", "assignee": APPROVER, **self.meta.get(t["id"], {})})
        return {"viewer": VIEWER, "approver": APPROVER, "tickets": tickets, "runs": runs,
                "proposals": self.proposals, "outbox": self.outbox, "log": self.log,
                "statuses": STATUSES, "people": PEOPLE}

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _supersede(self, tid: str, kinds: tuple[str, ...]) -> None:
        for p in self.proposals.values():
            if p["ticket"] == tid and p["kind"] in kinds and p["status"] == "pending":
                p["status"] = "superseded"

    def start(self, tid: str, mode: str, fresh: bool = False) -> Run:
        current = self.runs.get(tid, {}).get(mode)
        if current and (not current.done or not fresh):
            return current
        run = self.runs.setdefault(tid, {})[mode] = Run(tid, mode)
        self._spawn(self._work(run))
        return run

    async def _work(self, run: Run) -> None:
        ticket = self.tickets[run.ticket]
        try:
            result = await work(ticket, run.mode, Session(self.forge), run.emit)
        except Exception as e:                       # the run fails, the desk does not
            await run.emit({"type": "error", "text": f"{type(e).__name__}: {e}"})
            run.finish(error=str(e))
            self.save()
            return
        if run.mode == "graph":
            self._supersede(run.ticket, ("reply", "triage"))
            answer = result["answer"]
            for kind in ("reply", "triage"):
                pid = f"{run.ticket}-{kind}-{int(time.time())}"
                self.proposals[pid] = {"id": pid, "ticket": run.ticket, "kind": kind, "status": "pending",
                                       "created": _now(), "content": answer[kind], "sources": result["sources"]}
        await run.emit({"type": "result", "result": result})
        run.finish(result=result)
        self.save()

    def start_fix(self, tid: str) -> Run:
        graph = self.runs.get(tid, {}).get("graph")
        if not graph or not graph.result:
            raise ValueError("run the graph agent on this ticket first")
        current = self.runs[tid].get("fix")
        if current and not current.done:
            return current
        run = self.runs[tid]["fix"] = Run(tid, "fix")
        self._spawn(self._fix(run, graph.result))
        return run

    async def _fix(self, run: Run, graph: dict) -> None:
        ticket = self.tickets[run.ticket]
        answer = graph["answer"]
        session = Session(self.forge)
        try:
            proposal = await fixes.propose(ticket, answer["triage"], answer["fix"]["plan"], graph["sources"],
                                           session, run.emit, repo=answer["fix"].get("repo") or "auth-service")
        except Exception as e:
            await run.emit({"type": "error", "text": f"{type(e).__name__}: {e}"})
            run.finish(error=str(e))
            self.save()
            return
        # The graph run's entries first: they were cited there, and the fix run only adds to them.
        sources = {**{i: s.public() for i, s in session.seen.items()}, **graph["sources"]}
        check_sources(proposal["description"], sources)
        proposal["sources"] = sources
        self._supersede(run.ticket, ("fix",))
        pid = f"{run.ticket}-fix-{int(time.time())}"
        self.proposals[pid] = {"id": pid, "ticket": run.ticket, "kind": "fix", "status": "pending",
                               "created": _now(), "content": proposal, "sources": sources}
        await run.emit({"type": "result", "result": {"proposal": pid}})
        run.finish(result={"proposal": pid})
        self.save()

    def update(self, tid: str, changes: dict) -> dict:
        """A person changes a ticket's status or assignee."""
        meta = self.meta.setdefault(tid, {})
        for field, allowed in (("status", STATUSES), ("assignee", PEOPLE)):
            if field in changes:
                if changes[field] not in allowed:
                    raise ValueError(f"{field} must be one of {', '.join(allowed)}")
                if meta.get(field) != changes[field]:
                    meta[field] = changes[field]
                    self.log.append({"at": _now(), "ticket": tid, "what": f"{field.capitalize()} set to {changes[field]}"})
        self.save()
        return meta

    async def decide(self, pid: str, action: str, content: str | None) -> dict:
        p = self.proposals[pid]
        if p["status"] != "pending":
            raise ValueError(f"this proposal is already {p['status']}")
        ticket = self.tickets[p["ticket"]]
        p["decided"], p["decided_by"] = _now(), APPROVER
        if action == "reject":
            p["status"] = "rejected"
            self.log.append({"at": p["decided"], "ticket": ticket["id"], "what": f"Rejected the {p['kind']}"})
        elif p["kind"] == "reply":
            p["edited"] = content is not None
            body = content if content is not None else "\n\n".join(x["text"] for x in p["content"]["paragraphs"])
            self.outbox.append({"at": p["decided"], "ticket": ticket["id"], "to": ticket["reporter"]["email"],
                                "subject": p["content"]["subject"], "body": body, "approved_by": APPROVER})
            p["status"], p["outcome"] = "approved", f"Sent to {ticket['reporter']['email']} (local outbox)"
            self.meta.setdefault(ticket["id"], {})["status"] = "Waiting on customer"
            self.log.append({"at": p["decided"], "ticket": ticket["id"], "what": "Reply approved and sent"})
        elif p["kind"] == "triage":
            p["edited"] = content is not None
            if content is not None:
                p["content"]["edited_text"] = content
            p["status"], p["outcome"] = "approved", "Posted to the Platform triage log"
            self.log.append({"at": p["decided"], "ticket": ticket["id"], "what": "Triage note approved"})
        elif p["kind"] == "fix":
            fix = p["content"]
            if content is not None:
                fix["title"] = content.strip() or fix["title"]
            body = fixes.pr_body(fix, ticket, p["sources"], APPROVER)
            try:
                url = await asyncio.to_thread(fixes.open_pr, fix, body)
            except Exception as e:
                p.pop("decided"), p.pop("decided_by")
                p["last_error"] = str(e)
                self.save()
                raise RuntimeError(f"Could not open the PR: {e}") from e
            p["status"], p["outcome"], p["pr_url"] = "approved", f"Draft PR opened: {url}", url
            self.log.append({"at": p["decided"], "ticket": ticket["id"], "what": f"Draft PR opened {url}"})
        self.save()
        return p


def build(forge_target=None) -> Starlette:
    desk: Desk | None = None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal desk
        async with Forge(forge_target) as forge:
            desk = Desk(forge)
            app.state.desk = desk
            yield
            for task in list(desk.tasks):
                task.cancel()

    async def index(request: Request):
        return FileResponse(HERE / "static" / "index.html")

    async def state(request: Request):
        return JSONResponse(desk.public())

    async def run(request: Request):
        body = await request.json()
        mode = body.get("mode", "graph")
        if mode not in ("graph", "baseline"):
            return JSONResponse({"error": "mode is graph or baseline"}, status_code=400)
        tid = request.path_params["tid"]
        if tid not in desk.tickets:
            return JSONResponse({"error": "no such ticket"}, status_code=404)
        r = desk.start(tid, mode, bool(body.get("fresh")))
        return JSONResponse({"ticket": tid, "mode": mode, "done": r.done})

    async def start_fix(request: Request):
        try:
            desk.start_fix(request.path_params["tid"])
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        return JSONResponse({"ok": True})

    async def stream(request: Request):
        r = desk.runs.get(request.path_params["tid"], {}).get(request.path_params["mode"])
        if r is None:
            return JSONResponse({"error": "no such run"}, status_code=404)

        async def events():
            async for e in r.follow():
                yield f"data: {json.dumps(e, default=str)}\n\n"
            yield "event: end\ndata: {}\n\n"

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    async def decide(request: Request):
        body = await request.json()
        pid = request.path_params["pid"]
        if pid not in desk.proposals or body.get("action") not in ("approve", "reject"):
            return JSONResponse({"error": "unknown proposal or action"}, status_code=400)
        try:
            p = await desk.decide(pid, body["action"], body.get("content"))
        except (ValueError, RuntimeError) as e:
            return JSONResponse({"error": str(e)}, status_code=409)
        return JSONResponse(p)

    async def update(request: Request):
        tid = request.path_params["tid"]
        if tid not in desk.tickets:
            return JSONResponse({"error": "no such ticket"}, status_code=404)
        try:
            return JSONResponse(desk.update(tid, await request.json()))
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

    async def node(request: Request):
        try:
            text, data, is_error = await asyncio.wait_for(
                desk.forge.call("get_entity", {"id_or_ref": request.query_params.get("id", "")}), timeout=8)
        except asyncio.TimeoutError:
            return JSONResponse({"error": "the graph did not answer in time (is Neo4j running?)"}, status_code=503)
        if is_error:
            return JSONResponse({"error": (data.get("error") or {}).get("message", "not found")}, status_code=404)
        return JSONResponse(data)

    return Starlette(routes=[
        Route("/", index),
        Route("/api/state", state),
        Route("/api/tickets/{tid}/run", run, methods=["POST"]),
        Route("/api/tickets/{tid}/fix", start_fix, methods=["POST"]),
        Route("/api/tickets/{tid}/stream/{mode}", stream),
        Route("/api/proposals/{pid}", decide, methods=["POST"]),
        Route("/api/tickets/{tid}", update, methods=["POST"]),
        Route("/api/node", node),
    ], lifespan=lifespan)
