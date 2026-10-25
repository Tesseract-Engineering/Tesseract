"""
The identity pass: which source identities across the bucket are one person.

    python pipelines/identity/main.py                write to the bucket (S3_BUCKET, S3_ENDPOINT)
    python pipelines/identity/main.py --local DIR    read and write a local folder instead

Run it after the source pipelines; it reads every complete run they wrote.
Identities that share an email are merged in plain code; the model judges
only the ones no email joins (swarm/identity.py). The model follows
SWARM_MODEL_BACKEND and SWARM_MODEL (see swarm/model.py); with it off, those
identities stay apart until a run with one.
"""

from __future__ import annotations

import argparse
import sys

from swarm.cli import llm, report, setup_logging, store_for
from swarm.identity import run_identity


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--local", metavar="DIR", help="read and write a local folder instead of the bucket")
    args = parser.parse_args(argv)
    setup_logging()
    manifest, checks = run_identity(store_for(args.local), model=llm()["model"])
    report(manifest, checks)
    return 0


if __name__ == "__main__":
    sys.exit(main())
