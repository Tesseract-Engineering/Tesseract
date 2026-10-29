"""
make_seed.py - a throwaway Apps Script file that writes demo_doc.json into the open Google Doc.

Typing the demo doc by hand does not work: Docs capitalizes "billing-service", curls apostrophes and
turns "1. Summary" into a list, and the text must match demo_doc.json exactly for the recorded
answers to replay. This prints a Seed.gs to paste into the Apps Script project next to Code.gs. It
writes every block (headings, paragraphs, bullets, styled tables, the figures fetched from the
API's GET /figures/ with the key, and their captions) and renames the doc. Apps Script files
share one scope, so it does not define onOpen: add the menu line its header names to Code.gs while
seeding, and remove both afterwards.

    python -m docs_addin.apps_script.make_seed > /tmp/Seed.gs
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DOC = Path(__file__).parent.parent / "demo_doc.json"

TEMPLATE = r"""/**
 * TEMPORARY (docs_addin/apps_script/make_seed.py): writes the demo design doc into the open doc.
 * Add .addItem('Load demo text (temporary)', 'seedDemoDoc') to onOpen in Code.gs, reopen the doc,
 * run it from Extensions > Tesseract for Docs, then remove that line and delete this file.
 */
function seedDemoDoc() {
  var DOC = __DOC__;
  var props = PropertiesService.getScriptProperties();
  var base = props.getProperty('TESSERACT_API_URL').replace(/\/$/, '');
  var key = props.getProperty('TESSERACT_API_KEY');
  var P = DocumentApp.ParagraphHeading;
  var HEAD = {title: P.HEADING1, h2: P.HEADING2, h3: P.HEADING3};
  var d = DocumentApp.getActiveDocument();
  var body = d.getBody();
  body.clear();
  function styleTable(t, headerRow, headerCol) {
    t.setBorderColor('#dadce0');
    for (var r = 0; r < t.getNumRows(); r++) {
      var row = t.getRow(r);
      for (var c = 0; c < row.getNumCells(); c++) {
        var cell = row.getCell(c);
        cell.setPaddingTop(4).setPaddingBottom(4).setPaddingLeft(6).setPaddingRight(6);
        cell.editAsText().setFontSize(9.5);
        if ((headerRow && r === 0) || (headerCol && c === 0)) {
          cell.setBackgroundColor('#f1f3f4');
          cell.editAsText().setBold(true);
        }
      }
    }
  }
  DOC.blocks.forEach(function (b) {
    if (HEAD[b.style]) {
      body.appendParagraph(b.text).setHeading(HEAD[b.style]);
    } else if (b.style === 'p') {
      body.appendParagraph(b.text).setHeading(P.NORMAL).setSpacingAfter(6);
    } else if (b.style === 'bullets') {
      b.items.forEach(function (t) { body.appendListItem(t).setGlyphType(DocumentApp.GlyphType.BULLET); });
    } else if (b.style === 'meta') {
      styleTable(body.appendTable(b.rows), false, true);
    } else if (b.style === 'table') {
      styleTable(body.appendTable([b.header].concat(b.rows)), true, false);
    } else if (b.style === 'figure') {
      var blob = UrlFetchApp.fetch(base + '/figures/' + b.src, {headers: {'X-Addin-Key': key}}).getBlob();
      var p = body.appendParagraph('');
      var img = p.appendInlineImage(blob);
      var w = 468, h = Math.round(img.getHeight() * w / img.getWidth());
      img.setWidth(w).setHeight(h);
      p.setAlignment(DocumentApp.HorizontalAlignment.CENTER);
      var cap = body.appendParagraph(b.caption);
      cap.setAlignment(DocumentApp.HorizontalAlignment.CENTER).setSpacingAfter(10);
      cap.editAsText().setItalic(true).setFontSize(9).setForegroundColor('#5f6368');
    }
  });
  if (body.getNumChildren() > 1 && body.getChild(0).getType() === DocumentApp.ElementType.PARAGRAPH &&
      body.getChild(0).asText().getText() === '') body.getChild(0).removeFromParent();
  d.setName(DOC.title);
}
"""


def main() -> None:
    doc = json.loads(DOC.read_text(encoding="utf-8"))
    sys.stdout.write(TEMPLATE.replace("__DOC__", json.dumps(doc)))


if __name__ == "__main__":
    main()
