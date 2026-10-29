"""Source refs (graph/refs.py): the URI form, and links in and out."""

import pytest

from graph import refs
from graph.refs import SourceRef

SHA = "0123456789abcdef0123456789abcdef01234567"
JIRA = "https://acme.atlassian.net"

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
    assert refs.from_url(refs.to_url(ref, jira_base_url=JIRA)) == ref


def test_the_canonical_forms():
    assert EVERY_KIND[0].uri() == f"forge://github/blob/acme/api@{SHA}:src/auth/session.py"
    assert EVERY_KIND[2].uri() == "forge://github/pull/acme/api/412"
    assert EVERY_KIND[5].uri() == "forge://jira/issue/AUTH-42"
    assert refs.to_url(EVERY_KIND[5], jira_base_url=JIRA) == "https://acme.atlassian.net/browse/AUTH-42"


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
    assert refs.to_url(SourceRef("jira", "issue", "AUTH-42")) is None

