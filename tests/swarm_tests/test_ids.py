"""Every ID builder produces an ID of its own kind, and only that kind."""

import pytest

from swarm import ids

CASES = [
    (ids.github_repo(7001), "github_repo"),
    (ids.github_pr(7001, 412), "github_pr"),
    (ids.github_issue(7001, 398), "github_issue"),
    (ids.github_commit(7001, "3F9C2E1"), "github_commit"),
    (ids.github_file(7001, "docs/adr/0014 retrain: v2.md"), "github_file"),
    (ids.jira_issue("acme", 10318), "jira_issue"),
    (ids.jira_project("acme", 10000), "jira_project"),
    (ids.slack_channel("T01", "C42"), "slack_channel"),
    (ids.slack_thread("T01", "C42", "1729450000.1234"), "slack_thread"),
    (ids.gdrive_file("1AbC"), "gdrive_file"),
    (ids.person("slack", "U123"), "person_source"),
    (ids.person_cluster("7f3a91"), "person_cluster"),
]


@pytest.mark.parametrize(("value", "kind"), CASES)
def test_builder_matches_its_pattern(value, kind):
    assert ids.kind_of(value) == kind


@pytest.mark.parametrize("value", [
    "github:repo:acme/api:pr:x",          # non-numeric PR
    "jira:acme:issue:NEXP-318",           # a key, not the immutable id
    "slack:T01:thread:C42",               # no ts
    "gdrive:file:",                        # empty
    "acme/api#412",                        # an alias, not an ID
])
def test_aliases_and_malformed_ids_are_not_ids(value):
    assert not ids.is_valid(value)


def test_commit_sha_is_lowercased():
    assert ids.github_commit(1, "ABCDEF1") == "github:repo:1:commit:abcdef1"


def test_file_paths_stay_one_segment():
    assert ids.github_file(1, "docs/a b:c.md") == "github:repo:1:file:docs/a%20b%3Ac.md"
    assert ids.entity_types(ids.github_file(1, "README.md")) == {"document"}


def test_a_jira_identity_member_validates_with_its_encoded_id():
    from swarm.records import IdentityMember
    member = IdentityMember(id=ids.source_person("jira", "712020:abc"), source="jira", handle="712020:abc")
    assert member.id == "person:jira:712020%3Aabc" == ids.jira_person("712020:abc")
    assert ids.source_person("github", "kar722") == "person:github:kar722"
