"""The write and manifest stages against a real S3 API: the local dev bucket
from swarm/compose.yaml. Skipped unless S3_ENDPOINT answers.

    docker compose -f swarm/compose.yaml up -d storage
    S3_ENDPOINT=http://127.0.0.1:9000 AWS_ACCESS_KEY_ID=swarm-dev \\
        AWS_SECRET_ACCESS_KEY=swarm-dev-secret pytest -m s3

Each session writes to a fresh bucket, since no role may delete.
"""

import os
import uuid
from datetime import datetime, timezone

import pytest

from swarm.stages.checks import check_run
from swarm.stages.manifest import finish_run, read_manifest
from swarm.stages.write import RunWriter

from .test_write import items

pytestmark = pytest.mark.s3


@pytest.fixture(scope="module")
def store():
    endpoint = os.environ.get("S3_ENDPOINT")
    if not endpoint:
        pytest.skip("S3_ENDPOINT not set")
    from swarm.store import S3Store

    s3 = S3Store(bucket=f"swarm-test-{uuid.uuid4().hex[:12]}", endpoint=endpoint)
    try:
        s3.client.create_bucket(Bucket=s3.bucket)
    except Exception as e:  # unreachable endpoint or bad credentials
        pytest.skip(f"S3 endpoint unusable: {type(e).__name__}")
    return s3


def test_rerun_writes_nothing_against_s3(store):
    started = datetime.now(timezone.utc)
    for run_id, expected in (("r1", 3), ("r2", 0)):
        writer = RunWriter(store, "github", run_id)
        for item in items(3):
            writer.add_item(item)
        manifest = finish_run(writer, started_at=started)
        assert manifest.counts["items"] == expected
        assert read_manifest(store, "github", run_id) == manifest
        assert check_run(store, "github", run_id)["secret_hits"] == 0
    assert store.get("runs/github/r3/_manifest.json") is None
