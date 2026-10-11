"""The approval door: nothing runs until the exact payload is approved, and
execution only ever writes drafts to the outbox."""

import json

import pytest

from tesseract.actions import Door, DoorError

EMAIL = {"id": "a1", "type": "email.draft", "to": "jane@example.com",
         "payload": {"to": ["jane@example.com"], "subject": "s", "body": "b", "citations": ["doc:x"]}}
SLACK = {"id": "a2", "type": "slack.draft", "to": "@jane", "payload": {"to": ["@jane"], "text": "hi"}}
AGENT = {"id": "a3", "type": "agent.launch", "to": "services/billing",
         "payload": {"title": "Build on billing", "repo": "services/billing", "steps": ["Read"]}}


@pytest.fixture
def door(tmp_path):
    return Door(db_path=tmp_path / "actions.sqlite", outbox=tmp_path / "outbox")


def test_unknown_or_malformed_actions_are_refused(door):
    for bad in ([], [{"id": "a1", "type": "rm.rf", "payload": {}}], [{"type": "email.draft", "payload": {}}],
                [dict(EMAIL), dict(EMAIL)], [{"id": "a1", "type": "email.draft"}]):
        with pytest.raises(DoorError) as e:
            door.propose(bad)
        assert e.value.code == "bad_action"


def test_nothing_executes_before_approval(door):
    pid = door.propose([EMAIL])
    with pytest.raises(DoorError) as e:
        door.execute(pid, "a1")
    assert e.value.code == "not_approved"
    assert not (door.outbox / "email").exists()


def test_approving_a_different_payload_rejects_the_action(door):
    pid = door.propose([EMAIL])
    with pytest.raises(DoorError) as e:
        door.approve(pid, "a1", "jane", payload={**EMAIL["payload"], "to": ["attacker@example.com"]})
    assert e.value.code == "hash_mismatch"
    assert door.proposal(pid)["actions"][0]["state"] == "rejected"
    with pytest.raises(DoorError):
        door.approve(pid, "a1", "jane")


def test_approved_actions_write_drafts_to_the_outbox_and_reset_removes_them(door):
    pid = door.propose([EMAIL, SLACK, AGENT])
    for aid in ("a1", "a2", "a3"):
        assert door.approve(pid, aid, "jane", payload=None)["state"] == "approved"
        receipt = door.execute(pid, aid)
        assert receipt["state"] == "executed"
    email = json.loads((door.outbox / receipt_path(door, pid, "a1")).read_text())
    assert email["status"] == "draft" and email["to"] == ["jane@example.com"]
    assert email["from"]["approved_by"] == "jane" and email["citations"] == ["doc:x"]
    assert (door.outbox / "slack" / f"{pid}-a2.json").is_file()
    assert json.loads((door.outbox / "agent" / f"{pid}-a3.json").read_text())["brief"]["repo"] == "services/billing"
    assert door.execute(pid, "a1")["state"] == "executed"           # idempotent
    assert len(door.receipts()) == 3

    out = door.reset()
    assert sorted(out["deleted"]) == sorted([f"email/{pid}-a1.json", f"slack/{pid}-a2.json", f"agent/{pid}-a3.json"])
    assert not any(door.outbox.rglob("*.json"))


def test_rejected_actions_never_run(door):
    pid = door.propose([EMAIL])
    assert door.reject(pid, "a1", "jane", "wrong person")["state"] == "rejected"
    with pytest.raises(DoorError):
        door.execute(pid, "a1")


def test_default_door_uses_the_configured_state_and_outbox(tmp_path):
    d = Door()
    assert d.db_path == str(tmp_path / "state" / "actions.sqlite")
    assert d.outbox == tmp_path / "state" / "outbox"


def receipt_path(door, pid, aid):
    return next(a for a in door.receipts() if a["action_id"] == aid and a["proposal_id"] == pid)["result_path"]
