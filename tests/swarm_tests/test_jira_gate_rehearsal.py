"""The Jira pipeline end to end on the fake site, and the sources that link to it.

A night runs GitHub, then Jira, then Slack and Drive. This runs the same
order on the fixtures: Jira resolves GitHub links through GitHub's state,
and the others resolve Jira keys through the keys Jira publishes.
"""

import json
from datetime import timedelta

import pytest

from swarm import schema
from swarm.records import parse_line
from swarm.run import run_source
from swarm.sources.gdrive import DriveNormalizer
from swarm.sources.github import GitHubNormalizer
from swarm.sources.jira import JiraNormalizer
from swarm.sources.links import GitHubLinks, JiraLinks
from swarm.sources.slack import SlackNormalizer
from swarm.stages.manifest import read_manifest
from swarm.stages.redact import redact_payload
from swarm.store import LocalStore

from . import gdrive_fixture, github_fixture, slack_fixture
from .jira_fixture import NOW, connector, planted

SITE = "c0ffee00-1111-2222-3333-444455556666"
ZERO = {"items": 0, "extractions": 0, "changes": 0, "groups": 0, "identities": 0, "rejected": 0}
EPIC = f"jira:{SITE}:issue:10012"


def jira_run(store, now=NOW):
    known = store.get("state/jira/keys.json")
    conn, _ = connector(now=now, known_keys=json.loads(known) if known else None)
    return run_source(conn, JiraNormalizer(conn.index, GitHubLinks.from_store(store)), store, now=now)


def records(store, source, manifest):
    out = []
    for name in manifest.files:
        out += [parse_line(line) for line in store.get(f"runs/{source}/{manifest.run_id}/{name}").decode().splitlines()]
    return out


@pytest.fixture
def first(tmp_path):
    store = LocalStore(tmp_path)
    conn, _ = github_fixture.connector(now=NOW)
    run_source(conn, GitHubNormalizer(conn.index), store, now=NOW)
    manifest, checks = jira_run(store)
    return store, manifest, checks


def test_run_is_manifest_complete_with_full_acl_coverage_and_no_secrets(first):
    store, manifest, checks = first
    assert read_manifest(store, "jira", manifest.run_id) == manifest
    # Two browsers groups (AUTH, OPS) and AUTH's security level.
    assert manifest.counts == {**ZERO, "items": 4, "groups": 3}
    assert checks == {"items": 4, "written_with_acl": 1.0, "secret_hits": 0}
    everything = b"".join(store.get(k) for k in store.list("runs/jira/") + store.list("raw/jira/"))
    assert planted()["slack_bot"].encode() not in everything


def test_output_validates_against_the_published_schemas(first):
    jsonschema = pytest.importorskip("jsonschema")
    store, manifest, _ = first
    for record in records(store, "jira", manifest):
        spec = json.loads((schema.OUT / f"{record.record_type}.json").read_text(encoding="utf-8"))
        jsonschema.validate(json.loads(record.model_dump_json()), spec)


def test_acls_name_groups_that_exist(first):
    store, manifest, _ = first
    recs = records(store, "jira", manifest)
    groups = {r.id for r in recs if r.record_type == "group"}
    assert {p for r in recs if r.record_type == "item" for p in r.acl.principals} <= groups


def test_rerunning_the_window_writes_nothing(first):
    store, _, _ = first
    second, checks = jira_run(store, now=NOW + timedelta(hours=1))
    assert second.counts == ZERO and checks["secret_hits"] == 0


def test_keys_are_published_for_the_other_sources(first):
    store, _, _ = first
    links = JiraLinks.from_store(store)
    assert links.target("AUTH-12") == EPIC
    assert links.target("PLAT-7") == f"jira:{SITE}:issue:10014"       # the key before the move
    assert links.target("NEXP-318") is None


def test_slack_and_drive_link_jira_keys_and_keep_the_rest(first):
    store, _, _ = first
    sl, _ = slack_fixture.api_connector(now=NOW)
    slack, _ = run_source(sl, SlackNormalizer(sl.directory, GitHubLinks.from_store(store), JiraLinks.from_store(store)),
                          store, now=NOW)
    dr, _ = gdrive_fixture.connector(now=NOW)
    drive, _ = run_source(dr, DriveNormalizer(GitHubLinks.from_store(store), JiraLinks.from_store(store)),
                          store, now=NOW)
    for source, manifest in (("slack", slack), ("gdrive", drive)):
        items = [r for r in records(store, source, manifest) if r.record_type == "item"]
        linked = [i for i in items if any(r.target == EPIC and r.via == "key" for r in i.refs)]
        assert linked, f"no {source} item links AUTH-12"
        assert all("AUTH-12" not in i.meta.get("jira_keys", []) for i in items)
        assert any("NEXP-318" in i.meta.get("jira_keys", []) for i in items)   # no such issue: kept in meta


def test_github_links_jira_keys_from_the_last_published_state(first):
    store, _, _ = first
    conn, _ = github_fixture.connector(now=NOW + timedelta(hours=1))
    norm = GitHubNormalizer(conn.index, JiraLinks.from_store(store))
    norm.run_id = "r2"
    for container in conn.enumerate():
        for raw in conn.fetch(container, None):
            if raw.payload["type"] == "file" and raw.payload["path"].startswith("docs/adr/"):
                raw.payload, _ = redact_payload(raw.payload)
                adr = norm.to_item(raw, conn.acl(raw))
    assert ("references", EPIC, "key") in [(r.type, r.target, r.via) for r in adr.refs]
    assert "jira_keys" not in adr.meta
