"""
api.py - the core over HTTP, for bots in other processes: the Docs sidebar (Apps Script, through a
tunnel) and the ticketing bot. The Slack bot and the meeting page run in the core's own process.

    POST /answer                    {as, question, context?, thread?, audience?} -> a quoted answer
    POST /threads                   {owner, title, audience?} -> thread
    GET  /threads?member=|owner=    a person's open threads, newest first
    GET  /threads/{id}?as=          one thread, if `as` is in its audience
    POST /threads/{id}/entries      {by, kind, ...} -> entry (decision/question wait for the owner)
    POST /threads/{id}/entries/{entry}/accept | /dismiss   {by}: the owner only
    POST /threads/{id}/handoff      {by, to}: continue in docs | slack | tickets
    POST /threads/{id}/close        {by}: the owner only
    GET  /threads/{id}/brief?as=    the thread condensed for a ticket
    POST /threads/{id}/ticket       {by}: propose a ticket with the brief (an approval)
    POST /approvals                 {by, action, payload, thread?} -> {id, hash, ...}
    GET  /approvals?surface=&status=   e.g. the ticketing bot's approved work
    POST /approvals/{id}/approve    {by, hash}: the thread's owner; then the surface performs it
    POST /approvals/{id}/done       {result: {url?, text?}, ok?}: the surface did it
    POST /approvals/{id}/cancel     {by}
    POST /docs/answer, /docs/context, /docs/draft   the Docs engine (docs_addin/), with an optional thread
    GET  /health

Every route but /health needs a bot's key in `X-Tesseract-Key` (the Docs sidebar's `X-Addin-Key`
works too), from data/core-keys.json, made on first run; ADDIN_KEY (or TESSERACT_API_KEY), when set, is the docs key. The
key says which surface is calling, so a bot can only add entries and finish approvals as itself.
The people named in a body (`as`, `by`, `viewer`) are trusted because the key is: each bot maps its
own sign-in to an email before calling. Listens on 127.0.0.1 only.

ADDIN_DEV=1 also serves the Docs sidebar at /dev, beside the demo doc, without Google or a key.
"""

from __future__ import annotations

import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .service import Core, answer_json
from .threads import SURFACES, Denied

MAX_BODY = 32_000
MAX_TEXT = 2000
DOCS = Path(__file__).resolve().parent.parent / "docs_addin"


def load_keys(path: str | Path = "data/core-keys.json") -> dict[str, str]:
    """surface -> key. Made once, git-ignored under data/; ADDIN_KEY overrides the docs key so the
    sidebar's Script Properties keep working."""
    p = Path(path)
    keys = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    if any(s not in keys for s in SURFACES):
        keys = {s: keys.get(s) or secrets.token_hex(24) for s in SURFACES}
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(keys, indent=2), encoding="utf-8")
    # The sidebar's key, under the add-in's name or the Script Property's.
    docs = (os.environ.get("ADDIN_KEY") or os.environ.get("TESSERACT_API_KEY") or "").strip()
    if docs:
        keys["docs"] = docs
    return keys


def _s(body: dict, k: str, n: int = MAX_TEXT) -> str | None:
    v = body.get(k)
    return str(v).strip()[:n] or None if v is not None else None


def _email(body: dict, k: str) -> str:
    v = (_s(body, k, 320) or "").lower()
    if "@" not in v:
        raise ValueError(f"{k} must be an email")
    return v


def handler(core: Core, keys: dict[str, str], dev: bool = False):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _send(self, code: int, body, kind: str = "application/json") -> None:
            data = (json.dumps(body, default=str) if not isinstance(body, (str, bytes)) else body)
            data = data.encode("utf-8") if isinstance(data, str) else data
            self.send_response(code)
            self.send_header("Content-Type", f"{kind}; charset=utf-8" if kind.startswith("text") or "json" in kind
                             else kind)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _surface(self) -> str | None:
            given = self.headers.get("X-Tesseract-Key") or self.headers.get("X-Addin-Key") or ""
            for surface, key in keys.items():
                if key and hmac.compare_digest(given, key):
                    return surface
            return None

        def _local(self) -> bool:
            """Asked on this PC, not through the tunnel (which also connects from 127.0.0.1, but says so)."""
            return dev and not any(self.headers.get(h) for h in ("Cf-Connecting-Ip", "X-Forwarded-For", "Forwarded"))

        def _route(self) -> tuple[str, dict, str | None]:
            u = urlparse(self.path)
            path, query = u.path.rstrip("/") or "/", {k: v[0] for k, v in parse_qs(u.query).items()}
            surface = self._surface()
            if self._local() and path.startswith("/dev/"):
                path, surface = path[len("/dev"):], "docs"      # the /dev page, on localhost only
            return path, query, surface

        # -- GET ------------------------------------------------------------------------------- #

        def do_GET(self) -> None:
            u = urlparse(self.path)
            if u.path == "/health":
                return self._send(200, {"ok": True, "ns": core.ns})
            if self._local() and u.path == "/dev":
                page = (DOCS / "apps_script" / "Sidebar.html").read_text(encoding="utf-8")
                shim = (DOCS / "dev_shim.html").read_text(encoding="utf-8")
                shim = shim.replace("__DEV_DOC__", (DOCS / "demo_doc.json").read_text(encoding="utf-8"))
                return self._send(200, page.replace("<head>", "<head>\n" + shim, 1), "text/html")
            path, query, surface = self._route()
            name = path.rsplit("/", 1)[-1]
            if path.startswith("/figures/") and (surface or self._local()) and re.fullmatch(r"[\w-]+\.png", name):
                f = DOCS / "figures" / name
                if f.is_file():
                    return self._send(200, f.read_bytes(), "image/png")
            if not surface:
                return self._send(401, {"error": "bad key"})
            try:
                return self._send(200, self.get(path, query, surface))
            except LookupError:
                return self._send(404, {"error": "not found"})
            except Denied as e:
                return self._send(403, {"error": str(e)})
            except ValueError as e:
                return self._send(400, {"error": str(e)})

        def get(self, path: str, q: dict, surface: str):
            if path == "/threads":
                if q.get("owner"):
                    return core.threads.mine(q["owner"], owned_only=True)
                if q.get("member"):
                    return core.threads.mine(q["member"])
                raise ValueError("owner or member is required")
            if path == "/approvals":
                return core.threads.approvals(q.get("surface"), q.get("status"))
            m = re.fullmatch(r"/threads/(thr_\w+)(/brief)?", path)
            if m:
                caller = _email(q, "as")
                return core.brief(m.group(1), caller) if m.group(2) else core.threads.get(m.group(1), caller)
            raise LookupError(path)

        # -- POST ------------------------------------------------------------------------------ #

        def do_POST(self) -> None:
            path, _query, surface = self._route()
            if not surface:
                return self._send(401, {"error": "bad key"})
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self._send(413, {"error": "too large"})
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self._send(400, {"error": "expected a JSON object"})
            started = time.time()
            try:
                out = self.post(path, body, surface)
            except LookupError:
                return self._send(404, {"error": "not found"})
            except Denied as e:
                return self._send(403, {"error": str(e)})
            except ValueError as e:
                return self._send(400, {"error": str(e)})
            except Exception as e:                       # the caller shows this; the log has the rest
                print(f"{path} failed: {type(e).__name__}: {e}", file=sys.stderr)
                return self._send(502, {"error": f"{type(e).__name__}: {e}"})
            if isinstance(out, dict) and path.startswith(("/answer", "/docs/")):
                out["seconds"] = round(time.time() - started, 1)
                print(f"{surface:7} {path:13} {out.get('status', str(len(out.get('notes', []))) + ' notes'):9} "
                      f"{out['seconds']:5}s", file=sys.stderr)
            self._send(200, out)

        def post(self, path: str, b: dict, surface: str):
            if path == "/answer":
                question = _s(b, "question")
                if not question:
                    raise ValueError("question is required")
                audience = [str(e).lower() for e in b.get("audience") or []] or None
                a = core.answer(_email(b, "as"), question, context=_s(b, "context", 4000), thread=_s(b, "thread"),
                                audience=audience)
                return answer_json(a)
            if path in ("/docs/answer", "/docs/context", "/docs/draft"):
                viewer = _email(b, "viewer")
                thread = _s(b, "thread", 40)
                if path == "/docs/draft":
                    if not thread:
                        raise ValueError("thread is required")
                    return core.docs_draft(viewer, thread, _s(b, "url", 500), _s(b, "doc", 200))
                if path == "/docs/answer":
                    question = _s(b, "question")
                    if not question:
                        raise ValueError("question is required")
                    return core.docs_answer(viewer, question, _s(b, "about"), thread)
                paragraph = _s(b, "paragraph")
                if not paragraph:
                    raise ValueError("paragraph is required")
                return core.docs_context(viewer, paragraph, _s(b, "section", 200), _s(b, "doc", 200), thread)
            if path == "/threads":
                title = _s(b, "title", 200)
                if not title:
                    raise ValueError("title is required")
                return core.open_thread(_email(b, "owner"), title, [str(e) for e in b.get("audience") or []],
                                        surface)
            if path == "/approvals":
                action, payload = _s(b, "action", 60), b.get("payload")
                if not action or not isinstance(payload, dict):
                    raise ValueError("action and a payload object are required")
                return core.propose(_email(b, "by"), surface, action, payload, _s(b, "thread", 40))
            m = re.fullmatch(r"/approvals/(apr_\w+)/(approve|done|cancel)", path)
            if m:
                aid, verb = m.groups()
                if verb == "approve":
                    return core.approve(aid, _email(b, "by"), _s(b, "hash", 12) or "")
                if verb == "cancel":
                    return core.cancel(aid, _email(b, "by"))
                result = b.get("result") if isinstance(b.get("result"), dict) else {}
                return core.done(aid, surface, {k: str(v)[:500] for k, v in result.items()}, b.get("ok", True) is not False)
            m = re.fullmatch(r"/threads/(thr_\w+)/(entries|handoff|close|ticket|active)", path)
            if m:
                tid, verb = m.groups()
                by = _email(b, "by")
                if verb == "entries":
                    data = {k: v for k, v in b.items() if k not in ("by", "viewer", "kind", "surface", "status")}
                    return core.append(tid, by, surface, _s(b, "kind", 20) or "", **data)
                if verb == "handoff":
                    to = _s(b, "to", 20)
                    if to not in SURFACES:
                        raise ValueError(f"to must be one of {', '.join(SURFACES)}")
                    return core.handoff(tid, by, to, surface)
                if verb == "active":
                    return core.threads.activate(tid, by)
                if verb == "ticket":
                    return core.propose_ticket(tid, by)
                return core.close(tid, by)
            m = re.fullmatch(r"/threads/(thr_\w+)/entries/(ent_\w+)/(accept|dismiss)", path)
            if m:
                return core.decide(m.group(1), m.group(2), _email(b, "by"), m.group(3) == "accept")
            raise LookupError(path)

    return Handler


def serve(core: Core, port: int, keys: dict[str, str] | None = None, dev: bool = False) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), handler(core, keys or load_keys(), dev))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
