"""JiraConnector against the fake site: scope, windows, comments, keys, ACLs, groups."""

from datetime import timedelta

from swarm.interfaces import Checkpoint
from swarm.sources.jira.connector import browsers_group, security_group

from .jira_fixture import NOW, FakeJira, connector

SITE = "c0ffee00-1111-2222-3333-444455556666"


def fetch_all(conn, since=None):
    out = {}
    for container in conn.enumerate():
        for raw in conn.fetch(container, (since or {}).get(container.id)):
            if raw.payload["type"] == "issue":
                out[raw.payload["issue"]["key"]] = raw
    return out


def test_ids_use_the_cloud_id_and_scope_is_every_browsable_project():
    conn, fake = connector()
    containers = list(conn.enumerate())
    assert [(c.id, c.name) for c in containers] == [(f"jira:{SITE}:project:10000", "AUTH"),
                                                    (f"jira:{SITE}:project:10001", "OPS")]
    assert fake.paths()[0] == "/_edge/tenant_info"


def test_jira_projects_narrows_scope():
    conn, _ = connector(projects=["auth"])
    assert [c.name for c in conn.enumerate()] == ["AUTH"]


def test_first_run_backfills_oldest_first_and_pages():
    conn, fake = connector()
    raws = fetch_all(conn)
    assert list(raws) == ["AUTH-14", "AUTH-13", "AUTH-12", "OPS-3"]        # by updated; AUTH-9 is from 2024
    searches = [r for r in fake.requests if r.url.path == "/rest/api/3/search/jql"]
    assert searches[0].url.params["jql"] == ('project = 10000 AND updated >= "2024-10-21 12:00" '
                                            "ORDER BY updated ASC, id ASC")
    assert searches[1].url.params["nextPageToken"] == "2"                 # three AUTH issues, pages of two
    assert "changelog" in searches[0].url.params["expand"]


def test_checkpoint_is_the_newest_update_and_the_next_run_looks_back_a_week():
    conn, _ = connector()
    containers = {c.name: c for c in conn.enumerate()}
    list(conn.fetch(containers["AUTH"], None))
    cp = conn.checkpoint(containers["AUTH"])
    assert cp.cursor == "2026-10-15T10:00:00Z"
    later, fake = connector(now=NOW + timedelta(days=1))
    auth = next(iter(later.enumerate()))
    keys = [raw.payload["issue"]["key"] for raw in later.fetch(auth, cp) if raw.payload["type"] == "issue"]
    jql = next(r for r in fake.requests if r.url.path == "/rest/api/3/search/jql").url.params["jql"]
    assert 'updated >= "2026-10-08 10:00"' in jql and keys == ["AUTH-14", "AUTH-13", "AUTH-12"]


def test_each_project_comes_first_with_its_description_and_lead():
    conn, _ = connector()
    auth = next(iter(conn.enumerate()))
    first = next(iter(conn.fetch(auth, None)))
    assert first.native_id == "project-10000" and first.payload["type"] == "project"
    assert first.payload["description"] == "Staff sign-in: OIDC, keys and sessions."
    assert first.payload["lead"]["displayName"] == "Priya Raman"
    assert conn.acl(first).principals == [browsers_group(SITE, "10000")]


def test_text_is_rendered_before_redaction_and_comments_past_the_first_page_are_read():
    conn, fake = connector()
    epic = fetch_all(conn)["AUTH-12"].payload
    assert epic["description"].startswith("Replace SAML with OIDC for staff login. Owner: @Sam Lee.")
    assert "the PR (https://github.com/acme-sbx/api/pull/2)" in epic["description"]
    assert [c["id"] for c in epic["comments"]] == ["20001", "20002", "20003"]
    assert epic["comments"][2]["mentions"] == ["712020:cccc-dana"]
    assert "/rest/api/3/issue/10012/comment" in fake.paths()
    assert "/rest/api/3/issue/10013/comment" not in fake.paths()        # no comments past the first page
    assert "description" not in epic["fields"] and "comment" not in epic["fields"]


def test_a_moved_issue_keeps_its_old_key_and_every_key_is_published():
    conn, _ = connector()
    raws = fetch_all(conn)
    assert raws["AUTH-14"].payload["old_keys"] == ["PLAT-7"]
    assert conn.published_state()["keys"] == {"site": SITE, "keys": {
        "AUTH-12": "10012", "AUTH-13": "10013", "AUTH-14": "10014", "OPS-3": "10030", "PLAT-7": "10014"}}


def test_keys_from_earlier_runs_are_kept_and_unknown_keys_are_looked_up_in_scope_only():
    conn, fake = connector(projects=["AUTH"], known_keys={"site": SITE, "keys": {"AUTH-9": "10009"}})
    list(conn.enumerate())
    assert conn.index.target("AUTH-9") == f"jira:{SITE}:issue:10009"            # from state, no call
    assert conn.index.target("OPS-3") is None                                   # exists, but OPS is not granted
    assert conn.index.target("NOPE-1") is None
    assert conn.index.target("NOPE-1") is None                                  # asked once
    assert fake.paths().count("/rest/api/3/issue/NOPE-1") == 1


def test_keys_from_another_site_are_dropped():
    conn, _ = connector(known_keys={"site": "another-site", "keys": {"AUTH-9": "999"}})
    list(conn.enumerate())
    assert conn.index.site == SITE and "AUTH-9" not in conn.index.keys


def test_acl_is_the_projects_browsers_or_the_security_level():
    conn, _ = connector()
    raws = fetch_all(conn)
    assert conn.acl(raws["AUTH-12"]).principals == [browsers_group(SITE, "10000")]
    assert conn.acl(raws["AUTH-14"]).principals == [security_group(SITE, "10100")]
    assert conn.acl(raws["OPS-3"]).model_dump() == {"visibility": "restricted",
                                                   "principals": [f"group:jira:{SITE}:project:10001:browsers"]}


def test_browsers_are_active_people_by_email_or_account_and_levels_have_no_members():
    conn, _ = connector()
    auth = next(iter(conn.enumerate()))
    list(conn.fetch(auth, None))
    browsers, level = conn.groups(auth, run_id="r1")
    assert browsers.id == f"group:jira:{SITE}:project:10000:browsers"
    # Priya's email is visible; Sam's and Dana's are hidden; the inactive user and the app are left out.
    assert browsers.members == ["person:jira:712020%3Abbbb-sam", "person:jira:712020%3Acccc-dana",
                                "user:priya@acme-sbx.dev"]
    assert (level.id, level.members, level.name) == (security_group(SITE, "10100"), [], "AUTH security level Staff only")


def test_unlistable_browsers_write_an_empty_group():
    fake = FakeJira()
    fake.forbidden.add("/rest/api/3/user/permission/search")
    conn, _ = connector(fake)
    auth = next(iter(conn.enumerate()))
    [browsers] = conn.groups(auth, run_id="r1")
    assert browsers.members == []


def test_list_ids_covers_every_issue_in_the_project():
    conn, _ = connector()
    auth = next(iter(conn.enumerate()))
    assert list(conn.list_ids(auth)) == [auth.id] + [f"jira:{SITE}:issue:{n}" for n in ("10009", "10012", "10013", "10014")]


def test_a_checkpoint_in_the_future_heals():
    conn, fake = connector()
    auth = next(iter(conn.enumerate()))
    list(conn.fetch(auth, Checkpoint(auth.id, "2027-06-01T00:00:00Z", "x")))
    jql = next(r for r in fake.requests if r.url.path == "/rest/api/3/search/jql").url.params["jql"]
    assert 'updated >= "2026-10-14 12:00"' in jql
