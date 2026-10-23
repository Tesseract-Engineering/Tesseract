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

LLM extraction follows SWARM_MODEL_BACKEND (anthropic, bedrock or off),
SWARM_MODEL and SWARM_LLM_TOKEN_BUDGET; see swarm/model.py.
"""

from __future__ import annotations

import argparse
import sys

from swarm.cli import backfill_days, listed, llm, report, setup_logging, store_for
from swarm.http import Client
from swarm.run import run_source
from swarm.sources.github import GitHubConnector, GitHubNormalizer
from swarm.sources.github.auth import API, HEADERS, from_env
from swarm.sources.github.connector import read_known_docs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--local", metavar="DIR", help="write to a local folder instead of the bucket")
    args = parser.parse_args(argv)
    setup_logging()

    auth = from_env()
    store = store_for(args.local)
    connector = GitHubConnector(Client(API, headers=HEADERS, auth=auth.headers), mode=auth.mode,
                                repos=listed("GITHUB_REPOS") or None,
                                external_tracker=listed("GITHUB_EXTERNAL_TRACKER_REPOS"),
                                backfill_days=backfill_days(),
                                known_docs=read_known_docs(store))
    manifest, checks = run_source(connector, GitHubNormalizer(connector.index), store, **llm())
    report(manifest, checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
