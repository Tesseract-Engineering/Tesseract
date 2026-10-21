"""GitHubNormalizer: each kind becomes a valid item record."""

import pytest

from swarm.records import parse_line
from swarm.sources.github import GitHubNormalizer
from swarm.sources.github.normalize import MAX_TEXT
from swarm.stages.redact import redact_payload

from .github_fixture import connector, planted


@pytest.fixture(scope="module")
def items():
    conn, _ = connector()
    norm = GitHubNormalizer(conn.index)
    norm.run_id = "r1"
    out = {}
    for container in conn.enumerate():
        for raw in conn.fetch(container, None):
            raw.payload, _ = redact_payload(raw.payload)
            item = norm.to_item(raw, conn.acl(raw))
            out[item.id] = item
    return out


def test_every_kind_round_trips_through_the_contract(items):
    assert sorted({i.kind for i in items.values()}) == ["commit", "issue", "pull_request", "repo"]
    for item in items.values():
        assert parse_line(item.model_dump_json()) == item


def test_pr_folds_reviews_review_comments_and_comments(items):
    pr = items["github:repo:9001:pr:2"]
    assert pr.text.index("--- review: priya-r (APPROVED) ---\nLooks good.") \
        < pr.text.index("--- review comment: priya-r on api/auth/oidc.py ---") \
        < pr.text.index("--- comment: ext-dana ---")
    assert "--- review: ext-dana (COMMENTED) ---" in pr.text
    assert {p.handle: p.roles for p in pr.people} == {
        "sam-lee": ["author", "mentioned"], "priya-r": ["reviewer", "commenter"],
        "ext-dana": ["reviewer", "commenter"]}
    assert pr.meta["state"] == "merged" and pr.meta["base"] == "main" and pr.aliases == ["acme-sbx/api#2"]
    assert pr.container == "github:repo:9001" and pr.version == "2026-10-14T16:05:00Z"


def test_issue_people_and_meta(items):
    issue = items["github:repo:9001:issue:1"]
    assert {p.handle: p.roles for p in issue.people} == {
        "priya-r": ["author", "commenter", "mentioned"], "sam-lee": ["assignee", "commenter"]}
    assert issue.meta["labels"] == ["auth", "bug"] and issue.meta["jira_keys"] == ["AUTH-12"]
    assert issue.meta["slack_links"]


def test_commits_carry_email_and_fall_back_to_it_without_a_login(items):
    linked = items["github:repo:9001:commit:a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"]
    assert [(p.handle, p.email) for p in linked.people] == [("sam-lee", "sam@acme-sbx.dev")]
    assert linked.meta["files"] == ["api/auth/oidc.py", "api/auth/saml.py"]
    unlinked = items["github:repo:9001:commit:b2c3d4e5f60718293a4b5c6d7e8f901234567890"]
    assert [(p.id, p.email) for p in unlinked.people] == [("person:github:priya@acme-sbx.dev", "priya@acme-sbx.dev")]


def test_planted_secrets_are_gone_and_recorded(items):
    tokens = planted()
    pr = items["github:repo:9001:pr:2"]
    commit = items["github:repo:9001:commit:b2c3d4e5f60718293a4b5c6d7e8f901234567890"]
    assert tokens["github_classic"] not in pr.text and "[REDACTED:GitHub Token]" in pr.text
    assert [(r.field, r.type, r.count) for r in pr.redactions] == [("text", "GitHub Token", 1)]
    assert tokens["aws_access_key"] not in commit.text
    assert [r.type for r in commit.redactions] == ["AWS Access Key"]


def test_repo_items(items):
    api, web = items["github:repo:9001"], items["github:repo:9002"]
    assert api.acl.visibility == "restricted" and web.acl.visibility == "public"
    assert api.meta["visibility"] == "private" and api.text == "Staff auth and the public API\n\n#auth #api"
    assert api.version == "2026-10-14T16:05:00Z" and api.container is None


def test_long_text_is_truncated_and_flagged():
    from swarm.interfaces import RawContainer, RawItem
    from swarm.records import Acl
    from swarm.sources.github.refs import RepoIndex

    norm = GitHubNormalizer(RepoIndex(full_names={"acme-sbx/api": 9001}))
    norm.run_id = "r1"
    issue = {"number": 9, "title": "big", "user": {"login": "sam-lee"}, "state": "open", "body": "x" * (MAX_TEXT + 10),
             "html_url": "https://github.com/acme-sbx/api/issues/9", "created_at": "2026-10-01T00:00:00Z",
             "updated_at": "2026-10-01T00:00:00Z"}
    raw = RawItem("github", RawContainer("github", "github:repo:9001"), "issue-9001-9",
                  {"type": "issue", "repo": {"id": 9001, "full_name": "acme-sbx/api"}, "issue": issue, "comments": []},
                  "2026-10-21T12:00:00Z")
    item = norm.to_item(raw, Acl(visibility="public"))
    assert len(item.text) == MAX_TEXT and item.meta["truncated"] is True


def test_bots_are_not_people(items):
    pr = items["github:repo:9001:pr:2"]
    assert "github-actions[bot]" not in {p.handle for p in pr.people}
    assert pr.meta["bots"] == ["github-actions[bot]"]
    assert "--- comment: github-actions[bot] ---\nAll checks passed." in pr.text
