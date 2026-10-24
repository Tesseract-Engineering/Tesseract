"""A fake Drive API built from tests/fixtures/gdrive/acme.json.

Serves drives.list, files.list (a shared drive's corpus with a modifiedTime
filter, or a folder's children), files.get (metadata, or a PDF's bytes with
alt=media), files.export and permissions.list through httpx.MockTransport,
in small pageToken pages so pagination is exercised. PDFs are built on the
fly: a text PDF from the file's _text, an image-only one for a scan; so are
Word and PowerPoint files (make_office). Every request is recorded.
"""

from __future__ import annotations

import copy
import io
import json
import re
import zipfile
from datetime import datetime
from xml.sax.saxutils import escape

import httpx

from .github_fixture import FIXTURES, planted

NOW_ISO = "2026-10-21T12:00:00Z"
NOW = datetime.fromisoformat(NOW_ISO.replace("Z", "+00:00"))
PAGE = 2


def load() -> dict:
    text = (FIXTURES / "gdrive" / "acme.json").read_text(encoding="utf-8")
    tokens = planted()
    text = re.sub(r"\{\{PLANTED:(\w+)\}\}",
                  lambda m: json.dumps(tokens[m.group(1)])[1:-1] if m.group(1) in tokens else m.group(0), text)
    data = json.loads(text)
    data.pop("_about")
    return data


OFFICE = {"application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
          "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx"}
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"


def make_office(kind: str, text: str) -> bytes:
    """A minimal .docx (one paragraph per line, split into two runs to show
    runs join) or .pptx (one slide per \\f-separated block, one paragraph
    per line), as a zip of the XML parts the connector reads."""
    def paras(block: str, ns: str, pre: str) -> str:
        out = []
        for line in block.split("\n"):
            half = len(line) // 2
            runs = "".join(f"<{pre}:r><{pre}:t xml:space=\"preserve\">{escape(t)}</{pre}:t></{pre}:r>"
                           for t in (line[:half], line[half:]) if t)
            out.append(f"<{pre}:p>{runs}</{pre}:p>")
        return "".join(out)

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        if kind == "docx":
            z.writestr("word/document.xml", f'<w:document xmlns:w="{W}"><w:body>{paras(text, W, "w")}'
                                            f"</w:body></w:document>")
        else:
            # Slide 10 before slide 2 in the zip: order comes from the number, not the listing.
            blocks = text.split("\f")
            for n in sorted(range(1, len(blocks) + 1), key=str):
                z.writestr(f"ppt/slides/slide{n}.xml",
                           f'<p:sld xmlns:p="urn:p" xmlns:a="{A}"><p:cSld><p:spTree><p:sp><p:txBody>'
                           f"{paras(blocks[n - 1], A, 'a')}</p:txBody></p:sp></p:spTree></p:cSld></p:sld>")
    return buf.getvalue()


def make_pdf(text: str | None) -> bytes:
    """A one-page PDF. With text, a Helvetica text stream pypdf can read;
    without, a page that only draws a box, like a scan with no OCR layer."""
    if text is None:
        content = b"0 0 m 100 100 l S"
    else:
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        content = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


class FakeDrive:
    def __init__(self, data: dict | None = None):
        self.data = data or load()
        self.requests: list[httpx.Request] = []

    @property
    def files(self) -> dict[str, dict]:
        return {f["id"]: f for f in self.data["files"]}

    @staticmethod
    def public(f: dict) -> dict:
        return {k: v for k, v in f.items() if not k.startswith("_")}

    def permissions(self, file_id: str):
        if file_id in self.data["drive_permissions"]:
            return self.data["drive_permissions"][file_id]
        perms = self.files[file_id]["_permissions"]
        if isinstance(perms, str):
            kind, target = perms[1:].split(":", 1)
            return self.data["drive_permissions"][target] if kind == "drive" else self.permissions(target)
        return perms

    def _page(self, items: list, request: httpx.Request, key: str) -> httpx.Response:
        start = int(request.url.params.get("pageToken") or 0)
        body = {key: items[start:start + PAGE]}
        if start + PAGE < len(items):
            body["nextPageToken"] = str(start + PAGE)
        return httpx.Response(200, json=body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, q = request.url.path, request.url.params
        if path == "/drive/v3/drives":
            return self._page(self.data["drives"], request, "drives")
        if path == "/drive/v3/files":
            query = q.get("q", "")
            if q.get("corpora") == "drive":
                found = [f for f in self.data["files"] if f.get("driveId") == q["driveId"]]
            else:
                parent = re.match(r"'([^']+)' in parents", query).group(1)
                found = [f for f in self.data["files"] if parent in (f.get("parents") or [])]
            if m := re.search(r"modifiedTime >= '([^']+)'", query):
                found = [f for f in found if f["modifiedTime"][:19] >= m.group(1)[:19]]
            return self._page([self.public(f) for f in found], request, "files")
        m = re.fullmatch(r"/drive/v3/files/([^/]+)(/export|/permissions)?", path)
        if not m or (m.group(1) not in self.files and m.group(1) not in self.data["drive_permissions"]):
            return httpx.Response(404, json={"error": {"code": 404, "message": "File not found"}})
        file_id, action = m.groups()
        if action == "/permissions":
            perms = self.permissions(file_id)
            if perms == 403:
                return httpx.Response(403, json={"error": {"code": 403, "message": "insufficientFilePermissions"}})
            return self._page(perms, request, "permissions")
        f = self.files[file_id]
        if action == "/export":
            assert q["mimeType"] == "text/plain"
            return httpx.Response(200, text=f["_text"], headers={"content-type": "text/plain; charset=utf-8"})
        if q.get("alt") == "media":
            if f["mimeType"] in OFFICE:
                return httpx.Response(200, content=make_office(OFFICE[f["mimeType"]], f["_text"]),
                                      headers={"content-type": f["mimeType"]})
            return httpx.Response(200, content=make_pdf(f["_text"]), headers={"content-type": "application/pdf"})
        return httpx.Response(200, json=self.public(f))

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def connector(fake: FakeDrive | None = None, *, now=NOW, drives=("Engineering",), folders=("1CLUBfolder01",),
              **kwargs):
    from swarm.http import Client
    from swarm.sources.gdrive import DriveConnector

    fake = fake or FakeDrive()
    client = Client("https://www.googleapis.com", transport=fake.transport(), sleep=lambda s: None)
    conn = DriveConnector(client, drives=list(drives) if drives else None, folders=list(folders or []),
                          service_account=fake.data["service_account"], now=now, **kwargs)
    return conn, fake


def mutated(**changes) -> dict:
    """The fixture with some files' fields changed: mutated(id={field: value})."""
    data = copy.deepcopy(load())
    for f in data["files"]:
        f.update(changes.get(f["id"], {}))
    return data
