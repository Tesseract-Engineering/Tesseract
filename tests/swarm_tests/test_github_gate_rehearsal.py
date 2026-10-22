"""Phase 1 gate, rehearsed on fixtures.

The real gate is "a valid manifest-complete run from the club's GitHub repos,
ingested by the graph side, with zero secret hits and 100% ACL coverage". No
source access exists yet, so this runs the whole pipeline on the fake
acme-sbx org and checks the same criteria. Replace with a real run once the
sandbox org exists.
"""

import json
from datetime import datetime, timedelta

import pytest

from swarm import schema
from swarm.records import parse_line
from swarm.run import run_source
from swarm.sources.github import GitHubNormalizer
from swarm.stages.checks import CheckFailed
from swarm.stages.manifest import read_manifest
from swarm.store import LocalStore

from .github_fixture import NOW_ISO, FakeGitHub, connector, planted

NOW = datetime.fromisoformat(NOW_ISO.replace("Z", "+00:00"))


def run(store, fake=None, now=NOW):
    conn, fake = connector(fake, now=now)
    manifest, checks = run_source(conn, GitHubNormalizer(conn.index), store, now=now)
    return manifest, checks, fake


def records(store, manifest):
    out = []
    for name in manifest.files:
        out += [parse_line(line) for line in store.get(f"runs/github/{manifest.run_id}/{name}").decode().splitlines()]
    return out


@pytest.fixture
def first(tmp_path):
    store = LocalStore(tmp_path)
    manifest, checks, _ = run(store)
    return store, manifest, checks


def test_run_is_manifest_complete_with_full_acl_coverage_and_no_secrets(first):
    store, manifest, checks = first
    assert read_manifest(store, "github", manifest.run_id) == manifest
    assert manifest.status == "complete"
    assert manifest.counts == {"items": 9, "extractions": 0, "changes": 0, "groups": 1, "identities": 0, "rejected": 0}
    assert checks == {"items": 9, "written_with_acl": 1.0, "secret_hits": 0}
    assert manifest.acl_coverage == 1.0 and manifest.rejections == {}
    everything = b"".join(store.get(k) for k in store.list(""))
    for token in planted().values():
        assert token.encode() not in everything


def test_output_validates_against_the_published_schemas(first):
    jsonschema = pytest.importorskip("jsonschema")
    store, manifest, _ = first
    for record in records(store, manifest):
        spec = json.loads((schema.OUT / f"{record.record_type}.json").read_text(encoding="utf-8"))
        jsonschema.validate(json.loads(record.model_dump_json()), spec)


def test_in_scope_refs_resolve_to_items(first):
    store, manifest, _ = first
    recs = records(store, manifest)
    item_ids = {r.id for r in recs if r.record_type == "item"}
    github_targets = {ref.target for r in recs if r.record_type == "item" for ref in r.refs
                      if ref.target.startswith("github:")}
    # pr:4 is an old PR outside the backfill window: a forward ref, kept by rule 4.
    assert github_targets - item_ids == {"github:repo:9001:pr:4"}
    groups = {r.id for r in recs if r.record_type == "group"}
    principals = {p for r in recs if r.record_type == "item" for p in r.acl.principals}
    assert principals <= groups


def test_rerunning_the_window_writes_nothing(first):
    store, _, _ = first
    second, checks, _ = run(store, now=NOW + timedelta(hours=1))
    assert second.counts == {"items": 0, "extractions": 0, "changes": 0, "groups": 0, "identities": 0, "rejected": 0}
    assert checks["secret_hits"] == 0


def test_repo_made_public_produces_acl_updates_not_new_items(first):
    store, _, _ = first
    fake = FakeGitHub()
    for repo in fake.routes["GET /installation/repositories"]["repositories"]:
        if repo["id"] == 9001:
            repo.update(private=False, visibility="public")
    second, _, _ = run(store, fake, now=NOW + timedelta(hours=1))
    recs = records(store, second)
    # The repo's own record changes (its meta says public now); everything in
    # it only changed who may read it, so those arrive as acl_updates.
    assert [r.id for r in recs if r.record_type == "item"] == ["github:repo:9001"]
    changes = [r for r in recs if r.record_type == "acl_update"]
    assert len(changes) == 5 and all(c.acl.visibility == "public" for c in changes)


def test_a_leaked_secret_fails_the_run_before_any_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr("swarm.run.redact_payload", lambda payload: (payload, 0))   # pretend redaction broke
    store = LocalStore(tmp_path)
    conn, _ = connector()
    with pytest.raises(CheckFailed):
        run_source(conn, GitHubNormalizer(conn.index), store, now=NOW)
    assert store.list("runs/github/") and not [k for k in store.list("runs/") if k.endswith("_manifest.json")]
