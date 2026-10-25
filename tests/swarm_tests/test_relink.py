"""Links found later are never lost, whichever source ran first.

Each source resolves links to the others through what they published in the
bucket, so the first to run cannot link to the rest. These run the sources
in the worst order, then run the early ones again with nothing new to fetch,
so only relink can link them.
"""

import json
from datetime import timedelta

from swarm.records import ItemRecord, parse_line
from swarm.run import run_source
from swarm.sources.github import GitHubNormalizer
from swarm.sources.github.connector import read_known_docs
from swarm.sources.jira import JiraNormalizer
from swarm.sources.links import GitHubLinks, JiraLinks
from swarm.sources.slack import SlackNormalizer
from swarm.store import LocalStore

from . import github_fixture, jira_fixture, slack_fixture
from .test_extract import Scripted

NOW = jira_fixture.NOW
LATER = NOW + timedelta(hours=1)
EPIC = "jira:c0ffee00-1111-2222-3333-444455556666:issue:10012"


def quiet(conn):
    """The source with nothing changed since the last run: scope as before, nothing to fetch."""
    conn.fetch = lambda container, since: iter(())
    return conn


def github(store, now=NOW, model=None, *, idle=False):
    conn, _ = github_fixture.connector(now=now, known_docs=read_known_docs(store))
    conn = quiet(conn) if idle else conn
    return run_source(conn, GitHubNormalizer(conn.index, JiraLinks.from_store(store)), store, now=now, model=model)[0]


def jira(store, now=NOW, *, idle=False):
    known = store.get("state/jira/keys.json")
    conn, _ = jira_fixture.connector(now=now, known_keys=json.loads(known) if known else None)
    conn = quiet(conn) if idle else conn
    return run_source(conn, JiraNormalizer(conn.index, GitHubLinks.from_store(store)), store, now=now)[0]


def slack(store, now=NOW, *, idle=False):
    conn, _ = slack_fixture.api_connector(now=now)
    conn = quiet(conn) if idle else conn
    norm = SlackNormalizer(conn.directory, GitHubLinks.from_store(store), JiraLinks.from_store(store))
    return run_source(conn, norm, store, now=now)[0]


def items(store, source, manifest):
    out = {}
    for name in manifest.files:
        for line in store.get(f"runs/{source}/{manifest.run_id}/{name}").decode().splitlines():
            record = parse_line(line)
            if record.record_type == "item":
                out[record.id] = record
    return out


def unresolved(store, source):
    return json.loads(store.get(f"state/{source}/unresolved.json") or b"{}")


def test_slack_run_before_everything_is_linked_by_its_next_run(tmp_path):
    store = LocalStore(tmp_path)
    first = slack(store)                                 # no GitHub or Jira state yet
    early = items(store, "slack", first)
    assert any("AUTH-12" in i.meta.get("jira_keys", []) for i in early.values())
    assert unresolved(store, "slack")
    github(store)
    jira(store)
    later = slack(store, now=LATER, idle=True)
    linked = items(store, "slack", later)
    assert linked and set(linked) <= set(early)          # nothing was fetched: these are relinks
    for item in linked.values():
        assert "AUTH-12" not in item.meta.get("jira_keys", [])
        assert item.version == early[item.id].version and item.text == early[item.id].text
    assert any(r.target == EPIC and r.via == "key" for i in linked.values() for r in i.refs)
    assert slack(store, now=LATER + timedelta(hours=1), idle=True).counts["items"] == 0   # once is enough


def test_a_github_doc_that_never_changes_still_gets_its_jira_link(tmp_path):
    store = LocalStore(tmp_path)
    github(store)
    adr = "github:repo:9001:file:docs/adr/0002%20use%20oidc.md"
    assert adr in unresolved(store, "github")
    jira(store)
    second = github(store, now=LATER)                    # the doc's blob is unchanged, so it is not read again
    doc = items(store, "github", second)[adr]
    assert ("references", EPIC, "key") in [(r.type, r.target, r.via) for r in doc.refs]
    assert adr not in unresolved(store, "github")


def test_jira_run_before_github_links_github_on_its_next_run(tmp_path):
    store = LocalStore(tmp_path)
    jira(store)
    assert unresolved(store, "jira")                     # the epic's PR link: GitHub has not run
    github(store)
    later = jira(store, now=LATER, idle=True)
    epic = items(store, "jira", later)[EPIC]
    assert ("references", "github:repo:9001:pr:2", "url") in [(r.type, r.target, r.via) for r in epic.refs]
    assert "github_links" not in epic.meta


def test_a_stored_item_replaced_since_is_dropped_not_relinked(tmp_path):
    store = LocalStore(tmp_path)
    slack(store)
    state = unresolved(store, "slack")
    item_id, line = next(iter(state.items()))
    stale = ItemRecord.model_validate_json(line).model_copy(update={"content_hash": "sha256:" + "0" * 64})
    state[item_id] = stale.model_dump_json()
    store.put("state/slack/unresolved.json", json.dumps(state).encode())
    github(store)
    jira(store)
    later = slack(store, now=LATER, idle=True)
    assert item_id not in items(store, "slack", later) and item_id not in unresolved(store, "slack")


def test_a_run_with_no_model_leaves_extraction_to_the_next_run_with_one(tmp_path):
    store = LocalStore(tmp_path)
    github(store)                                        # no model
    backlog = json.loads(store.get("state/github/backlog.json"))
    assert "github:repo:9001:pr:2" in backlog and "github:repo:9001:repo" not in backlog
    model = Scripted()
    later = github(store, now=LATER, model=model, idle=True)
    assert set(model.calls) == set(backlog) and later.counts["extractions"] == len(backlog)
    assert json.loads(store.get("state/github/backlog.json")) == {}


def test_relinking_asks_the_jira_api_nothing(tmp_path):
    from swarm.records import content_hash
    store = LocalStore(tmp_path)
    jira(store)
    # Plant a key no Jira project has, as a stored unresolved item the bucket agrees with.
    state, seen = unresolved(store, "jira"), json.loads(store.get("state/jira/seen.json"))
    epic = ItemRecord.model_validate_json(state[EPIC])
    meta = {**epic.meta, "jira_keys": ["NOPE-7"]}
    planted = epic.model_copy(update={"meta": meta, "content_hash": content_hash(
        title=epic.title, text=epic.text, container=epic.container, people=epic.people, refs=epic.refs, meta=meta)})
    state[EPIC], seen["items"][EPIC][0] = planted.model_dump_json(), planted.content_hash
    store.put("state/jira/unresolved.json", json.dumps(state).encode())
    store.put("state/jira/seen.json", json.dumps(seen).encode())
    github(store)
    conn, fake = jira_fixture.connector(now=LATER, known_keys=json.loads(store.get("state/jira/keys.json")))
    later = run_source(quiet(conn), JiraNormalizer(conn.index, GitHubLinks.from_store(store)), store, now=LATER)[0]
    assert not [p for p in fake.paths() if p.startswith("/rest/api/3/issue/")]
    epic = items(store, "jira", later)[EPIC]                 # its PR link resolved; NOPE-7 stays a key
    assert epic.meta["jira_keys"] == ["NOPE-7"] and "github_links" not in epic.meta
