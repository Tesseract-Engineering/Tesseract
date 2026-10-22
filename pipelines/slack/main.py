"""
The Slack pipeline: one run over every channel in scope.

    python pipelines/slack/main.py                   nightly: the Web API with the bot token
    python pipelines/slack/main.py --export PATH     cold start: a workspace export (.zip or folder)
    python pipelines/slack/main.py --local DIR       write to a local folder instead of the bucket

Settings come from the environment (see .env.example): SLACK_BOT_TOKEN for
the API; SLACK_WORKSPACE_URL (and SLACK_TEAM_ID if users.json cannot tell)
for an export; SLACK_CHANNELS narrows scope by name or id;
SWARM_BACKFILL_DAYS sets the first API run's window (default 730).

GitHub links in messages resolve through what the GitHub pipeline left in
the same bucket, so run GitHub first.

LLM extraction follows SWARM_MODEL_BACKEND (anthropic, bedrock or off),
SWARM_MODEL and SWARM_LLM_TOKEN_BUDGET; see swarm/model.py.
"""

from __future__ import annotations

import argparse
import os
import sys

from swarm.cli import backfill_days, listed, llm, report, setup_logging, store_for
from swarm.http import Client
from swarm.run import run_source
from swarm.sources.links import GitHubLinks
from swarm.sources.slack import SlackApiConnector, SlackExportConnector, SlackNormalizer
from swarm.sources.slack.connector import API


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--export", metavar="PATH", help="read a workspace export instead of the API")
    parser.add_argument("--local", metavar="DIR", help="write to a local folder instead of the bucket")
    args = parser.parse_args(argv)
    setup_logging()

    channels = listed("SLACK_CHANNELS") or None
    if args.export:
        connector = SlackExportConnector(args.export, workspace_url=os.environ.get("SLACK_WORKSPACE_URL", ""),
                                         team_id=os.environ.get("SLACK_TEAM_ID") or None, channels=channels)
    else:
        token = os.environ.get("SLACK_BOT_TOKEN")
        if not token:
            raise SystemExit("Slack credentials missing: set SLACK_BOT_TOKEN, or pass --export")
        client = Client(API, auth=lambda: {"Authorization": f"Bearer {token}"})
        connector = SlackApiConnector(client, channels=channels, backfill_days=backfill_days())
    store = store_for(args.local)
    normalizer = SlackNormalizer(connector.directory, GitHubLinks.from_store(store))
    manifest, checks = run_source(connector, normalizer, store, **llm())
    report(manifest, checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
