"""
The Google Drive pipeline: one run over every shared drive and folder in scope.

    python pipelines/drive/main.py                   write to the bucket (S3_BUCKET, S3_ENDPOINT)
    python pipelines/drive/main.py --local DIR       write to a local folder instead, for a dry run

Settings come from the environment (see .env.example):
GDRIVE_SERVICE_ACCOUNT_KEY_PATH (or GDRIVE_SERVICE_ACCOUNT_KEY) for the
service account; GDRIVE_DRIVES narrows the shared drives it joined, by name
or id; GDRIVE_FOLDERS lists folders shared with it, for accounts without Google
Workspace; SWARM_BACKFILL_DAYS sets the first run's window (default 730).

GitHub links in documents resolve through what the GitHub pipeline left in
the same bucket, so run GitHub first.

LLM extraction follows SWARM_MODEL_BACKEND (anthropic, bedrock or off),
SWARM_MODEL and SWARM_LLM_TOKEN_BUDGET; see swarm/model.py.
"""

from __future__ import annotations

import argparse
import sys

from swarm.cli import backfill_days, listed, llm, report, setup_logging, store_for
from swarm.http import Client
from swarm.run import run_source
from swarm.sources.gdrive import DriveConnector, DriveNormalizer
from swarm.sources.gdrive.auth import from_env
from swarm.sources.gdrive.connector import API
from swarm.sources.links import GitHubLinks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--local", metavar="DIR", help="write to a local folder instead of the bucket")
    args = parser.parse_args(argv)
    setup_logging()

    auth = from_env()
    connector = DriveConnector(Client(API, auth=auth.headers), drives=listed("GDRIVE_DRIVES") or None,
                               folders=listed("GDRIVE_FOLDERS"), service_account=auth.email,
                               backfill_days=backfill_days())
    store = store_for(args.local)
    manifest, checks = run_source(connector, DriveNormalizer(GitHubLinks.from_store(store)), store, **llm())
    report(manifest, checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
