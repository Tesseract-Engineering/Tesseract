"""bootstrap.py - build the graph once, for container start-up.

    python -m tesseract.bootstrap

Waits for Neo4j, then builds TESSERACT_NS from TESSERACT_DATA with the adapter
TESSERACT_ADAPTER (default: auto-detect). Skips when the namespace already has
nodes, so a restart reuses the persisted graph; FORCE_REBUILD=1 rebuilds.
"""

from __future__ import annotations

import sys
import time

from . import build, graph
from .config import env, settings

WAIT_SECONDS = 120


def node_count() -> int:
    deadline = time.time() + WAIT_SECONDS
    while True:
        try:
            return graph.run("MATCH (n {ns: $ns}) RETURN count(n) AS c")[0]["c"]
        except Exception as e:  # noqa: BLE001 - Neo4j still starting
            if time.time() > deadline:
                sys.exit(f"bootstrap: Neo4j not reachable at {settings().neo4j_uri}: {e}")
            time.sleep(2)


def main() -> None:
    ns, data = settings().namespace, env("TESSERACT_DATA")
    if not data:
        sys.exit("bootstrap: set TESSERACT_DATA to the company's export folder")
    count = node_count()
    if count and env("FORCE_REBUILD") != "1":
        print(f"bootstrap: {ns} already has {count} nodes, skipping (FORCE_REBUILD=1 to rebuild)", flush=True)
        return
    print(f"bootstrap: building {ns} from {data}", flush=True)
    started = time.time()
    build.main(["--root", data, "--ns", ns, "--adapter", env("TESSERACT_ADAPTER", "auto")])
    print(f"bootstrap: done in {time.time() - started:.0f}s, {node_count()} nodes", flush=True)


if __name__ == "__main__":
    main()
