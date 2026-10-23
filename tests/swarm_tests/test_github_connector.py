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
                           "commit-b2c3d4e5f60718293a4b5c6d7e8f901234567890",
                           "file-9001-.github/CODEOWNERS", "file-9001-README.md", "file-9001-docs/adr/0002 use oidc.md"]
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


def test_list_ids_covers_issues_prs_commits_and_docs():
    conn, _ = connector()
    api = next(iter(conn.enumerate()))
    assert list(conn.list_ids(api)) == [
        "github:repo:9001", "github:repo:9001:issue:1", "github:repo:9001:pr:2", "github:repo:9001:issue:3",
        "github:repo:9001:commit:a1b2c3d4e5f60718293a4b5c6d7e8f9012345678",
        "github:repo:9001:commit:b2c3d4e5f60718293a4b5c6d7e8f901234567890",
        "github:repo:9001:file:.github/CODEOWNERS", "github:repo:9001:file:README.md",
        "github:repo:9001:file:docs/adr/0002%20use%20oidc.md"]


def test_empty_repository_has_no_commits_or_docs():
    from .github_fixture import FakeGitHub
    empty = {"status": 409, "body": {"message": "Git Repository is empty."}}
    fake = FakeGitHub({"GET /repos/acme-sbx/web/commits": empty, "GET /repos/acme-sbx/web/git/trees/main": empty})
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


# -- docs --------------------------------------------------------------------- #

API_DOCS = {".github/CODEOWNERS": "d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1d1",
            "README.md": "d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2",
            "docs/adr/0002 use oidc.md": "d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3d3"}


def docs_fetched(conn):
    api = next(iter(conn.enumerate()))
    return {raw.payload["path"]: raw.payload for raw in conn.fetch(api, None) if raw.payload["type"] == "file"}


def blob_calls(fake):
    return sorted(p.rsplit("/", 1)[1] for p in fake.paths() if "/git/blobs/" in p)


def test_docs_are_prose_and_codeowners_never_code_vendored_or_generated():
    conn, fake = connector()
    docs = docs_fetched(conn)
    assert list(docs) == list(API_DOCS)
    # api/auth/oidc.py is code, node_modules/ is vendored, docs/api-reference.md is 900 kB: none is read.
    assert blob_calls(fake) == sorted(API_DOCS.values())
    adr = docs["docs/adr/0002 use oidc.md"]
    assert adr["content"].startswith("# ADR 0002: Use OIDC for staff login")       # decoded, not base64
    assert adr["last_commit"]["commit"]["committer"]["date"] == "2026-10-14T16:05:00Z"
    tree = [r for r in fake.requests if r.url.path == "/repos/acme-sbx/api/git/trees/main"][0]
    assert tree.url.params["recursive"] == "1"


def test_a_doc_whose_blob_is_unchanged_is_not_read_again():
    known = {"9001": {"visibility": "private", "docs": {**API_DOCS, "README.md": "0" * 40}},
             "9002": {"visibility": "public", "docs": {"README.md": "d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4d4"}}}
    conn, fake = connector(known_docs=known)
    assert list(docs_fetched(conn)) == ["README.md"]                 # its sha changed; the other two did not
    assert blob_calls(fake) == ["d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2d2"]
    # Every doc read or confirmed unchanged is published; web, not reached this run, keeps its entry.
    assert conn.published_state()["docs"] == {"9001": {"visibility": "private", "docs": API_DOCS},
                                              "9002": known["9002"]}


def test_a_visibility_change_reads_every_doc_again():
    conn, fake = connector(known_docs={"9001": {"visibility": "public", "docs": API_DOCS}})
    assert list(docs_fetched(conn)) == list(API_DOCS)
    assert conn.published_state()["docs"]["9001"]["visibility"] == "private"


def test_a_doc_with_no_commit_to_date_it_is_skipped_and_retried():
    from .github_fixture import FakeGitHub
    fake = FakeGitHub({"GET /repos/acme-sbx/api/commits?path=README.md": {"pages": [[]]}})
    conn, _ = connector(fake)
    assert "README.md" not in docs_fetched(conn)
    assert "README.md" not in conn.published_state()["docs"]["9001"]["docs"]


def test_is_doc():
    from swarm.sources.github.connector import is_doc
    assert all(is_doc(p) for p in ["README.md", "docs/adr/0014-retrain.md", "guide.rst", "CHANGES.markdown",
                                   "CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"])
    assert not any(is_doc(p) for p in ["app.py", "config.yaml", ".env", "notes.txt", "vendor/lib/README.md",
                                       "web/node_modules/x/README.md", "codeowners.py"])
