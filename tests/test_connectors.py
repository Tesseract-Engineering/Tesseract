"""Connectors: each export format is built from scratch, read back, and checked."""

import io
import json
import zipfile

import pytest

from tesseract import connectors

DOC_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:body>
<w:p><w:r><w:t>Billing Service Design</w:t></w:r></w:p>
<w:p><w:r><w:t xml:space="preserve">The </w:t></w:r><w:r><w:t>RasterField</w:t></w:r>
<w:r><w:t xml:space="preserve"> owner is Dana.</w:t></w:r></w:p>
<w:tbl><w:tr><w:tc><w:p><w:r><w:t>cell text</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
</w:body></w:document>"""


def minimal_pdf(line: str) -> bytes:
    body = f"BT /F1 12 Tf 72 720 Td ({line}) Tj ET"
    objects = [
        b"<</Type/Catalog/Pages 2 0 R>>",
        b"<</Type/Pages/Kids[3 0 R]/Count 1>>",
        b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>",
        f"<</Length {len(body)}>>stream\n{body}\nendstream".encode(),
        b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>",
    ]
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for i, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj".encode() + obj + b"endobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode())
    out.write(b"0000000000 65535 f \n")
    for off in offsets:
        out.write(f"{off:010d} 00000 n \n".encode())
    out.write(f"trailer<</Size {len(objects) + 1}/Root 1 0 R>>\nstartxref\n{xref}\n%%EOF".encode())
    return out.getvalue()


def test_docx(tmp_path):
    docx_path = tmp_path / "design.docx"
    with zipfile.ZipFile(docx_path, "w") as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types"><Default Extension="xml" '
            'ContentType="application/xml"/></Types>',
        )
        z.writestr("word/document.xml", DOC_XML)

    text = connectors.read_docx(docx_path, 60_000)
    assert "Billing Service Design" in text, "docx: paragraphs read"
    assert "The RasterField owner is Dana." in text, "docx: runs joined within a paragraph"
    assert "cell text" in text, "docx: table cells included"


def test_pdf(tmp_path):
    pdf_path = tmp_path / "postmortem.pdf"
    pdf_path.write_bytes(minimal_pdf("Outage caused by BaseSpatialField"))
    text = connectors.read_pdf(pdf_path, 60_000)
    assert "BaseSpatialField" in text, "pdf: text layer extracted"

    bad_pdf = tmp_path / "broken.pdf"
    bad_pdf.write_bytes(b"not a pdf at all")
    errors = []
    list(connectors.documents(tmp_path, suffixes=(".pdf",), on_error=lambda p, e: errors.append(p.name)))
    assert errors == ["broken.pdf"], "pdf: unreadable file reported, not yielded as empty"


def test_jira_csv(tmp_path):
    # Two rows, and the first has THREE Labels columns with the same header -
    # the case csv.DictReader silently collapses.
    csv_path = tmp_path / "jira.csv"
    csv_path.write_text(
        "Issue key,Summary,Description,Created,Reporter,Status,Labels,Labels,Labels\n"
        "BILL-1,Billing API times out,The billing-api drops requests,2026-01-04,dana,Open,billing,latency,p1\n"
        "BILL-2,Invoice rounding,Off by one cent,2026-01-05,sam,Done,billing,,\n",
        encoding="utf-8",
    )
    rows = list(connectors.jira_csv(csv_path, project="BILL"))
    assert len(rows) == 2, "jira: rows parsed"
    assert rows[0].id == "jira:BILL:BILL-1" and rows[0].title == "Billing API times out", "jira: id and title"
    assert rows[0].meta["labels"] == ["billing", "latency", "p1"], "jira: repeated Labels columns all kept"
    assert rows[1].meta["labels"] == ["billing"], "jira: empty repeats dropped"
    assert rows[0].people[0]["handle"] == "dana", "jira: reporter becomes a person"


def test_slack_export(tmp_path):
    slack = tmp_path / "slack"
    (slack / "eng-billing").mkdir(parents=True)
    (slack / "random").mkdir(parents=True)
    (slack / "users.json").write_text(
        json.dumps(
            [
                {"id": "U1", "name": "dana", "profile": {"real_name": "Dana Ruiz", "email": "dana@x.com"}},
                {"id": "U2", "name": "sam", "profile": {"real_name": "Sam Lee", "email": "sam@x.com"}},
            ]
        ),
        encoding="utf-8",
    )
    (slack / "eng-billing" / "2026-01-04.json").write_text(
        json.dumps(
            [
                {"user": "U1", "ts": "1767513600.000100", "text": "the billing-api is timing out"},
                {
                    "user": "U2",
                    "ts": "1767513700.000200",
                    "thread_ts": "1767513600.000100",
                    "text": "looks like RasterField again",
                },
                {"user": "U1", "ts": "1767513800.000300", "subtype": "channel_join", "text": "joined"},
                {"user": "U2", "ts": "1767513900.000400", "text": "separate thought"},
            ]
        ),
        encoding="utf-8",
    )
    (slack / "random" / "2026-01-04.json").write_text(
        json.dumps(
            [
                {"user": "U1", "ts": "1767520000.000100", "text": "lunch?"},
            ]
        ),
        encoding="utf-8",
    )

    threads = list(connectors.slack_export(slack, private_channels=("eng-billing",)))
    by_id = {t.id: t for t in threads}
    assert len(threads) == 3, "slack: threads not messages"
    main = by_id["slack:eng-billing:1767513600.000100"]
    assert main.text.count("\n") == 1 and "RasterField" in main.text, "slack: reply folded into parent"
    assert "joined" not in main.text, "slack: join noise dropped"
    assert main.text.startswith("Dana Ruiz:"), "slack: speakers named"
    assert {p["handle"] for p in main.people} == {"dana", "sam"}, "slack: both participants captured"
    assert main.visibility == "private" and main.acl_groups == ["slack:eng-billing"], (
        "slack: private channel carries its ACL"
    )
    assert by_id["slack:random:1767520000.000100"].visibility == "internal", "slack: other channel not marked private"


def test_txt(tmp_path):
    (tmp_path / "notes.txt").write_text("A real note about ForeignKey.", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("django==5.2\n", encoding="utf-8")
    docs = {d.id: d for d in connectors.documents(tmp_path, suffixes=(".txt",))}
    assert "doc:notes.txt" in docs, "txt: prose file read"
    assert "doc:requirements.txt" not in docs, "txt: requirements skipped as non-prose"
    assert docs["doc:notes.txt"].meta["format"] == "plaintext", "txt: format recorded on the record"

    with pytest.raises(ValueError):
        list(connectors.documents(tmp_path, suffixes=(".xyz",)))


def test_microsoft_365(tmp_path):
    m365 = tmp_path / "m365"
    sp = m365 / "sharepoint" / "legal" / "contracts"
    sp.mkdir(parents=True)
    (sp / "acme-addendum.md").write_text(
        "Section C: 180 days notice before removing /v1/legacy/export.", encoding="utf-8"
    )
    (sp / "acme-addendum.md.meta.json").write_text(
        json.dumps(
            {
                "author": "Priya Sharma",
                "modified": "2025-11-20T10:00:00Z",
                "site": "legal",
                "library": "contracts",
                "sensitivity": "Confidential",
                "url": "https://sp.example/acme",
            }
        ),
        encoding="utf-8",
    )
    open_lib = m365 / "sharepoint" / "customer-success" / "account-plans"
    open_lib.mkdir(parents=True)
    (open_lib / "plan.md").write_text("Nightly export renewal plan.", encoding="utf-8")
    with zipfile.ZipFile(open_lib / "plan2.docx", "w") as z:
        z.writestr("word/document.xml", DOC_XML)

    sp_docs = {d.id: d for d in connectors.sharepoint_export(m365)}
    assert set(sp_docs) == {
        "sharepoint:legal/contracts/acme-addendum.md",
        "sharepoint:customer-success/account-plans/plan.md",
        "sharepoint:customer-success/account-plans/plan2.docx",
    }, "sharepoint: md, docx read; sidecar not a document"
    addendum = sp_docs["sharepoint:legal/contracts/acme-addendum.md"]
    assert "180 days notice" in addendum.text, "sharepoint: text survives"
    assert addendum.kind == "document" and addendum.source == "sharepoint", "sharepoint: kind/source"
    assert addendum.visibility == "restricted" and addendum.acl_groups == ["sharepoint:legal"], (
        "sharepoint: Confidential -> restricted to site"
    )
    assert (
        addendum.people[0]["name"] == "Priya Sharma"
        and addendum.created_at.startswith("2025-11-20")
        and addendum.url == "https://sp.example/acme"
    ), "sharepoint: sidecar author, modified, url"
    assert addendum.meta["sidecar"] == "sharepoint/legal/contracts/acme-addendum.md.meta.json", (
        "sharepoint: sidecar path recorded for coverage"
    )
    plan = sp_docs["sharepoint:customer-success/account-plans/plan.md"]
    assert plan.visibility == "internal" and plan.acl_groups == [] and plan.meta["site"] == "customer-success", (
        "sharepoint: no sidecar -> internal, site from folder"
    )
    assert "Billing Service Design" in sp_docs["sharepoint:customer-success/account-plans/plan2.docx"].text, (
        "sharepoint: docx via the docx reader"
    )

    thread = {
        "subject": "Export cadence",
        "conversation_id": "conv-1",
        "messages": [
            {
                "from": {"name": "Ops Lead", "email": "ops@acme.example"},
                "to": [{"name": "Cs Owner", "email": "CS@example.com"}],
                "cc": [],
                "date": "2026-09-14T09:00:00Z",
                "body": "Will the nightly export keep working?",
            },
            {
                "from": {"name": "Cs Owner", "email": "cs@example.com"},
                "to": [{"name": "Ops Lead", "email": "ops@acme.example"}],
                "cc": [{"name": "Boss", "email": "boss@example.com"}],
                "date": "2026-09-14T11:00:00Z",
                "body": "Yes, contractually guaranteed through Q4.",
            },
        ],
    }
    for box in ("cs", "boss"):
        (m365 / "outlook" / "mailboxes" / box).mkdir(parents=True)
        (m365 / "outlook" / "mailboxes" / box / "export.json").write_text(json.dumps(thread), encoding="utf-8")
    mail = list(connectors.outlook_export(m365))
    assert len(mail) == 1, "outlook: one record per conversation, not per mailbox"
    mail = mail[0]
    assert mail.id == "outlook:conv-1" and mail.kind == "document" and mail.source == "outlook", (
        "outlook: id, kind, source"
    )
    assert mail.text.splitlines() == [
        "Ops Lead: Will the nightly export keep working?",
        "Cs Owner: Yes, contractually guaranteed through Q4.",
    ], "outlook: text is Name: body lines"
    assert {p["email"] for p in mail.people} == {"ops@acme.example", "cs@example.com", "boss@example.com"}, (
        "outlook: people from from/to/cc, keyed by email"
    )
    assert mail.visibility == "private" and mail.acl_groups == [
        "outlook:user:boss@example.com",
        "outlook:user:cs@example.com",
        "outlook:user:ops@acme.example",
    ], "outlook: private, acl per participant"
    assert len(mail.meta["paths"]) == 2, "outlook: every mailbox copy listed for coverage"
    assert mail.created_at == "2026-09-14T09:00:00Z", "outlook: created_at from first message"

    chan = m365 / "teams" / "customer-success" / "acme-corp"
    chan.mkdir(parents=True)
    (chan / "renewal-prep.json").write_text(
        json.dumps(
            {
                "team": "customer-success",
                "channel": "acme-corp",
                "topic": "Renewal prep",
                "thread_id": "t-1",
                "messages": [
                    {
                        "user": "@dana",
                        "name": "Dana Ruiz",
                        "timestamp": "2026-09-20T15:00:00Z",
                        "text": "ACME's whole finance close runs on that nightly CSV",
                    },
                    {"user": "sam", "name": "Sam Lee", "timestamp": "2026-09-20T15:05:00Z", "text": "noted"},
                ],
            }
        ),
        encoding="utf-8",
    )
    (m365 / "teams" / "chats").mkdir(parents=True)
    (m365 / "teams" / "chats" / "dm-1.json").write_text(
        json.dumps(
            {
                "participants": ["dana", "sam"],
                "topic": "dm",
                "messages": [
                    {"user": "dana", "name": "Dana Ruiz", "timestamp": "2026-09-21T10:00:00Z", "text": "ping"}
                ],
            }
        ),
        encoding="utf-8",
    )
    team_recs = {r.id: r for r in connectors.teams_export(m365)}
    assert set(team_recs) == {"teams:customer-success/acme-corp/t-1", "teams:chat:dm-1"}, (
        "teams: channel thread and chat both read"
    )
    tthread = team_recs["teams:customer-success/acme-corp/t-1"]
    assert (
        tthread.text.startswith("Dana Ruiz: ACME's whole finance close")
        and {p["handle"] for p in tthread.people} == {"dana", "sam"}
        and tthread.source == "teams"
    ), "teams: text and speakers"
    assert tthread.visibility == "internal", "teams: public channel is internal"
    chat = team_recs["teams:chat:dm-1"]
    assert chat.visibility == "private" and chat.acl_groups == ["teams:user:dana", "teams:user:sam"], (
        "teams: chat private to participants"
    )

    assert (
        list(connectors.sharepoint_export(tmp_path / "empty")) == []
        and list(connectors.outlook_export(tmp_path / "empty")) == []
        and list(connectors.teams_export(tmp_path / "empty")) == []
    ), "m365: readers tolerate a root without their folders"
