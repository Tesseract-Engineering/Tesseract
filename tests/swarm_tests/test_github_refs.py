"""Deterministic refs from GitHub text."""

import pytest

from swarm.sources.github.refs import RepoIndex, extract

API, WEB = 9001, 9002


def index(lookup=None):
    return RepoIndex(full_names={"acme-sbx/api": API, "acme-sbx/web": WEB},
                     kinds={(API, 1): "issue", (API, 2): "pull_request", (API, 3): "issue", (WEB, 1): "issue"},
                     lookup=lookup)


def refs(text, kind="pull_request", own=2, idx=None):
    found = extract(text, item_kind=kind, repo_id=API, own_number=own, index=idx or index())
    return [(r.type, r.target, r.via) for r in found.refs], found


@pytest.mark.parametrize("keyword", ["Fixes", "fixes", "fixed", "Closes", "close", "closed", "Resolves",
                                     "resolve", "resolved", "Fix:"])
def test_every_closing_keyword(keyword):
    assert refs(f"{keyword} #1")[0] == [("closes", "github:repo:9001:issue:1", "closing_keyword")]


@pytest.mark.parametrize(("kind", "relation"), [("pull_request", "closes"), ("commit", "implements"),
                                                ("issue", "references")])
def test_closing_relation_depends_on_the_item_kind(kind, relation):
    assert refs("Fixes #1", kind=kind, own=None)[0][0][0] == relation


def test_closing_a_pr_number_is_only_a_reference():
    assert refs("Fixes #2", kind="commit", own=None)[0] == [
        ("references", "github:repo:9001:pr:2", "closing_keyword")]


def test_cross_repo_and_url_forms():
    text = ("Closes acme-sbx/web#1. See https://github.com/acme-sbx/api/issues/3 and "
            "https://github.com/acme-sbx/api/commit/abcdef1234567 and resolves https://github.com/acme-sbx/api/issues/1")
    assert refs(text)[0] == [
        ("closes", "github:repo:9002:issue:1", "closing_keyword"),
        ("closes", "github:repo:9001:issue:1", "closing_keyword"),
        ("references", "github:repo:9001:issue:3", "url"),
        ("references", "github:repo:9001:commit:abcdef1234567", "url"),
    ]


def test_plain_numbers_resolve_issue_vs_pr():
    assert refs("follow-up to #2 and #3", kind="issue", own=1)[0] == [
        ("references", "github:repo:9001:pr:2", "key"), ("references", "github:repo:9001:issue:3", "key")]


def test_unknown_numbers_are_looked_up_once_and_absent_ones_dropped():
    asked = []

    def lookup(repo_id, number):
        asked.append((repo_id, number))
        return "pull_request" if number == 4 else None

    idx = index(lookup)
    found, info = refs("see #4, #4 again, and #77 and #77", kind="issue", own=1, idx=idx)
    assert found == [("references", "github:repo:9001:pr:4", "key")]
    assert asked == [(API, 4), (API, 77)]
    assert info.dropped == 2


def test_out_of_scope_repos_are_dropped_and_counted():
    found, info = refs("Fixes other-org/lib#5 and see github.com/x https://github.com/other-org/lib/pull/9")
    assert found == [] and info.dropped == 2


def test_code_and_quotes_are_ignored():
    text = "Real: fixes #1\n\n```\nfixes #3\n```\n\nInline `closes #3` too.\n\n> fixes #3"
    assert refs(text)[0] == [("closes", "github:repo:9001:issue:1", "closing_keyword")]


def test_own_number_and_anchors_are_not_refs():
    assert refs("This PR (#2) see https://github.com/acme-sbx/api/pull/2#discussion_r1 and page#3x")[0] == []


def test_drive_jira_slack_and_mentions():
    text = ("Design https://docs.google.com/document/d/1DesignDocId0123/edit and "
            "https://drive.google.com/file/d/1PdfFileId0456/view. Tracked in AUTH-12 (not SHA-256). "
            "Thread https://acme-sbx.slack.com/archives/C42AUTH/p1760450400000100. "
            "cc @priya-r and @sam-lee; not email sam@acme-sbx.dev or @org/team")
    found, info = refs(text)
    assert found == [("references", "gdrive:file:1DesignDocId0123", "url"),
                     ("references", "gdrive:file:1PdfFileId0456", "url")]
    assert info.jira_keys == ["AUTH-12"]
    assert info.slack_links == ["https://acme-sbx.slack.com/archives/C42AUTH/p1760450400000100"]
    assert info.mentions == ["priya-r", "sam-lee"]


def test_outside_tracker_repos_never_resolve_bare_numbers():
    asked = []
    idx = index(lambda repo_id, number: asked.append(number) or "pull_request")
    idx.external.add(API)
    text = ("Fixed #11379 -- Refs #34865. Supersedes #2. Closes acme-sbx/web#1. "
            "See https://github.com/acme-sbx/api/pull/3")
    found, info = refs(text, kind="commit", own=None, idx=idx)
    # Bare numbers are Trac tickets here: kept aside, never looked up, never linked.
    assert info.external == ["#2", "#11379", "#34865"] and asked == []
    assert found == [("implements", "github:repo:9002:issue:1", "closing_keyword"),
                     ("references", "github:repo:9001:issue:3", "url")]
    assert info.dropped == 0
