"""Source refs: the URI form, links in and out, and what the build reports about them."""

import pytest

from tesseract import build, load, refs
from tesseract.config import Company
from tesseract.connectors import SourceRecord
from tesseract.refs import SourceRef

SHA = "0123456789abcdef0123456789abcdef01234567"
COMPANY = Company(jira_base_url="https://acme.atlassian.net")

EVERY_KIND = [
    SourceRef("github", "blob", "acme/api:src/auth/session.py", SHA),
    SourceRef("github", "blob", "acme/api:README.md"),
    SourceRef("github", "pull", "acme/api/412"),
    SourceRef("github", "issue", "acme/api/7"),
    SourceRef("github", "repo", "acme/api"),
    SourceRef("jira", "issue", "AUTH-42"),
    SourceRef("gdrive", "file", "1AbCdEfGhIjKlMnOpQrStUvWxYz"),
    SourceRef("gdrive", "folder", "0BxYz_folder-id_123"),
]


@pytest.mark.parametrize("ref", EVERY_KIND, ids=lambda r: f"{r.provider}-{r.kind}")
def test_uri_round_trips(ref):
    assert refs.parse(ref.uri()) == ref


@pytest.mark.parametrize("ref", EVERY_KIND, ids=lambda r: f"{r.provider}-{r.kind}")
def test_url_round_trips(ref):
    assert refs.from_url(refs.to_url(ref, COMPANY)) == ref


def test_the_canonical_forms():
    assert EVERY_KIND[0].uri() == f"forge://github/blob/acme/api@{SHA}:src/auth/session.py"
    assert EVERY_KIND[2].uri() == "forge://github/pull/acme/api/412"
    assert EVERY_KIND[5].uri() == "forge://jira/issue/AUTH-42"
    assert refs.to_url(EVERY_KIND[5], COMPANY) == "https://acme.atlassian.net/browse/AUTH-42"


def test_a_blob_path_may_contain_colons_and_at_signs():
    ref = SourceRef("github", "blob", "acme/api:docs/a:b@c.md", SHA)
    assert refs.parse(ref.uri()) == ref


@pytest.mark.parametrize("url, uri", [
    ("https://github.com/acme/api/pull/412/files#diff-1", "forge://github/pull/acme/api/412"),
    ("https://github.com/acme/api/issues/7?q=1", "forge://github/issue/acme/api/7"),
    ("https://github.com/acme/api.git", "forge://github/repo/acme/api"),
    ("https://github.com/acme/api/blob/main/docs/x.md", "forge://github/blob/acme/api@main:docs/x.md"),
    ("https://docs.google.com/document/d/1AbCdEfGhIjK/edit?usp=sharing", "forge://gdrive/file/1AbCdEfGhIjK"),
    ("https://docs.google.com/spreadsheets/d/1AbCdEfGhIjK/edit#gid=0", "forge://gdrive/file/1AbCdEfGhIjK"),
    ("https://drive.google.com/open?id=1AbCdEfGhIjK", "forge://gdrive/file/1AbCdEfGhIjK"),
    ("https://drive.google.com/drive/u/0/folders/0BxYzFolder1", "forge://gdrive/folder/0BxYzFolder1"),
    ("https://jira.internal.example/browse/AUTH-42", "forge://jira/issue/AUTH-42"),
])
def test_links_people_paste(url, uri):
    assert refs.from_url(url).uri() == uri


@pytest.mark.parametrize("url", [
    "https://example.com/acme/api", "https://github.com/acme", "https://github.com/acme/api/actions",
    "https://example.com/browse/utf-8", "mailto:a@b.c", "not a url", "",
])
def test_unrecognised_links_are_none(url):
    assert refs.from_url(url) is None


@pytest.mark.parametrize("args", [
    ("github", "commit", "acme/api"), ("slack", "thread", "x"), ("jira", "issue", "auth-42"),
    ("github", "pull", "acme/api/x"), ("gdrive", "file", "short"),
])
def test_malformed_refs_are_refused(args):
    with pytest.raises(ValueError):
        SourceRef(*args)


def test_jira_has_no_link_without_a_base_url():
    assert refs.to_url(SourceRef("jira", "issue", "AUTH-42"), Company()) is None


def record(**kw):
    base = dict(kind="document", id="doc:x", title="X", text="", url="", created_at="", source="docs")
    return SourceRecord(**{**base, **kw})


def test_artifacts_carry_ref_url_and_source_dates():
    ref = SourceRef("jira", "issue", "AUTH-42")
    ticket = record(kind="issue", id="ticket:AUTH-42", source="jira", ref=ref,
                    url="https://acme.atlassian.net/browse/AUTH-42",
                    meta={"updatedAt": "2026-10-01T12:00:00Z"})
    doc = record(url="https://intranet/doc", meta={"contentSha": "abc"})
    issue, document = load.artifacts([ticket, doc], ns="t")
    assert issue.props["ref"] == "forge://jira/issue/AUTH-42"
    assert (issue.props["refProvider"], issue.props["refKind"]) == ("jira", "issue")
    assert issue.props["url"] == ticket.url and issue.props["sourceUpdatedAt"].year == 2026
    assert document.props["url"] == "https://intranet/doc", "documents keep their link now"
    assert document.props["contentSha"] == "abc" and "ref" not in document.props


def test_ref_coverage_counts_live_sources_and_names_the_rest():
    good = SourceRef("jira", "issue", "AUTH-1")
    report = build.ref_coverage([
        record(id="ticket:AUTH-1", source="jira", ref=good, url="https://j.example/browse/AUTH-1"),
        record(id="ticket:AUTH-2", source="jira", ref=SourceRef("jira", "issue", "AUTH-2"),
               url="https://j.example/browse/AUTH-3"),
        record(id="ticket:odd", source="jira"),
        record(id="slack:a", source="slack"), record(id="slack:b", source="slack"),
    ])
    assert report["live"] == {"jira": (2, 3)}
    assert report["index_only"] == {"slack": 2}
    assert report["mismatched"] == [("ticket:AUTH-2", "forge://jira/issue/AUTH-2", "https://j.example/browse/AUTH-3")]
