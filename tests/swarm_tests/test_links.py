"""Cross-source links, and the GitHub state they resolve through."""

import json

from swarm.run import run_source
from swarm.sources.github import GitHubNormalizer
from swarm.sources.links import GitHubLinks, scan
from swarm.store import LocalStore

from .github_fixture import connector

GITHUB = GitHubLinks(repos={"acme-sbx/api": 9001}, kinds={(9001, 1): "issue", (9001, 2): "pull_request"})


def targets(text, **kw):
    return [(r.target, r.via) for r in scan(text, GITHUB, **kw).refs]


def test_github_keys_and_urls_resolve_when_granted_and_known():
    assert targets("see acme-sbx/api#2 and https://github.com/acme-sbx/api/issues/1") == [
        ("github:repo:9001:issue:1", "url"), ("github:repo:9001:pr:2", "key")]
    assert targets("https://github.com/Acme-Sbx/API/commit/3f9c2e1a") == [("github:repo:9001:commit:3f9c2e1a", "url")]


def test_unresolved_github_links_are_kept_in_meta():
    found = scan("acme-sbx/api#77, other/repo#1, https://github.com/other/repo/pull/3", GITHUB)
    assert found.refs == []
    assert found.meta() == {"github_links": ["acme-sbx/api#77", "https://github.com/other/repo/pull/3",
                                             "other/repo#1"]}


def test_drive_links_always_resolve_but_never_to_the_item_itself():
    text = ("https://docs.google.com/document/d/1AbCdEfGhIjK/edit and "
            "https://drive.google.com/drive/folders/0FoLdErIdXyZ1")
    assert targets(text) == [("gdrive:file:1AbCdEfGhIjK", "url"), ("gdrive:file:0FoLdErIdXyZ1", "url")]
    assert targets(text, own_id="gdrive:file:1AbCdEfGhIjK") == [("gdrive:file:0FoLdErIdXyZ1", "url")]


def test_code_and_quotes_link_nothing():
    assert targets("`acme-sbx/api#2`\n> acme-sbx/api#1\n```\nacme-sbx/api#2\n```") == []


def test_jira_keys_and_slack_permalinks_go_to_meta():
    found = scan("NEXP-318 and UTF-8, per ADR-014, a SEV-2, https://acme.slack.com/archives/C1/p1792054800000100", GITHUB)
    assert found.meta() == {"jira_keys": ["NEXP-318"],
                            "slack_links": ["https://acme.slack.com/archives/C1/p1792054800000100"]}


def test_github_runs_publish_what_other_sources_resolve_through(tmp_path):
    store = LocalStore(tmp_path)
    conn, _ = connector()
    run_source(conn, GitHubNormalizer(conn.index), store)
    assert json.loads(store.get("state/github/repos.json")) == {"acme-sbx/api": 9001, "acme-sbx/web": 9002}
    links = GitHubLinks.from_store(store)
    assert links.repos == {"acme-sbx/api": 9001, "acme-sbx/web": 9002}
    assert links.kinds[(9001, 2)] == "pull_request" and links.kinds[(9001, 1)] == "issue"


def test_an_empty_bucket_resolves_nothing():
    links = GitHubLinks.from_store(LocalStore("/nonexistent-swarm-bucket"))
    assert links.repos == {} and links.kinds == {}
