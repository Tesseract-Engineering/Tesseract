"""The write and manifest stages: layout, part files, manifest-last, and
idempotency on a LocalStore."""

import json
from datetime import datetime, timezone

from swarm.interfaces import Checkpoint
from swarm.records import Acl, TombstoneRecord, parse_line
from swarm.stages.acl import MissingAcl, coverage, require_acl
from swarm.stages.manifest import finish_run, read_checkpoints, read_manifest
from swarm.stages.write import RunWriter, new_run_id
from swarm.store import LocalStore

from .test_records import make_item

NOW = datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc)


def run(store, run_id, items, **kw):
    writer = RunWriter(store, "github", run_id, part_size=kw.pop("part_size", 5000))
    outcomes = [writer.add_item(i) for i in items]
    for record in kw.pop("changes", []):
        writer.add(record)
    manifest = finish_run(writer, started_at=NOW, **kw)
    return writer, manifest, outcomes


def items(n, **overrides):
    from swarm import ids
    return [make_item(id=ids.github_issue(7001, i), **overrides) for i in range(1, n + 1)]


def test_layout_and_manifest(tmp_path):
    store = LocalStore(tmp_path)
    _, manifest, _ = run(store, "r1", items(3))
    assert store.list("runs/github/r1/") == ["runs/github/r1/_manifest.json",
                                             "runs/github/r1/items/part-00001.jsonl"]
    assert manifest.counts["items"] == 3 and manifest.files == ["items/part-00001.jsonl"]
    assert read_manifest(store, "github", "r1") == manifest
    lines = store.get("runs/github/r1/items/part-00001.jsonl").decode().splitlines()
    assert [parse_line(line).id for line in lines] == [i.id for i in items(3)]


def test_parts_roll_over(tmp_path):
    store = LocalStore(tmp_path)
    _, manifest, _ = run(store, "r1", items(5), part_size=2)
    assert manifest.files == ["items/part-00001.jsonl", "items/part-00002.jsonl", "items/part-00003.jsonl"]


def test_rerun_of_the_same_window_writes_nothing(tmp_path):
    store = LocalStore(tmp_path)
    tomb = TombstoneRecord(id="github:repo:7001:issue:99", source="github", reason="deleted",
                           detected_at=NOW, run_id="r1")
    run(store, "r1", items(3), changes=[tomb])
    _, second, outcomes = run(store, "r2", items(3), changes=[tomb.model_copy(update={"run_id": "r2"})])
    assert outcomes == ["unchanged"] * 3
    assert second.counts == {"items": 0, "extractions": 0, "changes": 0, "groups": 0, "identities": 0, "rejected": 0}
    assert second.files == []


def test_changed_content_is_a_new_item_and_moved_acl_is_an_acl_update(tmp_path):
    store = LocalStore(tmp_path)
    run(store, "r1", items(2))
    first, second = items(2)
    edited = make_item(id=first.id, text="edited", version="v2")
    reshared = make_item(id=second.id, acl=Acl(visibility="internal"))
    _, manifest, outcomes = run(store, "r2", [edited, reshared])
    assert outcomes == ["item", "acl_update"]
    assert manifest.counts["items"] == 1 and manifest.counts["changes"] == 1
    change = parse_line(store.get("runs/github/r2/changes/part-00001.jsonl").decode().strip())
    assert change.record_type == "acl_update" and change.acl.visibility == "internal"


def test_group_records_are_written_once_until_membership_moves(tmp_path):
    from swarm.records import GroupRecord
    store = LocalStore(tmp_path)

    def group(run_id, members):
        return GroupRecord(id="group:github:repo:7001:readers", source="github", members=members,
                           observed_at=NOW, run_id=run_id)

    counts = []
    for run_id, members in (("r1", ["user:a@acme.com"]), ("r2", ["user:a@acme.com"]),
                            ("r3", ["user:a@acme.com", "user:b@acme.com"])):
        _, manifest, _ = run(store, run_id, [], changes=[group(run_id, members)])
        counts.append(manifest.counts["groups"])
    assert counts == [1, 0, 1]


def test_duplicates_within_a_run_are_written_once(tmp_path):
    store = LocalStore(tmp_path)
    one = items(1)[0]
    _, manifest, outcomes = run(store, "r1", [one, one])
    assert outcomes == ["item", "unchanged"] and manifest.counts["items"] == 1


def test_failed_run_leaves_no_manifest_and_no_state(tmp_path):
    store = LocalStore(tmp_path)
    writer = RunWriter(store, "github", "r1", part_size=1)
    writer.add_item(items(1)[0])                      # flushed as a part, then the run dies
    assert store.list("runs/github/r1/") == ["runs/github/r1/items/part-00001.jsonl"]
    assert read_manifest(store, "github", "r1") is None
    assert store.list("state/") == []
    # The retry with the same window writes everything again.
    _, manifest, outcomes = run(store, "r1", items(1))
    assert outcomes == ["item"] and manifest.counts["items"] == 1


def test_checkpoints_advance_only_with_the_manifest(tmp_path):
    store = LocalStore(tmp_path)
    cp = {"github:repo:7001": Checkpoint("github:repo:7001", "2026-10-20T02:59:00Z", "2026-10-20T03:04:00Z")}
    writer, _, _ = run(store, "r1", items(1), checkpoints=cp)
    assert read_checkpoints(writer) == cp
    assert json.loads(store.get("state/github/seen.json"))["items"]


def test_acl_stage():
    assert require_acl("x", Acl(visibility="public")).visibility == "public"
    try:
        require_acl("x", None)
    except MissingAcl:
        pass
    else:
        raise AssertionError("missing acl was accepted")
    assert coverage(10, 0) == 1.0 and coverage(9, 1) == 0.9


def test_run_id_format():
    assert new_run_id("slack", NOW) == "2026-10-20T03-00-00Z-slack"           # no colons: Windows paths


def test_local_store_refuses_escaping_keys(tmp_path):
    import pytest
    with pytest.raises(ValueError):
        LocalStore(tmp_path).put("../outside", b"x")
