"""
python -m ticket_desk         the Ticket Desk on http://127.0.0.1:8790 (TICKET_DESK_PORT)

Needs the sandbox graph in the local Neo4j (docker compose -f swarm/compose.yaml up -d neo4j) and an
Anthropic key; reads .env at the repo root for anything not already in the environment. The
graph is reached only through forge over MCP (forge_stub until forge ships; FORGE_COMMAND
overrides), as TICKET_DESK_VIEWER (Ishan by default).
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env() -> None:
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key and not key.startswith("#"):
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))
    os.environ.setdefault("NEO4J_PASSWORD", "neo4j-local-dev")       # swarm/compose.yaml's default


def main() -> None:
    load_env()
    import uvicorn

    from .app import build
    from .forge import NS, VIEWER

    port = int(os.environ.get("TICKET_DESK_PORT", "8790"))
    print(f"Ticket Desk on http://127.0.0.1:{port}  (graph {NS} as {VIEWER}, through forge over MCP)", file=sys.stderr)
    uvicorn.run(build(), host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
