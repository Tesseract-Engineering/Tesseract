"""
The Jira pipeline: one run over every project in scope.

    python pipelines/jira/main.py                    write to the bucket (S3_BUCKET, S3_ENDPOINT)
    python pipelines/jira/main.py --local DIR        write to a local folder instead, for a dry run

Settings come from the environment (see .env.example): JIRA_BASE_URL,
JIRA_EMAIL and JIRA_API_TOKEN for the read-only service account;
JIRA_PROJECTS narrows the projects it can browse, by key;
SWARM_BACKFILL_DAYS sets the first run's window (default 730).

GitHub links in tickets resolve through what the GitHub pipeline left in the
same bucket, so run GitHub first. Slack and Drive resolve Jira keys through
what this pipeline leaves, so run them after.

LLM extraction follows SWARM_MODEL_BACKEND (anthropic, bedrock or off),
SWARM_MODEL and SWARM_LLM_TOKEN_BUDGET; see swarm/model.py.
"""

from __future__ import annotations

import argparse
import json
import sys

from swarm.cli import backfill_days, listed, llm, report, setup_logging, store_for
from swarm.http import Client
from swarm.run import run_source
from swarm.sources.jira import JiraConnector, JiraNormalizer
from swarm.sources.jira.auth import from_env
from swarm.sources.links import GitHubLinks
from swarm.stages.write import state_key


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--local", metavar="DIR", help="write to a local folder instead of the bucket")
    args = parser.parse_args(argv)
    setup_logging()

    auth = from_env()
    store = store_for(args.local)
    known = store.get(state_key("jira", "keys"))
    connector = JiraConnector(Client(auth.base_url, headers={"Accept": "application/json"}, auth=auth.headers),
                              browse_url=auth.base_url, projects=listed("JIRA_PROJECTS") or None,
                              backfill_days=backfill_days(), known_keys=json.loads(known) if known else None)
    normalizer = JiraNormalizer(connector.index, GitHubLinks.from_store(store))
    manifest, checks = run_source(connector, normalizer, store, **llm())
    report(manifest, checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
