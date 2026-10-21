"""
The Phase 0 job: replays samples/run-0 through the real write, manifest and
check stages into the staging bucket, under fresh run ids.

    python pipelines/sample/main.py init            create the dev bucket (local dev only)
    python pipelines/sample/main.py run             one replay; a second replay writes nothing
    python pipelines/sample/main.py probe URL...    egress check; exits 1 if any URL is refused

Real pipelines (pipelines/github, ...) replace the replay with a Connector and
a Normalizer; everything after that is the same code.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from swarm.records import ExtractionRecord, ItemRecord, Manifest, parse_line
from swarm.stages.checks import check_run
from swarm.stages.manifest import finish_run
from swarm.stages.write import RunWriter, new_run_id
from swarm.store import S3Store

SAMPLE = Path(os.environ.get("SAMPLE_DIR", Path(__file__).resolve().parents[2] / "samples" / "run-0" / "runs"))


def replay(store: S3Store) -> list[Manifest]:
    manifests = []
    for source_dir in sorted(p for p in SAMPLE.iterdir() if p.is_dir()):
        source = source_dir.name
        for run_dir in sorted(p for p in source_dir.iterdir() if p.is_dir()):
            sample = Manifest.model_validate_json((run_dir / "_manifest.json").read_text(encoding="utf-8"))
            started = datetime.now(timezone.utc)
            run_id = new_run_id(source, started)
            writer = RunWriter(store, source, run_id)
            for name in sample.files:
                for line in (run_dir / name).read_text(encoding="utf-8").splitlines():
                    record = parse_line(line).model_copy(update={"run_id": run_id})
                    if isinstance(record, ItemRecord):
                        writer.add_item(record)
                    elif isinstance(record, ExtractionRecord):
                        writer.add_extraction(record)
                    else:
                        writer.add(record)
            manifest = finish_run(writer, started_at=started, extractor_versions=sample.extractor_versions)
            checks = check_run(store, source, run_id)
            print(json.dumps({"run_id": run_id, "counts": manifest.counts, "checks": checks}))
            manifests.append(manifest)
    return manifests


def init(store: S3Store) -> None:
    try:
        store.client.head_bucket(Bucket=store.bucket)
    except store.client.exceptions.ClientError:
        store.client.create_bucket(Bucket=store.bucket)
        print(f"created bucket {store.bucket}")


def probe(urls: list[str]) -> int:
    """Report whether each URL is reachable from inside the internal network.
    Returns how many were refused."""
    failed = 0
    for url in urls:
        try:
            urllib.request.urlopen(url, timeout=10)
            verdict = "reached"
        except urllib.error.HTTPError as e:
            # The host answered (auth errors are fine here), unless the answer
            # is the proxy's own refusal of a plain-HTTP request.
            denied = e.headers.get("X-Squid-Error")
            verdict = f"refused ({e.code} {denied})" if denied else "reached"
        except (urllib.error.URLError, OSError) as e:
            verdict = f"refused ({getattr(e, 'reason', e)})"
        print(f"{url}: {verdict}")
        failed += verdict != "reached"
    return failed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["init", "run", "probe"])
    parser.add_argument("urls", nargs="*")
    args = parser.parse_args(argv)
    if args.command == "probe":
        return 1 if probe(args.urls) else 0
    store = S3Store()
    init(store) if args.command == "init" else replay(store)
    return 0


if __name__ == "__main__":
    sys.exit(main())
