"""DriveConnector: scope, windows, text, and permissions."""

from datetime import timedelta

import httpx
import pytest

from swarm.interfaces import Checkpoint
from swarm.sources.gdrive.connector import MAX_PDF_BYTES

from .gdrive_fixture import NOW, FakeDrive, connector, make_pdf


def fetch_all(conn, since=None):
    out = {}
    for container in conn.enumerate():
        for raw in conn.fetch(container, (since or {}).get(container.id)):
            out[raw.payload["file"]["id"]] = raw
    return out


# -- scope ------------------------------------------------------------------------ #

def test_scope_is_granted_drives_narrowed_plus_listed_folders():
    conn, _ = connector()
    assert [(c.id, c.name) for c in conn.enumerate()] == [("gdrive:file:1CLUBfolder01", "Club shared"),
                                                          ("gdrive:file:0AENGdrive", "Engineering")]


def test_without_a_drive_list_every_drive_the_account_joined_is_in_scope():
    conn, _ = connector(drives=None, folders=None)
    assert [c.name for c in conn.enumerate()] == ["Engineering", "People Ops"]


def test_a_listed_id_that_is_not_a_folder_is_refused():
    conn, _ = connector(folders=["1AbCdEfGhIjK"])
    with pytest.raises(ValueError, match="not a folder"):
        list(conn.enumerate())


def test_a_listed_folder_is_walked_down_to_its_files():
    raws = fetch_all(connector(drives=["none"])[0])
    assert set(raws) == {"1CLUBfolder01", "1CLUBnotes001", "1CLUBmeet0001"}


# -- windows ---------------------------------------------------------------------- #

def test_first_run_backfills_and_skips_files_outside_the_window():
    conn, fake = connector()
    raws = fetch_all(conn)
    assert "1OLDdoc000001" not in raws                      # modified 2024-06, before the 730-day window
    drive_query = next(r for r in fake.requests if r.url.params.get("corpora") == "drive")
    assert drive_query.url.params["driveId"] == "0AENGdrive"
    assert "modifiedTime >= '2024-10-21T12:00:00Z'" in drive_query.url.params["q"]


def test_checkpoint_is_the_newest_modified_time_and_the_next_run_looks_back_a_week():
    conn, _ = connector()
    fetch_all(conn)
    containers = {c.id: c for c in conn.enumerate()}
    cp = conn.checkpoint(containers["gdrive:file:0AENGdrive"])
    assert cp.cursor == "2026-10-15T11:00:00Z"
    later, fake = connector(now=NOW + timedelta(days=1))
    raws = fetch_all(later, {cp.container_id: cp})
    q = next(r for r in fake.requests if r.url.params.get("corpora") == "drive").url.params["q"]
    assert "modifiedTime >= '2026-10-08T11:00:00Z'" in q
    assert "1PDFscanned01" not in raws and "1AbCdEfGhIjK" in raws


def test_other_file_types_are_skipped_and_counted():
    conn, _ = connector()
    raws = fetch_all(conn)
    assert "1PNGdiagram01" not in raws and conn.skipped == {"image/png": 1}


# -- text ------------------------------------------------------------------------- #

def test_docs_and_slides_are_exported_to_plain_text():
    raws = fetch_all(connector()[0])
    doc = raws["1AbCdEfGhIjK"].payload
    assert doc["text_status"] == "exported"
    assert doc["text"].startswith("OIDC rollout spec\nImplements")             # BOM and CRLF gone
    assert raws["1SLIDESdeck01"].payload["text"] == "Staff login moves to OIDC\n\nMonday cutover, NEXP-318"


def test_pdfs_are_read_when_they_have_text_and_small_enough():
    conn, fake = connector()
    raws = fetch_all(conn)
    assert raws["1PDFrunbook01"].payload["text_status"] == "extracted"
    assert "restart the IdP proxy" in raws["1PDFrunbook01"].payload["text"]
    assert raws["1PDFscanned01"].payload["text_status"] == "no_text"
    assert raws["1PDFhuge00001"].payload["text_status"] == "too_large"
    downloads = {r.url.path for r in fake.requests if r.url.params.get("alt") == "media"}
    assert "/drive/v3/files/1PDFhuge00001" not in downloads          # 90 MB, over MAX_PDF_BYTES
    assert MAX_PDF_BYTES < 90_000_000


def test_a_broken_pdf_is_unreadable_not_fatal():
    fake = FakeDrive()
    original = fake.handler

    def handler(request):
        if request.url.params.get("alt") == "media":
            return httpx.Response(200, content=b"%PDF-1.4 garbage")
        return original(request)

    fake.handler = handler
    raws = fetch_all(connector(fake)[0])
    assert raws["1PDFrunbook01"].payload["text_status"] in ("unreadable", "no_text")


def test_sheets_and_folders_keep_metadata_only():
    raws = fetch_all(connector()[0])
    assert raws["1SHEETbudget1"].payload["text_status"] == "metadata_only"
    assert raws["1SPECSfolder00"].payload["text_status"] == "metadata_only"


def test_make_pdf_round_trips_through_pypdf():
    from io import BytesIO

    from pypdf import PdfReader
    assert PdfReader(BytesIO(make_pdf("hello (world)"))).pages[0].extract_text() == "hello (world)"


# -- permissions ------------------------------------------------------------------ #

@pytest.fixture(scope="module")
def drive():
    conn, _ = connector()
    return conn, fetch_all(conn)


def acl(drive, file_id):
    conn, raws = drive
    return conn.acl(raws[file_id])


def test_people_and_groups_become_principals_without_the_service_account(drive):
    a = acl(drive, "1AbCdEfGhIjK")
    assert a.visibility == "restricted"
    assert a.principals == ["group:gdrive:group:08ENGGRP", "user:priya@acme.com", "user:sam@acme.com"]
    club = acl(drive, "1CLUBmeet0001")                    # deleted permissions are dropped too
    assert club.principals == ["user:jo.kim@gmail.com", "user:priya@acme.com", "user:sam@acme.com"]


def test_domains_and_anyone_links(drive):
    assert acl(drive, "1SLIDESdeck01").principals == ["domain:acme.com", "user:sam@acme.com"]
    assert acl(drive, "1SHEETbudget1").visibility == "public"


def test_unreadable_permissions_fall_back_to_the_parent_folder(drive):
    _, raws = drive
    assert raws["1PDFrunbook01"].payload["acl_inherited_from"] == "1SPECSfolder00"
    assert acl(drive, "1PDFrunbook01") == acl(drive, "1SPECSfolder00")


def test_limited_access_folders_never_inherit_and_go_unwritten(drive):
    assert acl(drive, "1LOCKEDfold01") is None
    assert acl(drive, "1LOCKEDdoc001") is None             # its parent's list is unreadable too


def test_groups_name_the_google_groups_acls_use(drive):
    conn, _ = drive
    containers = {c.id: c for c in conn.enumerate()}
    [group] = conn.groups(containers["gdrive:file:0AENGdrive"], run_id="r1")
    assert (group.id, group.name, group.members) == ("group:gdrive:group:08ENGGRP", "eng@acme.com", [])
    assert conn.groups(containers["gdrive:file:1CLUBfolder01"], run_id="r1") == []


def test_permissions_are_asked_once_per_file():
    conn, fake = connector()
    fetch_all(conn)
    asked = [r.url.path for r in fake.requests
             if r.url.path.endswith("/permissions") and "pageToken" not in r.url.params]
    assert len(asked) == len(set(asked))


def test_list_ids_covers_everything_in_scope():
    conn, _ = connector()
    containers = {c.id: c for c in conn.enumerate()}
    listed = list(conn.list_ids(containers["gdrive:file:0AENGdrive"]))
    assert listed[0] == "gdrive:file:0AENGdrive"
    assert "gdrive:file:1OLDdoc000001" in listed and "gdrive:file:1PNGdiagram01" not in listed


def test_the_checkpoint_cursor_survives_a_quiet_run():
    conn, _ = connector()
    containers = {c.id: c for c in conn.enumerate()}
    since = Checkpoint("gdrive:file:0AENGdrive", "2026-10-20T00:00:00Z", "x")
    list(conn.fetch(containers["gdrive:file:0AENGdrive"], since))
    assert conn.checkpoint(containers["gdrive:file:0AENGdrive"]).cursor == "2026-10-20T00:00:00Z"
