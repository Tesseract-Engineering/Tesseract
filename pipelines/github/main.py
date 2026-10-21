"""
The GitHub pipeline: one run over every repo in scope.

    python pipelines/github/main.py                  write to the bucket (S3_BUCKET, S3_ENDPOINT)
    python pipelines/github/main.py --local DIR      write to a local folder instead, for a dry run

Credentials and scope come from the environment (see .env.example):
GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY_PATH + GITHUB_INSTALLATION_ID, or
GITHUB_TOKEN; GITHUB_REPOS narrows scope (and is required with a token);
GITHUB_EXTERNAL_TRACKER_REPOS lists repos whose bare "#123" means an outside
tracker such as Trac; SWARM_BACKFILL_DAYS sets the first run's window
(default 730).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from swarm.http import Client
from swarm.run import run_source
from swarm.sources.github import GitHubConnector, GitHubNormalizer
from swarm.sources.github.auth import API, HEADERS, from_env
from swarm.store import LocalStore, S3Store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--local", metavar="DIR", help="write to a local folder instead of the bucket")
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(levelname)s %(name)s %(message)s")
    # httpx logs every request URL at INFO; swarm.http already logs retries.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    auth = from_env()
    def listed(name: str) -> list[str]:
        return [r.strip() for r in os.environ.get(name, "").split(",") if r.strip()]

    connector = GitHubConnector(Client(API, headers=HEADERS, auth=auth.headers), mode=auth.mode,
                                repos=listed("GITHUB_REPOS") or None,
                                external_tracker=listed("GITHUB_EXTERNAL_TRACKER_REPOS"),
                                backfill_days=int(os.environ.get("SWARM_BACKFILL_DAYS", "730")))
    store = LocalStore(args.local) if args.local else S3Store()
    manifest, checks = run_source(connector, GitHubNormalizer(connector.index), store)
    print(json.dumps({"run_id": manifest.run_id, "counts": manifest.counts, "checks": checks}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
