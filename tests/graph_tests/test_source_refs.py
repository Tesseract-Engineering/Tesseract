"""Source refs on graph nodes (graph/build_swarm.py): which items get one, and in what form."""

import pytest

from graph.build_swarm import item_props, source_ref

COMMIT = "0123456789abcdef0123456789abcdef01234567"
BLOB = "fedcba9876543210fedcba9876543210fedcba98"


def item(source, kind, url=None, iid="x:1", meta=None, version=None):
    return {"id": iid, "source": source, "kind": kind, "url": url, "title": "t", "text": "t",
            "meta": meta or {}, "version": version}


PR = item("github", "pull_request", "https://github.com/acme/api/pull/412")
THREAD = item("slack", "thread", "https://acme.slack.com/archives/C42AUTH/p1760450400000100")

CASES = [
    ("github pull", PR, "forge://github/pull/acme/api/412"),
    ("github issue", item("github", "issue", "https://github.com/acme/api/issues/398"),
     "forge://github/issue/acme/api/398"),
    ("github repo", item("github", "repo", "https://github.com/acme/api"), "forge://github/repo/acme/api"),
    ("github doc at a commit",
     item("github", "document", f"https://github.com/acme/api/blob/{COMMIT}/docs/adr/0002.md",
          meta={"path": "docs/adr/0002.md", "branch": "main", "last_commit": COMMIT}, version=BLOB),
     f"forge://github/blob/acme/api@{COMMIT}:docs/adr/0002.md"),
    ("github doc at a branch",
     item("github", "document", "https://github.com/acme/api/blob/main/README.md",
          meta={"path": "README.md", "branch": "main", "last_commit": None}, version=BLOB),
     "forge://github/blob/acme/api:README.md"),
    ("github commit", item("github", "commit", f"https://github.com/acme/api/commit/{COMMIT}"), None),
    ("jira issue", item("jira", "issue", "https://acme.atlassian.net/browse/AUTH-12"), "forge://jira/issue/AUTH-12"),
    ("jira project", item("jira", "project", "https://acme.atlassian.net/browse/AUTH"), None),
    ("drive doc", item("gdrive", "document", "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOp/edit"),
     "forge://gdrive/file/1AbCdEfGhIjKlMnOp"),
    ("drive folder", item("gdrive", "folder", "https://drive.google.com/drive/folders/0BxYzFolderId123"),
     "forge://gdrive/folder/0BxYzFolderId123"),
    ("drive doc with no link", item("gdrive", "document", None, iid="gdrive:file:1AbCdEfGhIjKlMnOp"),
     "forge://gdrive/file/1AbCdEfGhIjKlMnOp"),
    ("slack thread", THREAD, None),
    ("github issue linking a PR", item("github", "issue", "https://github.com/acme/api/pull/412"), None),
]


@pytest.mark.parametrize("it, uri", [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_source_ref(it, uri):
    ref = source_ref(it)
    assert (ref.uri() if ref else None) == uri


def test_a_doc_ref_carries_its_blob_sha():
    doc = CASES[3][1]
    props = item_props(doc)
    assert props["ref"] == f"forge://github/blob/acme/api@{COMMIT}:docs/adr/0002.md"
    assert props["contentSha"] == BLOB


def test_a_doc_path_is_the_unquoted_one():
    doc = item("github", "document", f"https://github.com/acme/api/blob/{COMMIT}/docs/my%20notes.md",
               meta={"path": "docs/my notes.md", "last_commit": COMMIT}, version=BLOB)
    assert source_ref(doc).id == "acme/api:docs/my notes.md"


def test_a_drive_id_that_is_not_a_drive_id_gives_no_ref():
    assert source_ref(item("gdrive", "document", None, iid="gdrive:file:short")) is None


def test_nodes_with_a_ref_say_so_and_others_do_not():
    pr = item_props(PR)
    assert (pr["ref"], pr["refProvider"], pr["refKind"]) == ("forge://github/pull/acme/api/412", "github", "pull")
    assert "contentSha" not in pr
    thread = item_props(THREAD)
    assert not {"ref", "refProvider", "refKind", "contentSha"} & set(thread)
