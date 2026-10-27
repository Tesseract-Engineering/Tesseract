"""
text.py - turning source files into the text the swarm stores.

Public so that anything that reads a source live (forge) converts it the same
way and compares like with like: a different converter would differ on
whitespace and list markers, not on real changes. Each function returns
(text, text_status), with the statuses the item record uses.

    pdf_text(data)            a PDF
    office_text(data, kind)   a .docx or .pptx, read against byte budgets
    adf_text(doc)             a Jira description or comment (Atlassian Document Format)
"""

from __future__ import annotations

import io
import re
import zipfile
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


def office_text(data: bytes, kind: str) -> tuple[str, str]:
    """A .docx or .pptx file's text, paragraph per line, slides apart. Files
    come from anyone who can share into the drive, so each part is read
    against a byte budget (a zip's declared sizes can lie), and a part that
    declares a DOCTYPE is refused: OOXML never has one, and entity expansion
    is how XML parsers are attacked."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            if kind == "docx":
                parts, ns = ["word/document.xml"], _WORD
            else:
                slides = [(int(m.group(1)), n) for n in z.namelist() if (m := _SLIDE.fullmatch(n))]
                parts, ns = [n for _, n in sorted(slides)], _DRAWING
            budget, blocks = MAX_OFFICE_XML, []
            for name in parts:
                with z.open(name) as part:
                    xml = part.read(budget + 1)
                if len(xml) > budget:
                    return "", "too_large"
                budget -= len(xml)
                if b"<!DOCTYPE" in xml:
                    return "", "unreadable"
                blocks.append(_paragraphs(ElementTree.fromstring(xml), ns))
    except Exception:                    # a malformed zip or XML raises many types; none is fatal
        return "", "unreadable"
    text = "\n\n".join(b for b in blocks if b).strip()
    return (text, "extracted") if text else ("", "no_text")


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
