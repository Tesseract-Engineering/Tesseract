"""Phase 2 gate for Slack, rehearsed on fixtures.

The real gate is "three sources run nightly; rerunning a window produces zero
new records". No workspace access exists yet, so this runs the whole Slack
pipeline on the fake acme workspace: a cold start from the export, then API
runs into the same bucket. Replace with real runs once the app is installed.
"""

import json
from datetime import timedelta

import pytest

from swarm import schema
from swarm.records import parse_line
from swarm.run import run_source
from swarm.sources.github import GitHubNormalizer
from swarm.sources.links import GitHubLinks
from swarm.sources.slack import SlackNormalizer
from swarm.stages.manifest import read_manifest
from swarm.store import LocalStore

from . import github_fixture
from .slack_fixture import NOW, FakeSlack, api_connector, export_connector, load, planted, write_export

ZERO = {"items": 0, "extractions": 0, "changes": 0, "groups": 0, "identities": 0, "rejected": 0}


def slack_run(store, conn, now):
    return run_source(conn, SlackNormalizer(conn.directory, GitHubLinks.from_store(store)), store, now=now)


def records(store, manifest):
    out = []
    for name in manifest.files:
        out += [parse_line(line) for line in store.get(f"runs/slack/{manifest.run_id}/{name}").decode().splitlines()]
    return out


@pytest.fixture
def store(tmp_path):
    """A bucket the GitHub pipeline already ran into, as on a real night."""
    store = LocalStore(tmp_path / "bucket")
    conn, _ = github_fixture.connector(now=NOW)
    run_source(conn, GitHubNormalizer(conn.index), store, now=NOW)
    return store


@pytest.fixture
def first(store):
    conn, _ = api_connector()
    manifest, checks = slack_run(store, conn, NOW)
    return store, manifest, checks


def test_run_is_manifest_complete_with_full_acl_coverage_and_no_secrets(first):
    store, manifest, checks = first
    assert read_manifest(store, "slack", manifest.run_id) == manifest
    assert manifest.counts == {**ZERO, "items": 9, "groups": 1}
    assert checks == {"items": 9, "acl_coverage": 1.0, "secret_hits": 0}
    everything = b"".join(store.get(k) for k in store.list("runs/slack/") + store.list("raw/slack/"))
    for token in planted().values():
        assert token.encode() not in everything


def test_output_validates_against_the_published_schemas(first):
    jsonschema = pytest.importorskip("jsonschema")
    store, manifest, _ = first
    for record in records(store, manifest):
        spec = json.loads((schema.OUT / f"{record.record_type}.json").read_text(encoding="utf-8"))
        jsonschema.validate(json.loads(record.model_dump_json()), spec)


def test_links_to_github_resolve_through_the_github_run(first):
    store, manifest, _ = first
    github_items = set()
    for key in store.list("runs/github/"):
        if key.endswith(".jsonl"):
            github_items |= {parse_line(line).id for line in store.get(key).decode().splitlines()}
    targets = {ref.target for r in records(store, manifest) if r.record_type == "item" for ref in r.refs
               if ref.target.startswith("github:")}
    assert targets == {"github:repo:9001:pr:2", "github:repo:9001:issue:1"}
    assert targets <= github_items


def test_acl_principals_are_all_backed_by_groups(first):
    store, manifest, _ = first
    recs = records(store, manifest)
    principals = {p for r in recs if r.record_type == "item" for p in r.acl.principals}
    assert principals == {r.id for r in recs if r.record_type == "group"}


def test_rerunning_the_window_writes_nothing(first):
    store, _, _ = first
    conn, _ = api_connector(now=NOW + timedelta(hours=1))
    second, checks = slack_run(store, conn, NOW + timedelta(hours=1))
    assert second.counts == ZERO and checks["secret_hits"] == 0


def test_a_new_reply_writes_only_its_thread(first):
    store, _, _ = first
    data = load()
    data["messages"]["C42AUTH"].append({"type": "message", "user": "U456PRI", "ts": "1792573200.000500",
                                        "thread_ts": "1785578400.000100", "text": "Yes, still relevant."})
    later = NOW + timedelta(hours=1)
    conn, _ = api_connector(FakeSlack(data), now=later)
    second, _ = slack_run(store, conn, later)
    assert [r.id for r in records(store, second)] == ["slack:T01ACME:thread:C42AUTH:1785578400.000100"]


def test_a_channel_made_public_moves_acls_not_content(first):
    store, _, _ = first
    data = load()
    data["channels"][0]["is_private"] = False
    later = NOW + timedelta(hours=1)
    conn, _ = api_connector(FakeSlack(data), now=later)
    second, _ = slack_run(store, conn, later)
    recs = records(store, second)
    # The channel's own record changes (its meta says public); its threads
    # only changed who may read them.
    assert [r.id for r in recs if r.record_type == "item"] == ["slack:T01ACME:channel:C42AUTH"]
    changes = [r for r in recs if r.record_type == "acl_update"]
    assert len(changes) == 6 and all(c.acl.visibility == "internal" for c in changes)


def test_cold_start_from_the_export_then_the_api_writes_nothing_new(store, tmp_path):
    export = write_export(tmp_path / "export")
    cold, checks = slack_run(store, export_connector(export, channels=["eng-auth", "general"]), NOW)
    assert cold.counts["items"] == 9 and checks["acl_coverage"] == 1.0
    conn, fake = api_connector(now=NOW + timedelta(hours=6))
    nightly, _ = slack_run(store, conn, NOW + timedelta(hours=6))
    assert nightly.counts == ZERO
    # ...and it continued from the export's checkpoint rather than backfilling two years.
    oldest = min(float(r.url.params["oldest"]) for r in fake.calls("conversations.history"))
    assert oldest > (NOW - timedelta(days=30)).timestamp()
