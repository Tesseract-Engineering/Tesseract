"""DriveNormalizer: folders and documents become valid item records."""

import pytest

from swarm.records import parse_line
from swarm.sources.gdrive import DriveNormalizer
from swarm.sources.links import GitHubLinks
from swarm.stages.redact import redact_payload

from .gdrive_fixture import connector, planted

GITHUB = GitHubLinks(repos={"acme-sbx/api": 9001}, kinds={(9001, 1): "issue", (9001, 2): "pull_request"})


@pytest.fixture(scope="module")
def items():
    conn, _ = connector()
    norm = DriveNormalizer(GITHUB)
    norm.run_id = "r1"
    out = {}
    for container in conn.enumerate():
        for raw in conn.fetch(container, None):
            raw.payload, _ = redact_payload(raw.payload)
            acl = conn.acl(raw)
            if acl is not None:
                item = norm.to_item(raw, acl)
                out[item.id] = item
    return out


def test_every_item_round_trips_through_the_contract(items):
    assert sorted({i.kind for i in items.values()}) == ["document", "folder"]
    for item in items.values():
        assert parse_line(item.model_dump_json()) == item


def test_a_doc_carries_its_text_version_and_place(items):
    doc = items["gdrive:file:1AbCdEfGhIjK"]
    assert doc.title == "OIDC spec" and doc.aliases == ["OIDC spec"]
    assert doc.container == "gdrive:file:1SPECSfolder00"
    assert doc.version == "42" and doc.updated_at.isoformat() == "2026-10-14T16:30:00+00:00"
    assert doc.url == "https://docs.google.com/document/d/1AbCdEfGhIjK/edit"
    assert doc.meta["file_type"] == "doc" and doc.meta["drive_id"] == "0AENGdrive"


def test_roots_have_no_container_and_folders_describe_themselves(items):
    assert items["gdrive:file:0AENGdrive"].container is None
    assert items["gdrive:file:1CLUBfolder01"].container is None             # its parent is outside the grant
    assert items["gdrive:file:1SPECSfolder00"].text == "Design specs"


def test_owners_and_editors_by_email(items):
    meeting = items["gdrive:file:1CLUBmeet0001"]
    assert [(p.id, p.roles) for p in meeting.people] == [("person:gdrive:sam@acme.com", ["owner", "participant"])]
    doc = items["gdrive:file:1AbCdEfGhIjK"]
    assert [(p.email, p.name, p.roles) for p in doc.people] == [("priya@acme.com", "Priya Rao", ["participant"])]


def test_refs_from_text_skip_the_doc_itself(items):
    doc = items["gdrive:file:1AbCdEfGhIjK"]
    assert {(r.target, r.via) for r in doc.refs} == {("github:repo:9001:pr:2", "key"),
                                                     ("gdrive:file:1SLIDESdeck01", "url")}
    assert doc.meta["slack_links"] == ["https://acme.slack.com/archives/C42AUTH/p1791986400000100"]
    meeting = items["gdrive:file:1CLUBmeet0001"]
    assert [r.target for r in meeting.refs] == ["github:repo:9001:issue:1"]


def test_secrets_are_gone_and_recorded(items):
    doc = items["gdrive:file:1AbCdEfGhIjK"]
    assert planted()["aws_access_key"] not in doc.text
    assert [(r.field, r.type, r.count) for r in doc.redactions] == [("text", "AWS Access Key", 1)]


def test_inherited_acls_are_marked(items):
    pdf = items["gdrive:file:1PDFrunbook01"]
    assert pdf.meta["acl_inherited_from"] == "gdrive:file:1SPECSfolder00"
    assert pdf.meta["text_status"] == "extracted"


def test_a_key_named_only_in_the_file_name_is_found():
    from swarm.interfaces import RawContainer, RawItem
    from swarm.records import Acl
    from swarm.sources.gdrive import DriveNormalizer

    norm = DriveNormalizer()
    norm.run_id = "r1"
    f = {"id": "1NAMEDdoc0001", "name": "SBX-1 OIDC notes", "mimeType": "application/vnd.google-apps.document",
         "parents": ["0AENGdrive"], "createdTime": "2026-10-01T00:00:00Z", "modifiedTime": "2026-10-01T00:00:00Z",
         "version": "1"}
    raw = RawItem("gdrive", RawContainer("gdrive", "gdrive:file:0AENGdrive"), "file-1NAMEDdoc0001",
                  {"type": "doc", "file": f, "root": "0AENGdrive", "text": "No keys in here.",
                   "text_status": "exported", "permissions": [], "acl_inherited_from": None}, "2026-10-21T12:00:00Z")
    assert norm.to_item(raw, Acl(visibility="public")).meta["jira_keys"] == ["SBX-1"]
