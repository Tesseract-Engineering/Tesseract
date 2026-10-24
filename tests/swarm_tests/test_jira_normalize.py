"""JiraNormalizer: each issue becomes a valid item record."""

import pytest

from swarm.records import parse_line
from swarm.sources.jira import JiraNormalizer
from swarm.sources.links import GitHubLinks
from swarm.stages.redact import redact_payload

from .github_fixture import planted
from .jira_fixture import connector

SITE = "c0ffee00-1111-2222-3333-444455556666"
GITHUB = GitHubLinks(repos={"acme-sbx/api": 9001}, kinds={(9001, 2): "pull_request"})


def issue_id(n):
    return f"jira:{SITE}:issue:{n}"


@pytest.fixture(scope="module")
def items():
    conn, _ = connector()
    norm = JiraNormalizer(conn.index, GITHUB)
    norm.run_id = "r1"
    out = {}
    for container in conn.enumerate():
        for raw in conn.fetch(container, None):
            raw.payload, _ = redact_payload(raw.payload)
            item = norm.to_item(raw, conn.acl(raw))
            out[item.meta["key"]] = item
    return out


def test_every_issue_round_trips_through_the_contract(items):
    assert sorted(items) == ["AUTH-12", "AUTH-13", "AUTH-14", "OPS-3"]
    for item in items.values():
        assert parse_line(item.model_dump_json()) == item


def test_an_epic_carries_its_text_people_and_place(items):
    epic = items["AUTH-12"]
    assert epic.id == issue_id("10012") and epic.kind == "issue" and epic.source == "jira"
    assert epic.container == f"jira:{SITE}:project:10000" and epic.aliases == ["AUTH-12"]
    assert epic.title == "Switch staff login to OIDC"
    assert epic.url == "https://acme-sbx.atlassian.net/browse/AUTH-12"
    assert epic.version == "2026-10-15T10:00:00.000+0000"
    assert "--- comment: Priya Raman ---\nDecided: OIDC, not SAML. @D. Okafor please review." in epic.text
    assert [(p.handle, p.name, p.email, p.roles) for p in epic.people] == [
        ("712020:aaaa-priya", "Priya Raman", "priya@acme-sbx.dev", ["reporter", "commenter"]),
        ("712020:bbbb-sam", "Sam Lee", None, ["assignee", "commenter", "mentioned"]),
        ("712020:cccc-dana", "D. Okafor", None, ["mentioned"])]
    assert epic.people[0].id == "person:jira:712020%3Aaaaa-priya"
    assert epic.meta["bots"] == ["Automation for Jira"]
    assert {k: epic.meta[k] for k in ("issue_type", "status", "status_category", "labels", "components")} == {
        "issue_type": "Epic", "status": "In Progress", "status_category": "indeterminate",
        "labels": ["auth", "oidc"], "components": ["login"]}


def test_text_links_resolve_across_sources_and_code_is_ignored(items):
    epic = items["AUTH-12"]
    assert [(r.type, r.target, r.via) for r in epic.refs] == [
        ("references", "github:repo:9001:pr:2", "url"),
        ("references", "gdrive:file:1AbCdEfGhIjK", "url"),
        ("references", issue_id("10013"), "key"),
        ("references", issue_id("10014"), "key")]        # PLAT-7, the key AUTH-14 had before it moved
    assert "jira_keys" not in epic.meta                     # AUTH-99 sits in a code block


def test_parent_and_outward_links_become_typed_refs(items):
    task = items["AUTH-13"]
    assert [(r.type, r.target, r.via) for r in task.refs] == [
        ("part_of", issue_id("10012"), "field"),
        ("blocks", issue_id("10014"), "field"),
        ("references", issue_id("10030"), "field")]         # AUTH-12 and OPS-3 in the text are already linked
    assert task.meta["parent_key"] == "AUTH-12"
    assert task.meta["links"] == [{"type": "blocks", "key": "AUTH-14"}, {"type": "relates to", "key": "OPS-3"}]


def test_inward_links_are_left_to_the_other_end_and_old_keys_are_aliases(items):
    story = items["AUTH-14"]
    assert [(r.type, r.target) for r in story.refs] == [("part_of", issue_id("10012"))]
    assert story.meta["links"] == [{"type": "is blocked by", "key": "AUTH-13"}]
    assert story.aliases == ["AUTH-14", "PLAT-7"] and story.meta["old_keys"] == ["PLAT-7"]
    assert story.meta["security_level"] == "Staff only"
    assert story.people == [] and story.meta["bots"] == ["Automation for Jira"]   # filed by automation
    assert story.text == ""


def test_secrets_are_gone_and_recorded(items):
    epic = items["AUTH-12"]
    assert planted()["slack_bot"] not in epic.text
    assert [(r.field, r.count) for r in epic.redactions] == [("text", 1)]
