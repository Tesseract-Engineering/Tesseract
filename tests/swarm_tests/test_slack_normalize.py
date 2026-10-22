"""SlackNormalizer: grouping into threads and bursts, and items from them."""

import pytest

from swarm.records import parse_line
from swarm.sources.links import GitHubLinks
from swarm.sources.slack import SlackNormalizer
from swarm.sources.slack.normalize import group_conversations, permalink, ts_key, ts_time
from swarm.stages.redact import redact_payload

from .slack_fixture import api_connector, planted

GITHUB = GitHubLinks(repos={"acme-sbx/api": 9001}, kinds={(9001, 1): "issue", (9001, 2): "pull_request"})


def msg(ts, text="hi", user="U1", **extra):
    return {"ts": ts, "user": user, "text": text, **extra}


# -- grouping ------------------------------------------------------------------ #

def anchors(messages):
    return [(c.grouping, c.anchor, [m["ts"] for m in c.messages]) for c in group_conversations(messages)]


def test_bursts_split_on_a_gap_over_20_minutes():
    t = 1792054800                                            # 2026-10-15 09:00 UTC
    out = anchors([msg(f"{t}.000001"), msg(f"{t + 600}.000002"), msg(f"{t + 1800}.000002"),
                   msg(f"{t + 3001}.000002")])
    assert out == [("burst", f"{t}.000001", [f"{t}.000001", f"{t + 600}.000002", f"{t + 1800}.000002"]),
                   ("burst", f"{t + 3001}.000002", [f"{t + 3001}.000002"])]


def test_bursts_never_cross_midnight_utc():
    midnight = 1792195200                                     # 2026-10-17 00:00 UTC
    out = anchors([msg(f"{midnight - 60}.000001"), msg(f"{midnight + 60}.000002")])
    assert [a for _, a, _ in out] == [f"{midnight - 60}.000001", f"{midnight + 60}.000002"]


def test_threads_take_their_replies_and_leave_the_flow():
    out = anchors([msg("100.000001"), msg("100.000002", thread_ts="100.000002"),
                   msg("150.000003", thread_ts="100.000002"), msg("200.000004")])
    assert out == [("burst", "100.000001", ["100.000001", "200.000004"]),
                   ("thread", "100.000002", ["100.000002", "150.000003"])]


def test_broadcasts_count_once_and_system_messages_are_dropped():
    reply = msg("150.000003", thread_ts="100.000002", subtype="thread_broadcast")
    out = anchors([msg("100.000002", reply_count=1), reply, dict(reply),
                   msg("120.000005", subtype="channel_join"), msg("130.000006", subtype="tombstone")])
    assert out == [("thread", "100.000002", ["100.000002", "150.000003"])]


def test_a_thread_whose_parent_is_gone_keeps_its_id():
    out = anchors([msg("150.000003", thread_ts="100.000002")])
    assert out == [("thread", "100.000002", ["150.000003"])]


def test_ts_helpers_are_exact():
    assert ts_key("1792054800.000100") < ts_key("1792054800.0002")
    assert ts_time("1792054800.000100").isoformat() == "2026-10-15T09:00:00.000100+00:00"
    assert permalink("https://acme.slack.com", "C1", "1792054800.000100") == \
        "https://acme.slack.com/archives/C1/p1792054800000100"


# -- items --------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def items():
    conn, _ = api_connector()
    norm = SlackNormalizer(conn.directory, GITHUB)
    norm.run_id = "r1"
    out = {}
    for container in conn.enumerate():
        for raw in conn.fetch(container, None):
            raw.payload, _ = redact_payload(raw.payload)
            item = norm.to_item(raw, conn.acl(raw))
            out[item.id] = item
    return out


THREAD = "slack:T01ACME:thread:C42AUTH:1791986400.000100"


def test_every_item_round_trips_through_the_contract(items):
    assert sorted({i.kind for i in items.values()}) == ["channel", "thread"]
    for item in items.values():
        assert parse_line(item.model_dump_json()) == item


def test_a_thread_folds_its_replies_with_speakers(items):
    t = items[THREAD]
    assert t.text.splitlines()[0] == ("Sam Lee: SAML keeps breaking behind the IdP proxy. Proposal: we use OIDC "
                                      "instead of SAML for staff login.")
    assert "Jo Kim: Heads up @here:" in t.text
    assert "by Friday & share it here." in t.text                     # entities unescaped
    assert t.meta["grouping"] == "thread" and t.meta["reply_count"] == 3
    assert t.container == "slack:T01ACME:channel:C42AUTH"
    assert t.url == "https://acme.slack.com/archives/C42AUTH/p1791986400000100" and t.aliases == [t.url]


def test_an_edit_moves_the_version(items):
    t = items[THREAD]
    assert t.version == "1791987600.000000"
    assert t.updated_at.isoformat() == "2026-10-14T14:20:00+00:00"
    assert t.created_at.isoformat() == "2026-10-14T14:00:00.000100+00:00"


def test_people_carry_roles_names_and_emails(items):
    t = items[THREAD]
    people = {p.handle: p for p in t.people}
    assert people["U123SAM"].roles == ["author", "participant"]
    assert people["U123SAM"].email == "sam@acme.com" and people["U123SAM"].name == "Sam Lee"
    assert people["U789JO"].email is None
    burst = items["slack:T01ACME:thread:C42AUTH:1792054800.000100"]
    assert {p.handle: p.roles for p in burst.people}["U123SAM"] == ["mentioned", "participant"]
    assert "@Sam Lee is on the rollout" in burst.text


def test_bots_are_named_in_text_and_meta_never_as_people(items):
    burst = items["slack:T01ACME:thread:C42AUTH:1792195500.000200"]
    assert "deploybot: Deployed api@3f9c2e1" in burst.text
    assert burst.meta["bots"] == ["deploybot"]
    assert [p.handle for p in burst.people] == ["U123SAM"]


def test_refs_from_text(items):
    t = items[THREAD]
    assert {(r.target, r.via) for r in t.refs} == {("github:repo:9001:pr:2", "key"),
                                                   ("gdrive:file:1AbCdEfGhIjK", "url")}
    burst = items["slack:T01ACME:thread:C42AUTH:1792054800.000100"]
    assert [(r.target, r.via) for r in burst.refs] == [("slack:T01ACME:channel:C10GEN", "mention")]
    old = items["slack:T01ACME:thread:C42AUTH:1785578400.000100"]
    assert [(r.target, r.via) for r in old.refs] == [(THREAD, "url")]
    # A link to one message may sit inside a burst: kept, not guessed.
    assert old.meta["slack_links"] == ["https://acme.slack.com/archives/C42AUTH/p1792055400000200"]
    assert old.meta["jira_keys"] == ["AUTH-12"]


def test_github_links_outside_the_grant_stay_in_meta(items):
    general = items["slack:T01ACME:thread:C10GEN:1792144800.000100"]
    assert [r.target for r in general.refs] == ["github:repo:9001:issue:1"]
    assert general.meta["github_links"] == ["https://github.com/other-org/thing/pull/9"]
    assert general.meta["jira_keys"] == ["NEXP-318"]


def test_secrets_are_gone_and_recorded(items):
    t = items[THREAD]
    assert planted()["slack_bot"] not in t.text
    assert [(r.field, r.type, r.count) for r in t.redactions] == [("text", "Slack Token", 1)]


def test_channels_become_items_owned_by_their_creator(items):
    ch = items["slack:T01ACME:channel:C42AUTH"]
    assert ch.title == "#eng-auth" and ch.aliases == ["#eng-auth"] and ch.container is None
    assert ch.text == "Auth migration and the IdP proxy\n\nOIDC rollout. Spec: OIDC spec " \
                      "(https://docs.google.com/document/d/1AbCdEfGhIjK/edit)"
    assert [(p.handle, p.roles) for p in ch.people] == [("U123SAM", ["owner"])]
    assert ch.meta == {"is_private": True, "is_archived": False}
    assert ch.version == "1788256800"


def test_long_first_lines_make_short_titles(items):
    general = items["slack:T01ACME:thread:C10GEN:1792144800.000100"]
    assert len(general.title) <= 121 and general.title.endswith("…")
