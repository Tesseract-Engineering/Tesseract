"""
api.py - the add-in's one endpoint, in front of the graph and the model.

    POST /answer   {"question": "...", "about": "...", "viewer": "hamilton@..."}   header X-Addin-Key
                   -> answer.py's JSON: status, answer with [n] markers, cited sources, gaps,
                      who to ask, and what is locked; "about" is the passage the question is
                      about (the writer's selection), when there is one
    POST /context  {"paragraph": "...", "section": "...", "doc": "...", "viewer": "..."}  same key
                   -> up to three notes on the paragraph being written (conflict, incident,
                      decided, owner), cited the same way; none when nothing bears on it
    GET  /health   {"ok": true, "ns": ...}
    GET  /figures/<name>.png   the demo doc's figures, with the key (the script that writes the doc)
    GET  /dev      the sidebar in a plain browser tab, its google.script.run calls served from
                   here (POST /dev/answer, no key): ADDIN_DEV=1 only, for working on the sidebar
                   without deploying it. The server listens on 127.0.0.1 alone

Apps Script runs on Google's servers, so in the demo this sits behind a tunnel and the script
sends the key from its Script Properties. The viewer is whoever the script says is signed in;
the key is what makes that trustworthy, so it never leaves the server and the script project.
A deployment for real users would verify a Google ID token (ScriptApp.getIdentityToken) instead.

    ADDIN_KEY=... NEO4J_PASSWORD=... python -m docs_addin.api        ADDIN_PORT (8787), ADDIN_NS (kg:sandbox),
                                                                     ADDIN_TODAY (pin the date, to replay)
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

from .answer import answer, context, model_from_env
from .retrieve import directory, gather, paragraph_query, slack_people

HERE = Path(__file__).parent
MAX_QUESTION = 2000
MAX_BODY = 16_000

# The document the /dev page shows in place of a real one: the billing-service design doc.
DEV_DOC = json.loads((HERE / "demo_doc.json").read_text(encoding="utf-8"))


class App:
    def __init__(self, ns: str, key: str, dev: bool = False):
        self.ns, self.key, self.dev = ns, key, dev
        self.store = Neo4jStore()
        self.model = model_from_env()
        # The date the prompts say it is. Pinned (ADDIN_TODAY=2026-10-28), recorded answers keep
        # replaying on later days; unset, it is the real date.
        self.today = os.environ.get("ADDIN_TODAY", "").strip() or None
        self._people: tuple[float, dict[str, str], dict[str, dict]] | None = None
        self._lock = threading.Lock()

    def _directory(self) -> tuple[dict[str, str], dict[str, dict]]:
        with self._lock:
            if self._people is None or time.time() - self._people[0] > 300:
                self._people = (time.time(), directory(self.store, self.ns), slack_people(self.store, self.ns))
            return self._people[1], self._people[2]

    def people(self) -> dict[str, str]:
        return self._directory()[0]

    def with_slack(self, out: dict) -> dict:
        """Where to message the people an answer names: the person to ask, whoever can share
        what is locked, and anyone a note names in full. Added after the model, so prompts and
        recordings do not change."""
        slack = self._directory()[1]
        if out.get("ask") and out["ask"]["name"] in slack:
            out["ask"]["slack"] = slack[out["ask"]["name"]]
        for owner in out.get("locked", {}).get("owners", []):
            if owner.get("name") in slack:
                owner["slack"] = slack[owner["name"]]
        for note in out.get("notes", []):
            # The person a note is about is the one in its title ("Dhruv Pendharkar owns infra
            # Terraform"); someone named in passing in the text ("then Karthik is on call") is not.
            named = [n for n in slack if n in note["title"]] or [n for n in slack if n in note["text"]]
            note["people"] = [{"name": n, "slack": slack[n]} for n in named]
        return out

    def ask(self, question: str, viewer: str, about: str | None = None) -> dict:
        ctx = gather(self.store, self.ns, viewer, f"{question} {about}" if about else question)
        out = answer(self.model, question, ctx, self.people(), today=self.today, about=about)
        out["viewer"] = {"email": ctx.viewer["email"], "member": ctx.viewer["member"]}
        return self.with_slack(out)

    def context(self, paragraph: str, section: str | None, doc: str | None, viewer: str) -> dict:
        ctx = gather(self.store, self.ns, viewer, paragraph_query(paragraph, section))
        out = context(self.model, paragraph, section, doc, ctx, self.people(), today=self.today)
        out["viewer"] = {"email": ctx.viewer["email"], "member": ctx.viewer["member"]}
        return self.with_slack(out)


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
            # The demo doc's figures: on /dev for the dev page, and behind the key for the script that
            # writes the Google Doc. Only the PNGs in figures/, by bare name.
            route = self.path.split("?")[0]
            name = route.rsplit("/", 1)[-1]
            keyed = route.startswith("/figures/") and hmac.compare_digest(self.headers.get("X-Addin-Key", ""), app.key)
            if (keyed or (app.dev and route.startswith("/dev/figures/"))) and name.endswith(".png") and "/" not in name:
                path = HERE / "figures" / name
                if path.is_file():
                    data = path.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "image/png")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                    return
            if self.path.split("?")[0] == "/dev" and app.dev:
                page = (HERE / "apps_script" / "Sidebar.html").read_text(encoding="utf-8")
                shim = (HERE / "dev_shim.html").read_text(encoding="utf-8")
                shim = shim.replace("__DEV_DOC__", json.dumps(DEV_DOC))
                return self._send(200, page.replace("<head>", "<head>\n" + shim, 1), "text/html")
            self._send(404, {"error": "not found"})

        def do_POST(self) -> None:
            path = self.path
            dev = path.startswith("/dev/") and app.dev       # the /dev page, on localhost only
            if dev:
                path = path[len("/dev"):]
            if path not in ("/answer", "/context"):
                return self._send(404, {"error": "not found"})
            if not dev and not hmac.compare_digest(self.headers.get("X-Addin-Key", ""), app.key):
                return self._send(401, {"error": "bad key"})
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send(413, {"error": "too large"})
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
                viewer = str(body.get("viewer") or "").strip().lower()
                text = str(body.get("question" if path == "/answer" else "paragraph") or "").strip()[:MAX_QUESTION]
                section = str(body.get("section") or "").strip()[:200] or None
                doc = str(body.get("doc") or "").strip()[:200] or None
                about = str(body.get("about") or "").strip()[:MAX_QUESTION] or None
            except (ValueError, AttributeError):
                return self._send(400, {"error": "expected JSON"})
            if not text or "@" not in viewer:
                return self._send(400, {"error": "text and viewer are required"})
            started = time.time()
            try:
                out = app.ask(text, viewer, about) if path == "/answer" else app.context(text, section, doc, viewer)
            except Exception as e:              # the sidebar shows this; the log has the rest
                print(f"{path} failed: {type(e).__name__}: {e}", file=sys.stderr)
                return self._send(502, {"error": f"{type(e).__name__}: {e}"})
            out["seconds"] = round(time.time() - started, 1)
            what = out["status"] if path == "/answer" else f"{len(out['notes'])} notes"
            print(f"{viewer} {path[1:]:7} {what:8} {out['seconds']:5}s  {text[:70]}", file=sys.stderr)
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
