"""
text.py - turning source files into the text the swarm stores.

Public so that anything that reads a source live (forge) converts it the same
way and compares like with like: a different converter would differ on
whitespace and list markers, not on real changes. Each function returns
(text, text_status), with the statuses the item record uses.

    pdf_text(data)            a PDF
    office_text(data, kind)   a .docx, .pptx or .xlsx, read against byte budgets
    adf_text(doc)             a Jira description or comment (Atlassian Document Format)
"""

from __future__ import annotations

import io
import re
import zipfile
from datetime import datetime, timedelta
from xml.etree import ElementTree

from swarm.sources.jira.adf import to_text as adf_text

__all__ = ["MAX_OFFICE_BYTES", "MAX_OFFICE_XML", "MAX_PDF_BYTES", "adf_text", "office_text", "pdf_text"]

MAX_PDF_BYTES = 25 * 1024 * 1024
MAX_OFFICE_BYTES = 25 * 1024 * 1024    # the download
MAX_OFFICE_XML = 50 * 1024 * 1024      # what its text parts may unzip to, against zip bombs


def pdf_text(data: bytes) -> tuple[str, str]:
    """A PDF's text, pages apart. Returns (text, text_status)."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        text = "\n\n".join((page.extract_text() or "").strip() for page in reader.pages).strip()
    except Exception:                    # pypdf raises many types on a malformed or hostile file
        return "", "unreadable"
    return (text, "extracted") if text else ("", "no_text")


# Office Open XML: a zip of XML parts. Word keeps its body in word/document.xml,
# PowerPoint one part per slide; both put text in runs inside paragraphs.
_WORD = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DRAWING = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_SLIDE = re.compile(r"ppt/slides/slide(\d+)\.xml")


class _Refused(Exception):
    """A part that is over budget ("too_large") or declares a DOCTYPE ("unreadable")."""

    def __init__(self, status: str):
        self.status = status


class _Parts:
    """Reads a zip's XML parts against one byte budget for the whole file."""

    def __init__(self, z: zipfile.ZipFile):
        self.z, self.budget = z, MAX_OFFICE_XML

    def read(self, name: str) -> ElementTree.Element:
        with self.z.open(name) as part:
            xml = part.read(self.budget + 1)
        if len(xml) > self.budget:
            raise _Refused("too_large")
        self.budget -= len(xml)
        if b"<!DOCTYPE" in xml:
            raise _Refused("unreadable")
        return ElementTree.fromstring(xml)

    def optional(self, name: str) -> ElementTree.Element | None:
        return self.read(name) if name in self.z.namelist() else None


def office_text(data: bytes, kind: str) -> tuple[str, str]:
    """A .docx, .pptx or .xlsx file's text: paragraph per line, slides apart,
    and for a workbook each sheet apart, its name then one row per line with
    cells tab-separated. Files come from anyone who can share into the drive,
    so each part is read against a byte budget (a zip's declared sizes can
    lie), and a part that declares a DOCTYPE is refused: OOXML never has one,
    and entity expansion is how XML parsers are attacked."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            parts = _Parts(z)
            if kind == "xlsx":
                blocks = _workbook(parts)
            else:
                if kind == "docx":
                    names, ns = ["word/document.xml"], _WORD
                else:
                    slides = [(int(m.group(1)), n) for n in z.namelist() if (m := _SLIDE.fullmatch(n))]
                    names, ns = [n for _, n in sorted(slides)], _DRAWING
                blocks = [_paragraphs(parts.read(name), ns) for name in names]
    except _Refused as e:
        return "", e.status
    except Exception:                    # a malformed zip or XML raises many types; none is fatal
        return "", "unreadable"
    text = "\n\n".join(b for b in blocks if b).strip()
    return (text, "extracted") if text else ("", "no_text")


# SpreadsheetML: the workbook names the sheets and, through its relationships,
# the part each one lives in. Text cells usually point into one shared-strings
# table, and a number is a date only by its cell style's number format.
_SHEET = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
_PACKAGE_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
_DATE_FORMATS = {14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47}     # the built-in date and time formats
_EPOCH = datetime(1899, 12, 30)                                      # day 0 in the 1900 date system
_CELL = re.compile(r"([A-Z]+)\d+")


def _workbook(parts: _Parts) -> list[str]:
    targets = {r.get("Id"): r.get("Target", "") for r in parts.read("xl/_rels/workbook.xml.rels").iter(_PACKAGE_REL)}
    strings = parts.optional("xl/sharedStrings.xml")
    shared = [_string(si) for si in (strings if strings is not None else []) if si.tag == f"{_SHEET}si"]
    dates = _date_styles(parts.optional("xl/styles.xml"))
    blocks = []
    for sheet in parts.read("xl/workbook.xml").iter(f"{_SHEET}sheet"):
        target = targets.get(sheet.get(_REL_ID), "")
        name = target.lstrip("/") if target.startswith("/") else f"xl/{target}"
        rows = [_row(row, shared, dates) for row in parts.read(name).iter(f"{_SHEET}row")]
        rows = [r for r in rows if r]
        if rows:
            blocks.append("\n".join([sheet.get("name") or "", *rows]))
    return blocks


def _string(node: ElementTree.Element) -> str:
    """A shared or inline string: plain, or rich text in runs. Phonetic runs are skipped."""
    parts = []
    for child in node:
        if child.tag == f"{_SHEET}t":
            parts.append(child.text or "")
        elif child.tag == f"{_SHEET}r":
            parts += [t.text or "" for t in child.iter(f"{_SHEET}t")]
    return "".join(parts)


def _date_styles(styles: ElementTree.Element | None) -> set[int]:
    """Indexes of the cell styles whose number format shows a date or time."""
    if styles is None:
        return set()
    custom = {int(f.get("numFmtId", -1)): f.get("formatCode", "") for f in styles.iter(f"{_SHEET}numFmt")}
    xfs = styles.find(f"{_SHEET}cellXfs")
    out = set()
    for i, xf in enumerate(xfs if xfs is not None else []):
        fmt = int(xf.get("numFmtId", 0))
        code = re.sub(r'"[^"]*"|\[[^\]]*\]|\\.', "", custom.get(fmt, "")).lower()
        if fmt in _DATE_FORMATS or any(c in code for c in "dmyhs"):
            out.add(i)
    return out


def _row(row: ElementTree.Element, shared: list[str], dates: set[int]) -> str:
    cells: list[str] = []
    for c in row.iter(f"{_SHEET}c"):
        if m := _CELL.fullmatch(c.get("r", "")):
            column = 0
            for letter in m.group(1):
                column = column * 26 + ord(letter) - 64
            cells += [""] * (column - 1 - len(cells))
        cells.append(_value(c, shared, dates).replace("\t", " ").replace("\n", " "))
    while cells and not cells[-1]:
        cells.pop()
    return "\t".join(cells)


def _value(c: ElementTree.Element, shared: list[str], dates: set[int]) -> str:
    kind, v = c.get("t", "n"), c.findtext(f"{_SHEET}v") or ""
    if kind == "s":
        return shared[int(v)] if v.isdigit() and int(v) < len(shared) else ""
    if kind == "inlineStr":
        node = c.find(f"{_SHEET}is")
        return _string(node) if node is not None else ""
    if kind == "b":
        return "TRUE" if v == "1" else "FALSE"
    if kind == "n" and v and int(c.get("s", 0)) in dates:
        try:
            when = _EPOCH + timedelta(days=float(v))
        except (ValueError, OverflowError):
            return v
        return when.strftime("%Y-%m-%d" if when.time() == datetime.min.time() else "%Y-%m-%d %H:%M")
    return v


def _paragraphs(root: ElementTree.Element, ns: str) -> str:
    """Each paragraph's text on its own line. A text box inside a paragraph
    holds paragraphs of its own; they follow the one that holds them."""
    lines, queue = [], [root]
    while queue:
        node = queue.pop(0)
        if node.tag == f"{ns}p":
            parts: list[str] = []
            nested: list[ElementTree.Element] = []
            _runs(node, ns, parts, nested)
            lines.append("".join(parts).strip())
            queue[:0] = nested
        else:
            queue[:0] = list(node)
    return "\n".join(line for line in lines if line)


def _runs(node: ElementTree.Element, ns: str, parts: list[str], nested: list[ElementTree.Element]) -> None:
    for child in node:
        if child.tag == f"{ns}t":
            parts.append(child.text or "")
        elif child.tag == f"{ns}tab":
            parts.append("\t")
        elif child.tag in (f"{ns}br", f"{ns}cr"):
            parts.append("\n")
        elif child.tag == f"{ns}p":
            nested.append(child)
        else:
            _runs(child, ns, parts, nested)
