"""Phase 2 gate for Drive, rehearsed on fixtures.

The real gate is "three sources run nightly; rerunning a window produces zero
new records". No service account exists yet, so this runs the whole Drive
pipeline on the fake acme Drive, into a bucket the GitHub pipeline already
ran into. Replace with real runs once the account is added to a drive.
"""

import json
from datetime import timedelta

import pytest

from swarm import schema
from swarm.records import parse_line
from swarm.run import run_source
from swarm.sources.gdrive import DriveNormalizer
from swarm.sources.github import GitHubNormalizer
from swarm.sources.links import GitHubLinks
from swarm.stages.manifest import read_manifest
from swarm.store import LocalStore

from . import github_fixture
from .gdrive_fixture import NOW, FakeDrive, connector, mutated, planted

ZERO = {"items": 0, "extractions": 0, "changes": 0, "groups": 0, "identities": 0, "rejected": 0}


def drive_run(store, fake=None, now=NOW):
    conn, _ = connector(fake, now=now)
    return run_source(conn, DriveNormalizer(GitHubLinks.from_store(store)), store, now=now)


def records(store, manifest):
    out = []
    for name in manifest.files:
        out += [parse_line(line) for line in store.get(f"runs/gdrive/{manifest.run_id}/{name}").decode().splitlines()]
    return out


@pytest.fixture
def first(tmp_path):
    store = LocalStore(tmp_path)
    conn, _ = github_fixture.connector(now=NOW)
    run_source(conn, GitHubNormalizer(conn.index), store, now=NOW)
    manifest, checks = drive_run(store)
    return store, manifest, checks


def test_run_is_manifest_complete_and_reports_files_without_an_acl(first):
    store, manifest, checks = first
    assert read_manifest(store, "gdrive", manifest.run_id) == manifest
    # The limited-access folder and the note inside it have no readable ACL:
    # not written, and reported rather than fatal.
    assert manifest.counts == {**ZERO, "items": 11, "groups": 1, "rejected": 2}
    assert manifest.rejections == {"MissingAcl": 2}
    assert manifest.acl_coverage == pytest.approx(11 / 13)
    assert checks == {"items": 11, "written_with_acl": 1.0, "secret_hits": 0}
    everything = b"".join(store.get(k) for k in store.list("runs/gdrive/") + store.list("raw/gdrive/"))
    for token in planted().values():
        assert token.encode() not in everything


def test_output_validates_against_the_published_schemas(first):
    jsonschema = pytest.importorskip("jsonschema")
    store, manifest, _ = first
    for record in records(store, manifest):
        spec = json.loads((schema.OUT / f"{record.record_type}.json").read_text(encoding="utf-8"))
        jsonschema.validate(json.loads(record.model_dump_json()), spec)


def test_containers_and_links_resolve(first):
    store, manifest, _ = first
    recs = records(store, manifest)
    items = {r.id for r in recs if r.record_type == "item"}
    assert {r.container for r in recs if r.record_type == "item" and r.container} <= items
    targets = {ref.target for r in recs if r.record_type == "item" for ref in r.refs}
    assert targets == {"github:repo:9001:pr:2", "github:repo:9001:issue:1", "gdrive:file:1SLIDESdeck01"}


def test_acl_groups_are_backed_by_group_records(first):
    store, manifest, _ = first
    recs = records(store, manifest)
    groups = {p for r in recs if r.record_type == "item" for p in r.acl.principals if p.startswith("group:")}
    assert groups == {r.id for r in recs if r.record_type == "group"}


def test_rerunning_the_window_writes_nothing(first):
    store, _, _ = first
    second, checks = drive_run(store, now=NOW + timedelta(hours=1))
    # The two rejected files sit before this window; reconciliation re-reads them.
    assert second.counts == ZERO and checks["secret_hits"] == 0


def test_an_edited_doc_writes_one_new_version(first):
    store, _, _ = first
    data = mutated(**{"1SLIDESdeck01": {"_text": "Cutover moved to Tuesday", "version": "10",
                                        "modifiedTime": "2026-10-21T12:30:00.000Z"}})
    second, _ = drive_run(store, FakeDrive(data), now=NOW + timedelta(hours=1))
    [item] = [r for r in records(store, second) if r.record_type == "item"]
    assert (item.id, item.version, item.text) == ("gdrive:file:1SLIDESdeck01", "10", "Cutover moved to Tuesday")


def test_a_sharing_change_seen_with_an_edit_is_an_acl_update(first):
    store, _, _ = first
    data = mutated(**{"1SLIDESdeck01": {"modifiedTime": "2026-10-21T12:30:00.000Z",
                                        "_permissions": [{"id": "01SAM", "type": "user", "role": "organizer",
                                                          "emailAddress": "sam@acme.com"}]}})
    second, _ = drive_run(store, FakeDrive(data), now=NOW + timedelta(hours=1))
    [change] = [r for r in records(store, second) if r.record_type == "acl_update"]
    assert change.id == "gdrive:file:1SLIDESdeck01" and change.acl.principals == ["user:sam@acme.com"]
    assert not [r for r in records(store, second) if r.record_type == "item"]
