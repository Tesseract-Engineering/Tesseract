"""GitHubConnector against the fake API: scope, windows, ACLs, groups."""

from datetime import timedelta

import pytest

from swarm.interfaces import Checkpoint

from .github_fixture import connector


def test_app_mode_enumerates_the_installation_and_indexes_repos():
    conn, fake = connector()
    containers = list(conn.enumerate())
    assert [c.name for c in containers] == ["acme-sbx/api", "acme-sbx/web"]
    assert [c.id for c in containers] == ["github:repo:9001", "github:repo:9002"]
    assert conn.index.full_names == {"acme-sbx/api": 9001, "acme-sbx/web": 9002}
    assert fake.paths() == ["/installation/repositories"]


def test_allowlist_narrows_app_scope_and_token_mode_needs_it():
    conn, _ = connector(repos=["ACME-SBX/web"])
    assert [c.name for c in conn.enumerate()] == ["acme-sbx/web"]
    conn, fake = connector(mode="token", repos=["acme-sbx/api"])
    assert [c.name for c in conn.enumerate()] == ["acme-sbx/api"]
    assert fake.paths() == ["/repos/acme-sbx/api"]
    with pytest.raises(ValueError, match="GITHUB_REPOS"):
        connector(mode="token")


def test_fetch_yields_every_kind_with_sub_resources():
    conn, fake = connector()
    api = next(iter(conn.enumerate()))
    items = {raw.native_id: raw.payload for raw in conn.fetch(api, None)}
    assert list(items) == ["repo-9001", "issue-9001-1", "pr-9001-2", "issue-9001-3",
                           "commit-a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
                           "commit-b2c3d4e5f60718293a4b5c6d7e8f901234567890"]
    pr = items["pr-9001-2"]
    assert pr["pull"]["merged_at"] and len(pr["reviews"]) == 2 and len(pr["review_comments"]) == 1
    assert len(pr["comments"]) == 2              # one person, one bot
    assert items["issue-9001-3"]["comments"] == []          # comments: 0 -> no call
    assert "/repos/acme-sbx/api/issues/3/comments" not in fake.paths()


def test_first_run_backfills_and_later_runs_use_checkpoint_minus_lookback():
    conn, fake = connector()
    api = next(iter(conn.enumerate()))
    list(conn.fetch(api, None))
    first = [r for r in fake.requests if r.url.path == "/repos/acme-sbx/api/issues"][0]
    assert first.url.params["since"] == (conn.now - timedelta(days=730)).strftime("%Y-%m-%dT%H:%M:%SZ")
    cp = conn.checkpoint(api)
    assert cp.cursor == "2026-10-15T09:00:00Z"              # newest updated_at seen

    conn2, fake2 = connector()
    api2 = next(iter(conn2.enumerate()))
    list(conn2.fetch(api2, Checkpoint(api2.id, "2026-10-15T09:00:00Z", "x")))
    second = [r for r in fake2.requests if r.url.path == "/repos/acme-sbx/api/issues"][0]
    assert second.url.params["since"] == "2026-10-08T09:00:00Z"


def test_acl_follows_repo_visibility():
    conn, _ = connector()
    api, web = list(conn.enumerate())
    api_item = next(iter(conn.fetch(api, None)))
    web_item = next(iter(conn.fetch(web, None)))
    assert conn.acl(api_item).model_dump() == {"visibility": "restricted",
                                               "principals": ["group:github:repo:9001:readers"]}
    assert conn.acl(web_item).model_dump() == {"visibility": "public", "principals": []}


def test_readers_group_lists_every_collaborator_for_private_repos_only():
    conn, fake = connector()
    api, web = list(conn.enumerate())
    [group] = conn.groups(api, run_id="r1")
    assert group.id == "group:github:repo:9001:readers"
    assert group.members == ["person:github:ext-dana", "person:github:priya-r", "person:github:sam-lee"]
    collab = [r for r in fake.requests if r.url.path.endswith("/collaborators")][0]
    assert collab.url.params["affiliation"] == "all"         # includes team and org-default access
    assert conn.groups(web, run_id="r1") == []


def test_list_ids_covers_issues_prs_and_commits():
    conn, _ = connector()
    api = next(iter(conn.enumerate()))
    assert list(conn.list_ids(api)) == [
        "github:repo:9001", "github:repo:9001:issue:1", "github:repo:9001:pr:2", "github:repo:9001:issue:3",
        "github:repo:9001:commit:a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
        "github:repo:9001:commit:b2c3d4e5f60718293a4b5c6d7e8f901234567890"]


def test_empty_repository_has_no_commits():
    from .github_fixture import FakeGitHub
    fake = FakeGitHub({"GET /repos/acme-sbx/web/commits": {"status": 409,
                                                          "body": {"message": "Git Repository is empty."}}})
    conn, _ = connector(fake)
    web = list(conn.enumerate())[1]
    kinds = [raw.payload["type"] for raw in conn.fetch(web, None)]
    assert kinds == ["repo", "issue"]


def test_outside_tracker_setting_marks_repos_in_the_index():
    from swarm.http import Client
    from swarm.sources.github import GitHubConnector

    from .github_fixture import FakeGitHub

    fake = FakeGitHub()
    conn = GitHubConnector(Client("https://api.github.com", transport=fake.transport()), mode="app",
                           external_tracker=["ACME-SBX/api"], now=connector()[0].now)
    list(conn.enumerate())
    assert conn.index.external == {9001}


def test_a_gone_issue_is_absent_not_fatal():
    # GitHub answers 410 for a deleted issue, and for any number in a repo with issues turned off.
    from .github_fixture import FakeGitHub
    fake = FakeGitHub({"GET /repos/acme-sbx/api/issues/99": {"status": 410, "body": {"message": "Gone"}}})
    conn, _ = connector(fake)
    list(conn.enumerate())
    assert conn.index.kind(9001, 99) is None
    assert (9001, 99) in conn.index.missing


def test_unlistable_collaborators_write_an_empty_readers_group():
    # Listing collaborators needs push access; a read-only token gets 403.
    from .github_fixture import FakeGitHub
    fake = FakeGitHub({"GET /repos/acme-sbx/api/collaborators": {"status": 403,
                                                                "body": {"message": "Must have push access"}}})
    conn, _ = connector(fake)
    api = next(iter(conn.enumerate()))
    [group] = conn.groups(api, run_id="r1")
    assert group.id == "group:github:repo:9001:readers" and group.members == []


def test_other_collaborator_errors_still_stop_the_run():
    from swarm.http import SourceError

    from .github_fixture import FakeGitHub
    fake = FakeGitHub({"GET /repos/acme-sbx/api/collaborators": {"status": 401, "body": {}}})
    conn, _ = connector(fake)
    api = next(iter(conn.enumerate()))
    with pytest.raises(SourceError):
        conn.groups(api, run_id="r1")


def test_a_future_commit_date_does_not_move_the_checkpoint_past_now():
    from .github_fixture import FakeGitHub
    fake = FakeGitHub()
    sha = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
    fake.routes[f"GET /repos/acme-sbx/api/commits/{sha}"]["commit"]["committer"]["date"] = "2027-06-01T00:00:00Z"
    conn, _ = connector(fake)
    api = next(iter(conn.enumerate()))
    list(conn.fetch(api, None))
    assert conn.checkpoint(api).cursor == "2026-10-21T12:00:00Z"      # the run's clock, not 2027


def test_a_checkpoint_already_in_the_future_heals():
    conn, fake = connector()
    api = next(iter(conn.enumerate()))
    list(conn.fetch(api, Checkpoint(api.id, "2027-06-01T00:00:00Z", "x")))
    issues = [r for r in fake.requests if r.url.path == "/repos/acme-sbx/api/issues"][0]
    assert issues.url.params["since"] == "2026-10-14T12:00:00Z"        # now minus the lookback
    assert conn.checkpoint(api).cursor <= "2026-10-21T12:00:00Z"


def test_visibility_reads_older_payloads_with_only_private():
    from swarm.sources.github.connector import repo_visibility
    assert repo_visibility({"visibility": "internal", "private": True}) == "internal"
    assert repo_visibility({"private": True}) == "private"
    assert repo_visibility({"private": False}) == "public"
