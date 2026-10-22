"""The Slack connectors: scope, windows, threads, permissions, and the export."""

import json
import shutil
import zipfile
from datetime import timedelta

import pytest

from swarm.interfaces import Checkpoint
from swarm.sources.slack.connector import SlackError
from swarm.sources.slack.normalize import ts_time

from .slack_fixture import NOW, FakeSlack, api_connector, export_connector, load, write_export


def fetch_all(conn, since=None):
    out = {}
    for container in conn.enumerate():
        out[container.id] = list(conn.fetch(container, (since or {}).get(container.id)))
    return out


# -- API: scope ------------------------------------------------------------------ #

def test_scope_is_the_channels_the_bot_was_invited_to():
    conn, fake = api_connector()
    assert [c.name for c in conn.enumerate()] == ["#eng-auth", "#general"]          # not #ops
    assert conn.team_id == "T01ACME" and conn.workspace_url == "https://acme.slack.com"
    assert len(fake.calls("users.list")) == 2                                       # 5 users, pages of 3


@pytest.mark.parametrize("narrow", [["general"], ["#general"], ["C10GEN"]])
def test_slack_channels_narrows_scope(narrow):
    conn, _ = api_connector(channels=narrow)
    assert [c.name for c in conn.enumerate()] == ["#general"]


def test_slack_errors_inside_a_200_abort_with_their_code():
    conn, _ = api_connector(FakeSlack(errors={"conversations.history": "not_in_channel"}))
    with pytest.raises(SlackError) as err:
        fetch_all(conn)
    assert err.value.error == "not_in_channel"


# -- API: windows and threads ----------------------------------------------------- #

def test_first_run_backfills_from_midnight_and_fetches_every_thread():
    conn, fake = api_connector(backfill_days=730)
    fetch_all(conn)
    oldest = {r.url.params["channel"]: r.url.params["oldest"] for r in fake.calls("conversations.history")}
    start = (NOW - timedelta(days=730)).replace(hour=0, minute=0, second=0, microsecond=0)
    assert oldest == {"C42AUTH": f"{int(start.timestamp())}.000000", "C10GEN": f"{int(start.timestamp())}.000000"}
    replied = sorted(r.url.params["ts"] for r in fake.calls("conversations.replies") if "cursor" not in r.url.params)
    assert replied == ["1785578400.000100", "1791986400.000100"]


def test_checkpoint_holds_the_newest_ts_and_active_threads():
    conn, _ = api_connector()
    fetch_all(conn)
    cursor = json.loads(conn.checkpoint(next(iter(conn.enumerate()))).cursor)
    assert cursor == {"ts": "1792310400.000300",
                      "active": {"1785578400.000100": "1792310400.000300", "1791986400.000100": "1791986970.000400"}}


def test_next_run_reads_from_midnight_a_week_before_the_checkpoint_and_rereads_active_threads():
    data = load()
    # A new reply to the August thread, whose parent is far outside the window.
    data["messages"]["C42AUTH"].append({"type": "message", "user": "U456PRI", "ts": "1792573200.000500",
                                        "thread_ts": "1785578400.000100", "text": "Yes, still relevant."})
    cursor = {"ts": "1792310400.000300", "active": {"1785578400.000100": "1792310400.000300",
                                                    "1700000000.000100": "1700000100.000100"}}
    since = {"slack:T01ACME:channel:C42AUTH": Checkpoint("slack:T01ACME:channel:C42AUTH", json.dumps(cursor), "x")}
    conn, fake = api_connector(FakeSlack(data), now=NOW + timedelta(days=1))
    raws = fetch_all(conn, since)["slack:T01ACME:channel:C42AUTH"]
    history = next(r for r in fake.calls("conversations.history") if r.url.params["channel"] == "C42AUTH")
    start = ts_time("1792310400.000300") - timedelta(days=7)
    assert history.url.params["oldest"] == f"{int(start.replace(hour=0, minute=0, second=0).timestamp())}.000000"
    replied = {r.url.params["ts"] for r in fake.calls("conversations.replies")}
    assert "1785578400.000100" in replied                       # still active: reread
    assert "1700000000.000100" not in replied                   # quiet for over 30 days: dropped
    old = next(r for r in raws if r.native_id == "thread-C42AUTH-1785578400.000100")
    assert [m["ts"] for m in old.payload["messages"]][-1] == "1792573200.000500"


def test_a_deleted_thread_is_skipped_not_fatal():
    cursor = {"ts": "1792310400.000300", "active": {"1792300000.000100": "1792310000.000100"}}
    since = {"slack:T01ACME:channel:C42AUTH": Checkpoint("slack:T01ACME:channel:C42AUTH", json.dumps(cursor), "x")}
    conn, fake = api_connector()
    fetch_all(conn, since)
    assert "1792300000.000100" in {r.url.params["ts"] for r in fake.calls("conversations.replies")}


def test_raw_payloads_keep_only_the_fields_the_normalizer_reads():
    conn, _ = api_connector()
    raws = fetch_all(conn)["slack:T01ACME:channel:C42AUTH"]
    thread = next(r for r in raws if r.native_id == "thread-C42AUTH-1791986400.000100")
    parent = thread.payload["messages"][0]
    assert "blocks" not in parent and "reply_users" not in parent and "team" not in parent


# -- permissions ---------------------------------------------------------------- #

def test_public_channels_are_internal_and_private_ones_their_members():
    conn, _ = api_connector()
    raws = fetch_all(conn)
    private = raws["slack:T01ACME:channel:C42AUTH"][0]
    public = raws["slack:T01ACME:channel:C10GEN"][0]
    assert conn.acl(public).visibility == "internal" and conn.acl(public).principals == []
    assert conn.acl(private).principals == ["group:slack:T01ACME:channel:C42AUTH:members"]


def test_members_group_uses_emails_then_slack_ids_and_counts_strangers():
    conn, _ = api_connector()
    containers = {c.id: c for c in conn.enumerate()}
    [group] = conn.groups(containers["slack:T01ACME:channel:C42AUTH"], run_id="r1")
    # The bot and the deactivated member are not readers; U999GONE is unknown.
    assert group.members == ["person:slack:U789JO", "user:priya@acme.com", "user:sam@acme.com"]
    assert group.unresolved == 1 and group.name == "#eng-auth members"
    assert conn.groups(containers["slack:T01ACME:channel:C10GEN"], run_id="r1") == []


def test_list_ids_names_the_channel_and_every_conversation():
    conn, _ = api_connector()
    container = next(iter(conn.enumerate()))
    listed = list(conn.list_ids(container))
    assert listed[0] == "slack:T01ACME:channel:C42AUTH"
    assert "slack:T01ACME:thread:C42AUTH:1791986400.000100" in listed and len(listed) == 7


# -- export ----------------------------------------------------------------------- #

@pytest.fixture
def export_dir(tmp_path):
    return write_export(tmp_path / "export")


def test_export_reads_every_channel_and_never_dms(export_dir):
    conn = export_connector(export_dir)
    raws = fetch_all(conn)
    assert sorted(raws) == ["slack:T01ACME:channel:C10GEN", "slack:T01ACME:channel:C42AUTH",
                            "slack:T01ACME:channel:C77OPS"]
    assert conn.team_id == "T01ACME"                                   # from users.json
    everything = json.dumps([r.payload for rs in raws.values() for r in rs])
    assert "private DM" not in everything


def test_export_reads_a_zip_with_a_top_folder(export_dir, tmp_path):
    wrapped = tmp_path / "wrapped" / "Acme Slack export"
    shutil.copytree(export_dir, wrapped)
    archive = tmp_path / "export.zip"
    with zipfile.ZipFile(archive, "w") as z:
        for path in wrapped.rglob("*.json"):
            z.write(path, path.relative_to(wrapped.parent).as_posix())
    from_zip = {k: [r.payload for r in v] for k, v in fetch_all(export_connector(archive)).items()}
    from_dir = {k: [r.payload for r in v] for k, v in fetch_all(export_connector(export_dir)).items()}
    assert from_zip == from_dir


def test_export_needs_a_workspace_url(export_dir):
    from swarm.sources.slack import SlackExportConnector
    with pytest.raises(ValueError, match="SLACK_WORKSPACE_URL"):
        SlackExportConnector(export_dir, workspace_url="")


def test_export_and_api_build_the_same_payloads(export_dir):
    api, _ = api_connector()
    from_api = {r.native_id: r.payload for rs in fetch_all(api).values() for r in rs}
    from_export = {r.native_id: r.payload for rs in fetch_all(export_connector(export_dir)).values() for r in rs}
    shared = from_api.keys() & from_export.keys()
    assert shared == set(from_api)                                      # the export has #ops too
    assert {k: from_export[k] for k in shared} == from_api


def test_export_members_match_the_api(export_dir):
    api, _ = api_connector()
    export = export_connector(export_dir)
    a = {c.id: c for c in api.enumerate()}
    e = {c.id: c for c in export.enumerate()}
    cid = "slack:T01ACME:channel:C42AUTH"
    assert export.groups(e[cid], run_id="r") == api.groups(a[cid], run_id="r")
