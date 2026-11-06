"""
python -m core [--env .env] [--slack] [--meeting] [--port 8787]

Runs the Tesseract core API (core/api.py) and, in the same process, the bots that live next to it:
--slack the Slack app (slackbot/), --meeting the call page too (it needs --slack: the people on the
call come from the Slack workspace). The Docs sidebar and the ticketing bot call the API.

Loads the env file before anything reads the environment (graph/store.py reads NEO4J_* on import).
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import threading
from pathlib import Path


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            if v.strip():
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def main(argv: list[str] | None = None, prog: str = "python -m core", slack_default: bool = False) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog=prog)
    ap.add_argument("--env", type=Path, default=Path(".env"))
    ap.add_argument("--slack", action="store_true", default=slack_default, help="also run the Slack app")
    ap.add_argument("--meeting", action="store_true", help="also serve the Tesseract Meeting page (implies --slack)")
    ap.add_argument("--port", type=int, default=None, help="the core API's port (TESSERACT_PORT, 8787)")
    ap.add_argument("--no-api", action="store_true", help="run the bots without the HTTP API")
    ap.add_argument("--ask", nargs=2, metavar=("EMAIL", "QUESTION"), help="answer one question in the terminal")
    args = ap.parse_args(argv)
    load_env(args.env)
    os.environ.setdefault("NEO4J_PASSWORD", "neo4j-local-dev")     # swarm/compose.yaml's default
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    log = logging.getLogger("core")

    from .service import Core

    core = Core()

    if args.ask:
        email, question = args.ask
        a = core.answer(email, question)
        print(f"{email}: searched {a.searched}\n\n{a.status}: {a.headline}")
        for c in a.claims:
            print(f"  - {c.text}\n    > {c.quote}\n    — {c.hit.title} ({c.hit.source} {c.hit.kind})")
        for h in a.locked:
            print(f"    locked: {h.source} {h.kind}, owner {[o.name for o in h.owners]}")
        return

    if not args.no_api:
        from .api import load_keys, serve

        port = args.port or int(os.environ.get("TESSERACT_PORT") or os.environ.get("ADDIN_PORT") or "8787")
        dev = os.environ.get("ADDIN_DEV") == "1"
        serve(core, port, load_keys(os.environ.get("TESSERACT_KEYS", "data/core-keys.json")), dev=dev)
        log.info("Tesseract core on http://127.0.0.1:%d (namespace %s%s); bot keys in %s", port, core.ns,
                 ", /dev on" if dev else "", os.environ.get("TESSERACT_KEYS", "data/core-keys.json"))

    if args.slack or args.meeting:
        from slackbot.app import Bot

        Bot(core, meeting=args.meeting).run()
    else:
        threading.Event().wait()


if __name__ == "__main__":
    main()
