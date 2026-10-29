"""
python -m slackbot [--env .env] [--ask EMAIL QUESTION]

Loads the env file before anything reads the environment (graph/store.py reads NEO4J_* on import),
then runs the Slack app. --ask answers one question as that email in the terminal, with no Slack:
for checking what the graph gives a person before trying it in Slack.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            if v.strip():
                os.environ.setdefault(k.strip(), v.strip().strip('"'))


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(prog="python -m slackbot")
    ap.add_argument("--env", type=Path, default=Path(".env"))
    ap.add_argument("--ask", nargs=2, metavar=("EMAIL", "QUESTION"))
    ap.add_argument("--meeting", action="store_true",
                    help="also serve the Tesseract Meeting page (live transcript and asks) on this PC")
    args = ap.parse_args()
    load_env(args.env)
    os.environ.setdefault("NEO4J_PASSWORD", "neo4j-local-dev")     # swarm/compose.yaml's default
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

    if args.ask:
        from graph.store import Neo4jStore

        from .answer import answer
        from .app import model_from_env
        from .cards import lock_line
        from .graph import Graph

        email, question = args.ask
        g = Graph(Neo4jStore(), os.environ.get("SLACKBOT_NS", "kg:swarm"))
        v = g.viewer(email)
        a = answer(g, v, question, model_from_env())
        print(f"{email}: member={v['member']}  searched {a.searched}")
        print(f"\n{a.status}: {a.headline}")
        for c in a.claims:
            print(f"  - {c.text}\n    > {c.quote}\n    — {c.hit.title} ({c.hit.source} {c.hit.kind})")
        for h in a.misses:
            print(f"  near miss: {h.title}")
        print(lock_line(a) or "(nothing locked)")
        for h in a.locked:
            print(f"    locked: {h.source} {h.kind}, owner {[o.name for o in h.owners]}")
        return

    from .app import Bot

    Bot(meeting=args.meeting).run()


if __name__ == "__main__":
    main()
