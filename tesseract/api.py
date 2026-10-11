"""api.py - the JSON API over answering (answer.py) and the approval door (actions.py).

    python -m tesseract.api            # http://localhost:8091 (API_HOST, API_PORT)

GET  /health                      -> {"ok", "neo4j", "time"}
GET  /company                     -> {"name", "viewers", "default_viewer", "users", "suggestions"}
GET  /viewers                     -> {"viewers": [...], "default"}
POST /ask      {question[, viewer, asker]}   -> answer.answer; viewer is a preset
                                     name or {"name", "acl_groups"}; asker is a name
POST /design   {question, node_ids[, viewer]} -> design.diagram
POST /propose  {actions}          -> {"proposal_id", "actions": [...]}
POST /approve  {proposal_id, action_id, approver[, payload]}
POST /reject   {proposal_id, action_id, approver[, reason]}
POST /execute  {proposal_id, action_id}
POST /reset                       -> remove every executed draft, clear proposals
GET  /proposal/<id>               -> one proposal and its actions
GET  /receipts                    -> approved / executed / rejected actions
GET  /doc?node_id=..[&viewer=]    -> one record's full text (403 if unreadable)
GET  /systems[?viewer=]           -> readable record counts by source
GET  /usage/top?limit=10[&viewer=]               -> most-asked nodes
GET  /usage/co-asked?node_id=..[&limit=&viewer=] -> nodes the same questions led to

Stdlib http.server. Errors are {"error": code, "message": str}. CORS origins
come from API_CORS_ORIGINS (comma-separated; default *).
"""

from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

from . import access, actions, design, graph, llm, querylog
from .answer import Q_SOURCES, answer
from .config import env, settings

PORT = int(env("API_PORT", "8091"))
HOST = env("API_HOST", "127.0.0.1")
CORS_ORIGINS = [o.strip() for o in env("API_CORS_ORIGINS", "*").split(",") if o.strip()]
MAX_BODY = 1_000_000


class ApiError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# usage log: never allowed to fail an answer
# --------------------------------------------------------------------------- #

def _log_ask(question: str, out: dict) -> None:
    """Record the ask, then write it into the graph as (:Question)-[:LED_TO]->(node)."""
    try:
        ids = [c["node_id"] for c in out.get("claims", []) if c.get("node_id")] + list(out.get("entities") or [])
        querylog.record(question, "\n;\n".join(out.get("cypher", [])), ids, out.get("ms"),
                        viewer=out.get("viewer"), origin="api")
        querylog.sync_to_graph(graph.driver(), ns=graph.namespace(), database=graph.database())
    except Exception as e:  # noqa: BLE001
        sys.stderr.write(f"usage log failed: {type(e).__name__}: {e}\n")


def _redact_usage(rows: list[dict], viewer: dict) -> list[dict]:
    """Titles of records the viewer cannot read are dropped; ids stay, as in /ask stubs."""
    out = []
    for r in rows:
        r = dict(r)
        acl = r.pop("acl", [])
        if not access.readable(r.get("visibility"), acl, viewer):
            r.update(title="", restricted=True, questions=[],
                     note=f"you do not have access; owner {access.owner_of(acl)}")
        out.append(r)
    return out


# --------------------------------------------------------------------------- #
# records
# --------------------------------------------------------------------------- #

Q_DOC = """
MATCH (n {ns: $ns, id: $id}) WHERE n:Document OR n:Issue OR n:Service
OPTIONAL MATCH (p:Person)-[:HAS_ACCOUNT]->(:Account)-[:AUTHORED]->(n)
OPTIONAL MATCH (n)-[:ASSIGNED_TO]->(a:Person)
RETURN labels(n)[0] AS label, n.id AS id,
       coalesce(n.title, CASE WHEN n.key IS NOT NULL THEN n.key ELSE n.name END) AS title, n.key AS key,
       coalesce(n.text, n.body, n.description, '') AS text, coalesce(n.source, '') AS source,
       n.createdAt AS at, coalesce(n.visibility, 'internal') AS visibility,
       coalesce(n.aclGroups, []) AS acl, n.path AS path, n.state AS state,
       collect(DISTINCT p.name) AS authors, collect(DISTINCT a.name) AS assignees
""".strip()


def _doc(node_id: str, viewer: dict) -> tuple[int, dict]:
    """Full text of one Document, Issue or Service, only when the viewer may read it."""
    rows = graph.run(Q_DOC, id=node_id)
    if not rows:
        raise ApiError("not_found", f"no document {node_id}")
    r = rows[0]
    if not access.readable(r["visibility"], r["acl"], viewer):
        owner = access.owner_of(r["acl"])
        return 403, {"error": "forbidden", "message": f"you do not have access; owner {owner}",
                     "node_id": node_id, "visibility": r["visibility"], "owner": owner}
    people = list(dict.fromkeys((r["authors"] or []) + (r["assignees"] or [])))
    title = r["title"] or node_id
    if r["label"] == "Issue" and r["key"] and r["key"] not in title:
        title = f"{r['key']}: {title}"
    return 200, {"node_id": node_id, "label": r["label"], "title": title, "text": r["text"],
                 "source": r["source"], "date": graph.date(r["at"]), "author": ", ".join(people),
                 "visibility": r["visibility"], "access": access.grant(r["acl"], viewer), "path": r["path"],
                 "state": r["state"], "viewer": viewer["name"]}


# --------------------------------------------------------------------------- #
# routing
# --------------------------------------------------------------------------- #

def _need(body: dict, *keys: str) -> list:
    missing = [k for k in keys if not body.get(k)]
    if missing:
        raise ApiError("bad_request", f"missing field(s): {', '.join(missing)}")
    return [body[k] for k in keys]


def _viewer(value) -> dict:
    try:
        return access.viewer_of(value)
    except ValueError as e:
        raise ApiError("bad_request", str(e)) from None


def _limit(query: dict) -> int:
    try:
        return max(1, min(100, int(query.get("limit") or 10)))
    except ValueError:
        raise ApiError("bad_request", "limit must be an integer") from None


def _get(path: str, query: dict) -> tuple[int, object]:
    if path == "/health":
        try:
            graph.driver().verify_connectivity()
            neo = True
        except Exception:  # noqa: BLE001
            neo = False
        return 200, {"ok": True, "neo4j": neo, "time": time.strftime("%Y-%m-%dT%H:%M:%S")}
    if path == "/company":
        company = settings().company
        return 200, {"name": company.name, "viewers": list(access.viewers().values()),
                     "default_viewer": company.default_viewer, "users": company.sign_in_users(),
                     "suggestions": list(company.suggestions)}
    if path == "/viewers":
        return 200, {"viewers": list(access.viewers().values()), "default": access.default_viewer()["name"]}
    if path == "/receipts":
        return 200, {"receipts": actions.receipts()}
    if path.startswith("/proposal/"):
        return 200, actions.proposal(path.split("/", 2)[2])
    if path == "/systems":
        viewer = _viewer(query.get("viewer"))
        rows = graph.run(Q_SOURCES, acl=viewer["acl_groups"])
        return 200, {"systems": [{"source": r["source"], "documents": r["documents"]} for r in rows],
                     "viewer": viewer["name"]}
    if path == "/doc":
        viewer = _viewer(query.get("viewer"))
        if not query.get("node_id"):
            raise ApiError("bad_request", "missing query parameter: node_id")
        return _doc(query["node_id"], viewer)
    if path == "/usage/top":
        viewer = _viewer(query.get("viewer"))
        rows = querylog.top_asked(graph.driver(), _limit(query), ns=graph.namespace(), database=graph.database())
        return 200, {"top": _redact_usage(rows, viewer), "viewer": viewer["name"],
                     "logged_queries": querylog.count()}
    if path == "/usage/co-asked":
        viewer = _viewer(query.get("viewer"))
        if not query.get("node_id"):
            raise ApiError("bad_request", "missing query parameter: node_id")
        rows = querylog.co_asked(graph.driver(), query["node_id"], _limit(query),
                                 ns=graph.namespace(), database=graph.database())
        return 200, {"node_id": query["node_id"], "co_asked": _redact_usage(rows, viewer),
                     "viewer": viewer["name"],
                     "note": "inferred from shared questions (LED_TO, producer usage), not from source documents"}
    return 404, {"error": "not_found", "message": f"GET {path}"}


def _post(path: str, body: dict) -> tuple[int, object]:
    if path == "/ask":
        (question,) = _need(body, "question")
        viewer = _viewer(body.get("viewer"))
        asker = body.get("asker")
        asker = (asker.get("name") if isinstance(asker, dict) else asker) or None
        out = answer(str(question), viewer=viewer, asker=str(asker) if asker else None)
        _log_ask(str(question), out)
        return 200, out
    if path == "/design":
        question, ids = _need(body, "question", "node_ids")
        if not isinstance(ids, list):
            raise ApiError("bad_request", "node_ids must be a list")
        return 200, design.diagram(_viewer(body.get("viewer")), str(question), [str(i) for i in ids])
    if path == "/propose":
        (acts,) = _need(body, "actions")
        return 200, actions.proposal(actions.propose(acts))
    if path == "/approve":
        pid, aid, who = _need(body, "proposal_id", "action_id", "approver")
        return 200, actions.approve(pid, aid, who, body.get("payload"))
    if path == "/reject":
        pid, aid, who = _need(body, "proposal_id", "action_id", "approver")
        return 200, actions.reject(pid, aid, who, body.get("reason", ""))
    if path == "/execute":
        pid, aid = _need(body, "proposal_id", "action_id")
        return 200, actions.execute(pid, aid)
    if path == "/reset":
        return 200, actions.reset()
    return 404, {"error": "not_found", "message": f"POST {path}"}


def route(method: str, path: str, body: dict, query: dict | None = None) -> tuple[int, object]:
    if method == "GET":
        return _get(path, query or {})
    if method == "POST":
        return _post(path, body)
    return 405, {"error": "method_not_allowed", "message": method}


STATUS = {"forbidden": 403, "not_found": 404, "bad_request": 400, "bad_action": 400, "hash_mismatch": 409,
          "rejected": 409, "not_approved": 409}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("api %s - %s\n" % (self.command, fmt % args))

    def _cors(self) -> None:
        origin = self.headers.get("Origin") or ""
        if "*" in CORS_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", "*")
        elif origin in CORS_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send(self, code: int, obj) -> None:
        data = json.dumps(obj, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _handle(self, method: str) -> None:
        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                raise ApiError("bad_request", f"body larger than {MAX_BODY} bytes")
            body = json.loads(self.rfile.read(length) or b"{}") if length else {}
            if not isinstance(body, dict):
                raise ApiError("bad_request", "body must be a JSON object")
            raw_path, _, raw_query = self.path.partition("?")
            query = {k: v[-1] for k, v in parse_qs(raw_query).items()}
            code, obj = route(method, raw_path.rstrip("/") or "/", body, query)
        except json.JSONDecodeError as e:
            code, obj = 400, {"error": "bad_request", "message": f"invalid JSON: {e}"}
        except (ApiError, actions.DoorError) as e:
            code, obj = STATUS.get(e.code, 400), {"error": e.code, "message": str(e)}
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            code, obj = 500, {"error": "internal", "message": f"{type(e).__name__}: {e}"}
        self._send(code, obj)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")


def _warm_llm() -> None:
    """Make the first model call off the request path, so the first question
    does not pay for client setup (or, on Gemini, model discovery)."""
    if not llm.available():
        print("tesseract api: no model key, /ask runs keyword search only", flush=True)
        return
    t0 = time.perf_counter()
    llm.generate("Reply with the single word: ready", "ping", max_tokens=5, temperature=0.0)
    print(f"tesseract api: model ready ({llm.provider()} {llm.model_name()}, "
          f"{time.perf_counter() - t0:.1f}s)", flush=True)


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    s = settings()
    print(f"tesseract api on http://{HOST}:{PORT}  company={s.company.name!r} ns={s.namespace}", flush=True)
    threading.Thread(target=_warm_llm, daemon=True, name="llm-warmup").start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
