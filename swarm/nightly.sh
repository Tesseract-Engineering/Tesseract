#!/usr/bin/env sh
# swarm/nightly.sh - one night's runs, in order, against the local dev stack.
#
# GitHub runs first and Jira second: the sources after each resolve links to
# it through what its run leaves in the bucket. A failed source does not stop the others (its run
# writes no manifest and is simply retried tomorrow); the script exits
# non-zero if any failed.
#
#   swarm/nightly.sh                          github, jira, slack, drive
#   SWARM_SOURCES="slack drive" swarm/nightly.sh
#
# Until Phase 5 schedules these as cloud jobs, cron on the dev machine runs it
# (the minute is arbitrary, off the hour):
#
#   47 2 * * * cd /path/to/Tesseract && swarm/nightly.sh >> var/nightly.log 2>&1

set -u
cd "$(dirname "$0")/.." || exit 1
compose="docker compose -f swarm/compose.yaml"

$compose up -d storage proxy || exit 1

failed=""
for source in ${SWARM_SOURCES:-github jira slack drive}; do
    echo "== $(date -u +%Y-%m-%dT%H:%M:%SZ) $source"
    $compose run --rm "$source" || failed="$failed $source"
done

if [ -n "$failed" ]; then
    echo "== failed:$failed"
    exit 1
fi
echo "== all sources complete"
