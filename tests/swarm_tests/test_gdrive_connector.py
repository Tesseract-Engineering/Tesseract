"""DriveConnector: scope, windows, text, and permissions."""

from datetime import timedelta

import httpx
import pytest

from swarm.interfaces import Checkpoint
from swarm.text import MAX_OFFICE_XML, MAX_PDF_BYTES, office_text

from .gdrive_fixture import NOW, FakeDrive, connector, make_office, make_pdf


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


def test_other_file_types_keep_metadata_and_are_counted():
    conn, fake = connector()
    raws = fetch_all(conn)
    assert raws["1PNGdiagram01"].payload["type"] == "file"
    assert raws["1PNGdiagram01"].payload["text_status"] == "metadata_only"
    assert conn.metadata_only == {"image/png": 1}
    assert "/drive/v3/files/1PNGdiagram01" not in {r.url.path for r in fake.requests if r.url.params.get("alt")}


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


def test_sheets_are_exported_as_xlsx_with_every_sheet_and_folders_keep_metadata_only():
    conn, fake = connector()
    raws = fetch_all(conn)
    sheet = raws["1SHEETbudget1"].payload
    assert sheet["text_status"] == "extracted"
    assert sheet["text"] == "Budget\nItem\tMonthly\nIdP proxy\t1200\n\nNotes\nOwner\tsam@acme.com"   # both sheets
    export = next(r for r in fake.requests if r.url.path == "/drive/v3/files/1SHEETbudget1/export")
    assert export.url.params["mimeType"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
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
    assert "gdrive:file:1OLDdoc000001" in listed and "gdrive:file:1PNGdiagram01" in listed


def test_the_checkpoint_cursor_survives_a_quiet_run():
    conn, _ = connector()
    containers = {c.id: c for c in conn.enumerate()}
    since = Checkpoint("gdrive:file:0AENGdrive", "2026-10-20T00:00:00Z", "x")
    list(conn.fetch(containers["gdrive:file:0AENGdrive"], since))
    assert conn.checkpoint(containers["gdrive:file:0AENGdrive"]).cursor == "2026-10-20T00:00:00Z"


# -- failures that must not pass for answers ---------------------------------------- #

THROTTLED = httpx.Response(403, json={"error": {"errors": [{"reason": "userRateLimitExceeded"}]}})


def _wrap(fake, intercept):
    """Answer some requests with `intercept`; it returns None to let the fake answer."""
    original = fake.handler

    def handler(request):
        return intercept(request) or original(request)

    fake.handler = handler
    return fake


@pytest.mark.parametrize("error", [IndexError, RecursionError, AttributeError, OSError])
def test_any_pdf_parser_failure_is_unreadable_not_fatal(monkeypatch, error):
    import pypdf

    def explode(*args, **kwargs):
        raise error("bad pdf")

    monkeypatch.setattr(pypdf, "PdfReader", explode)
    raws = fetch_all(connector()[0])
    assert raws["1PDFrunbook01"].payload["text_status"] == "unreadable"


def test_a_throttled_export_is_retried_not_recorded_as_too_large():
    throttled = []

    def intercept(request):
        if request.url.path.endswith("/1AbCdEfGhIjK/export") and not throttled:
            throttled.append(request)
            return THROTTLED

    raws = fetch_all(connector(_wrap(FakeDrive(), intercept))[0])
    assert throttled and raws["1AbCdEfGhIjK"].payload["text_status"] == "exported"


def test_throttling_that_never_lets_up_stops_the_run():
    from swarm.http import SourceError

    def intercept(request):
        if request.url.path.endswith("/export"):
            return THROTTLED

    with pytest.raises(SourceError):
        fetch_all(connector(_wrap(FakeDrive(), intercept))[0])


@pytest.mark.parametrize(("reason", "status"), [("exportSizeLimitExceeded", "too_large"),
                                                ("cannotExportFile", "unreadable")])
def test_export_refusals_are_named_by_their_reason(reason, status):
    def intercept(request):
        if request.url.path.endswith("/1AbCdEfGhIjK/export"):
            return httpx.Response(403, json={"error": {"errors": [{"reason": reason}]}})

    raws = fetch_all(connector(_wrap(FakeDrive(), intercept))[0])
    assert raws["1AbCdEfGhIjK"].payload["text_status"] == status


def test_throttled_permissions_are_not_taken_as_unlistable():
    # Treating throttling as "may not list" would give the file its folder's
    # readers (or reject it) when its own permissions were readable all along.
    from swarm.http import SourceError

    def intercept(request):
        if request.url.path.endswith("/1PDFrunbook01/permissions"):
            return THROTTLED

    with pytest.raises(SourceError):
        fetch_all(connector(_wrap(FakeDrive(), intercept))[0])


# -- Word and PowerPoint uploads ------------------------------------------------- #

def test_word_powerpoint_and_excel_uploads_are_read():
    conn, fake = connector()
    raws = fetch_all(conn)
    docx = raws["1DOCXadr00001"].payload
    assert docx["type"] == "docx" and docx["text_status"] == "extracted"
    assert docx["text"].startswith("ADR-007: Put the proxy in front of the IdP\nStatus: Accepted, see AUTH-12\n")
    pptx = raws["1PPTXreview01"].payload
    assert pptx["text"] == "Q4 auth review\n\nOIDC is live for staff\nNext: retire SAML"     # slides apart
    xlsx = raws["1XLSXcosts001"].payload
    assert xlsx["type"] == "xlsx" and xlsx["text_status"] == "extracted"
    assert xlsx["text"] == "Costs\nVendor\tMonthly\nSquid support\t300"


def test_workbook_cells_keep_their_columns_and_dates_read_as_dates():
    s = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    r = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    p = "http://schemas.openxmlformats.org/package/2006/relationships"
    sheet = (f'<worksheet xmlns="{s}"><sheetData>'
             f'<row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1" t="inlineStr"><is><r><t>Rich </t></r><r><t>text</t></r></is></c></row>'
             f'<row r="2"><c r="A2" s="1"><v>46310</v></c><c r="B2" s="2"><v>46310.5</v></c><c r="C2" t="b"><v>1</v></c>'
             f'<c r="D2" s="0"><v>0.25</v></c></row>'
             f'<row r="3"/></sheetData></worksheet>')
    data = _zip({
        "xl/workbook.xml": f'<workbook xmlns="{s}" xmlns:r="{r}"><sheets><sheet name="Log" sheetId="1" r:id="rId9"/>'
                           f"</sheets></workbook>",
        "xl/_rels/workbook.xml.rels": f'<Relationships xmlns="{p}"><Relationship Id="rId9" Target="/xl/worksheets/log.xml"/>'
                                      f"</Relationships>",
        "xl/sharedStrings.xml": f'<sst xmlns="{s}"><si><t>Opened</t><rPh><t>ignored</t></rPh></si></sst>',
        "xl/styles.xml": f'<styleSheet xmlns="{s}"><numFmts><numFmt numFmtId="164" formatCode="yyyy-mm-dd hh:mm"/>'
                         f'</numFmts><cellXfs><xf numFmtId="0"/><xf numFmtId="14"/><xf numFmtId="164"/></cellXfs></styleSheet>',
        "xl/worksheets/log.xml": sheet,
    })
    assert office_text(data, "xlsx") == ("Log\nOpened\t\tRich text\n2026-10-15\t2026-10-15 12:00\tTRUE\t0.25",
                                         "extracted")


@pytest.mark.parametrize(("parts", "status"), [
    ({"xl/workbook.xml": "<workbook/>"}, "unreadable"),                            # no relationships part
    ({"xl/workbook.xml": '<!DOCTYPE x [<!ENTITY a "a">]><workbook/>', "xl/_rels/workbook.xml.rels": "<r/>"}, "unreadable"),
    ({"xl/_rels/workbook.xml.rels": b"<r>" + b" " * (MAX_OFFICE_XML + 1) + b"</r>"}, "too_large"),
    ({"xl/workbook.xml": "<workbook/>", "xl/_rels/workbook.xml.rels": "<r/>"}, "no_text"),   # no sheets
])
def test_hostile_or_empty_workbooks_are_never_fatal(parts, status):
    assert office_text(_zip(parts), "xlsx") == ("", status)


def test_slides_are_read_in_number_order():
    text, status = office_text(make_office("pptx", "\f".join(f"slide {n}" for n in range(1, 12))), "pptx")
    assert status == "extracted" and text.split("\n\n") == [f"slide {n}" for n in range(1, 12)]


def test_text_box_paragraphs_follow_the_paragraph_that_holds_them():
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = (f'<w:document xmlns:w="{w}"><w:body><w:p><w:r><w:t>Before</w:t></w:r><w:r><w:txbxContent>'
            f"<w:p><w:r><w:t>In the box</w:t></w:r></w:p></w:txbxContent></w:r><w:r><w:tab/><w:t>after</w:t>"
            f"</w:r></w:p><w:p><w:r><w:t>Next</w:t></w:r></w:p></w:body></w:document>")
    assert office_text(_zip({"word/document.xml": body}), "docx") == ("Before\tafter\nIn the box\nNext", "extracted")


def _zip(parts: dict[str, str | bytes]) -> bytes:
    import io
    import zipfile
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items():
            z.writestr(name, data)
    return buf.getvalue()


@pytest.mark.parametrize(("data", "status"), [
    (b"PK\x03\x04 not really a zip", "unreadable"),
    (_zip({"[Content_Types].xml": "<Types/>"}), "unreadable"),                     # no word/document.xml
    (_zip({"word/document.xml": "<w:document"}), "unreadable"),                    # broken XML
    (_zip({"word/document.xml": '<?xml version="1.0"?><!DOCTYPE lol [<!ENTITY a "aaaa">]><d>&a;</d>'}),
     "unreadable"),                                                               # entity expansion refused
    (_zip({"word/document.xml": b"<d>" + b" " * (MAX_OFFICE_XML + 1) + b"</d>"}), "too_large"),   # a zip bomb
    (make_office("docx", ""), "no_text"),
])
def test_hostile_or_empty_office_files_are_never_fatal(data, status):
    assert office_text(data, "docx") == ("", status)


def test_an_oversized_office_file_is_not_downloaded():
    fake = FakeDrive()
    fake.files["1DOCXadr00001"]["size"] = "90000000"
    conn, _ = connector(fake)
    raws = fetch_all(conn)
    assert raws["1DOCXadr00001"].payload["text_status"] == "too_large"
    assert "/drive/v3/files/1DOCXadr00001" not in {r.url.path for r in fake.requests if r.url.params.get("alt")}
