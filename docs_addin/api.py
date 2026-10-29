"""
api.py - the add-in's one endpoint, in front of the graph and the model.

    POST /answer   {"question": "...", "viewer": "hamilton@..."}   header X-Addin-Key
                   -> answer.py's JSON: status, answer with [n] markers, cited sources, gaps,
                      who to ask, and what is locked
    GET  /health   {"ok": true, "ns": ...}
    GET  /dev      the sidebar in a plain browser tab, its google.script.run calls served from
                   here (POST /dev/answer, no key): ADDIN_DEV=1 only, for working on the sidebar
                   without deploying it. The server listens on 127.0.0.1 alone

Apps Script runs on Google's servers, so in the demo this sits behind a tunnel and the script
sends the key from its Script Properties. The viewer is whoever the script says is signed in;
the key is what makes that trustworthy, so it never leaves the server and the script project.
A deployment for real users would verify a Google ID token (ScriptApp.getIdentityToken) instead.

    ADDIN_KEY=... NEO4J_PASSWORD=... python -m docs_addin.api        ADDIN_PORT (8787), ADDIN_NS (kg:sandbox)
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from graph.store import Neo4jStore

from .answer import answer, model_from_env
from .retrieve import directory, gather

HERE = Path(__file__).parent
MAX_QUESTION = 2000
MAX_BODY = 16_000

# The questions the /dev page shows in place of a real document's.
DEV_DOC = {
    "title": "SOC2 Responses - Authentication (FY2026)",
    "questions": [
        "Does the authentication service use an industry-standard protocol (for example OpenID Connect) "
        "for federated login?",
        "How are token signing keys rotated, and how often? Provide evidence of the last rotation.",
        "Which custom claims are issued in access tokens, and how are they validated downstream?",
        "Are deprecated authentication endpoints inventoried, with a removal date?",
        "How is access to token signing material restricted?",
    ],
}


class App:
    def __init__(self, ns: str, key: str, dev: bool = False):
        self.ns, self.key, self.dev = ns, key, dev
        self.store = Neo4jStore()
        self.model = model_from_env()
        self._people: tuple[float, dict[str, str]] | None = None
        self._lock = threading.Lock()

    def people(self) -> dict[str, str]:
        with self._lock:
            if self._people is None or time.time() - self._people[0] > 300:
                self._people = (time.time(), directory(self.store, self.ns))
            return self._people[1]

    def ask(self, question: str, viewer: str) -> dict:
        ctx = gather(self.store, self.ns, viewer, question)
        out = answer(self.model, question, ctx, self.people())
        out["viewer"] = {"email": ctx.viewer["email"], "member": ctx.viewer["member"]}
        return out


def handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code: int, body: dict | str, kind: str = "application/json") -> None:
            data = (json.dumps(body) if isinstance(body, dict) else body).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", f"{kind}; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            if self.path == "/health":
                return self._send(200, {"ok": True, "ns": app.ns})
            if self.path.split("?")[0] == "/dev" and app.dev:
                page = (HERE / "apps_script" / "Sidebar.html").read_text(encoding="utf-8")
                shim = (HERE / "dev_shim.html").read_text(encoding="utf-8")
                shim = shim.replace("__DEV_DOC__", json.dumps(DEV_DOC))
                return self._send(200, page.replace("<head>", "<head>\n" + shim, 1), "text/html")
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            dev = self.path == "/dev/answer" and app.dev       # the /dev page, on localhost only
            if self.path != "/answer" and not dev:
                return self._send(404, {"error": "not found"})
            if not dev and not hmac.compare_digest(self.headers.get("X-Addin-Key", ""), app.key):
                return self._send(401, {"error": "bad key"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send(413, {"error": "too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                question = str(body.get("question") or "").strip()[:MAX_QUESTION]
                viewer = str(body.get("viewer") or "").strip().lower()
            except (ValueError, AttributeError):
                return self._send(400, {"error": "expected JSON"})
            if not question or "@" not in viewer:
                return self._send(400, {"error": "question and viewer are required"})
            started = time.time()
            try:
                out = app.ask(question, viewer)
            except Exception as e:              # the sidebar shows this; the log has the rest
                print(f"answer failed: {type(e).__name__}: {e}", file=sys.stderr)
                return self._send(502, {"error": f"{type(e).__name__}: {e}"})
            out["seconds"] = round(time.time() - started, 1)
            print(f"{viewer} {out['status']:8} {out['seconds']:5}s  {question[:70]}", file=sys.stderr)
            self._send(200, out)

        def log_message(self, *_args) -> None:     # one line per answer above is enough
            pass

    return Handler


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    key = os.environ.get("ADDIN_KEY", "").strip()
    if len(key) < 16:
        raise SystemExit("ADDIN_KEY must be set (16+ characters); the Apps Script project sends the same one")
    app = App(os.environ.get("ADDIN_NS", "kg:sandbox"), key, dev=os.environ.get("ADDIN_DEV") == "1")
    port = int(os.environ.get("ADDIN_PORT", "8787"))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler(app))
    print(f"docs add-in API on http://127.0.0.1:{port} (namespace {app.ns}{', /dev on' if app.dev else ''})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.store.close()


if __name__ == "__main__":
    main()
