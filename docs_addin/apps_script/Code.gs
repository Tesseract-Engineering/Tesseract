/**
 * Tesseract for Google Docs: the sidebar's server side.
 *
 * Only when asked: for the text the writer selects (or the paragraph the cursor is in) it asks
 * the add-in API (docs_addin/api.py), as the signed-in user, what the writer should know
 * (conflicts, past incidents, decisions, owners), or answers a question about it. Finds the
 * document's questions and inserts an answer under one when the user clicks Insert. Nothing is
 * written to the document otherwise.
 *
 * Script Properties (Project Settings > Script Properties):
 *   TESSERACT_API_URL   the API's public URL (the tunnel in front of docs_addin/api.py)
 *   TESSERACT_API_KEY   the API's ADDIN_KEY
 *   TESSERACT_VIEWER    optional: answer as this email instead of the signed-in user (demo only)
 */

var ANSWER_BG = '#EEF0FF';
var SOURCE_COLOR = '#616161';

function onOpen() {
  DocumentApp.getUi().createAddonMenu()
    .addItem('Open Tesseract', 'showSidebar')
    .addToUi();
}

function onInstall(e) {
  onOpen(e);
}

function showSidebar() {
  var page = HtmlService.createHtmlOutputFromFile('Sidebar').setTitle('Tesseract');
  DocumentApp.getUi().showSidebar(page);
}

function viewer_() {
  var override = PropertiesService.getScriptProperties().getProperty('TESSERACT_VIEWER');
  return (override || Session.getActiveUser().getEmail() || '').toLowerCase();
}

/**
 * A paragraph's text as a question, or null: it asks something (a "?" ending a sentence, so
 * "How often? Provide evidence of the last rotation." counts) and is long enough to mean something.
 */
function question_(text) {
  var t = (text || '').trim().replace(/^(\d+|[a-z])[.)]\s+/i, '');
  return t.length >= 15 && /\?(\s|$)/.test(t) ? t : null;
}

/** The body paragraph (or list item) holding this question, or null. */
function findParagraph_(question) {
  var body = DocumentApp.getActiveDocument().getBody();
  for (var i = 0; i < body.getNumChildren(); i++) {
    var child = body.getChild(i);
    var type = child.getType();
    if (type !== DocumentApp.ElementType.PARAGRAPH && type !== DocumentApp.ElementType.LIST_ITEM) continue;
    if (question_(child.asText().getText()) === question) return child;
  }
  return null;
}

/** Whether an answer already sits under this paragraph (the shaded one Insert writes). */
function answered_(paragraph) {
  var next = paragraph.getNextSibling();
  if (!next || next.getType() !== DocumentApp.ElementType.PARAGRAPH) return false;
  var text = next.asText();
  return text.getText().length > 0 && text.getBackgroundColor(0) === ANSWER_BG;
}

/** What the sidebar shows on open: the document, who is asking, and its questions in order. */
function getContext() {
  var doc = DocumentApp.getActiveDocument();
  var body = doc.getBody();
  var questions = [];
  for (var i = 0; i < body.getNumChildren(); i++) {
    var child = body.getChild(i);
    var type = child.getType();
    if (type !== DocumentApp.ElementType.PARAGRAPH && type !== DocumentApp.ElementType.LIST_ITEM) continue;
    var q = question_(child.asText().getText());
    if (q) questions.push({text: q, answered: answered_(child)});
  }
  return {title: doc.getName(), url: doc.getUrl(), viewer: viewer_(), questions: questions};
}

/** The nearest heading above a body child: the section a paragraph sits in, or null. */
function section_(body, index) {
  for (var i = index - 1; i >= 0; i--) {
    var child = body.getChild(i);
    if (child.getType() !== DocumentApp.ElementType.PARAGRAPH) continue;
    var heading = child.asParagraph().getHeading();
    if (heading !== DocumentApp.ParagraphHeading.NORMAL && heading !== DocumentApp.ParagraphHeading.TITLE) {
      var text = child.asText().getText().trim();
      if (text) return text;
    }
  }
  return null;
}

/** The paragraph (or list item) an element sits in, or null. */
function block_(el) {
  while (el && el.getType() !== DocumentApp.ElementType.PARAGRAPH && el.getType() !== DocumentApp.ElementType.LIST_ITEM) {
    el = el.getParent();
  }
  return el;
}

/** Headings and the shaded answers Insert wrote are not text to check. */
function checkable_(block) {
  if (block.getType() === DocumentApp.ElementType.PARAGRAPH &&
      block.asParagraph().getHeading() !== DocumentApp.ParagraphHeading.NORMAL) return false;
  var text = block.asText().getText();
  return text.trim().length > 0 && block.asText().getBackgroundColor(0) !== ANSWER_BG;
}

/**
 * What the writer is pointing at: the selected text if there is a selection (across paragraphs,
 * partial ones trimmed to what is selected), else the paragraph the cursor is in. With its
 * section, and whether it is one of the document's questions. The sidebar shows it and asks about
 * it only when the writer clicks; reading it costs nothing.
 */
function getFocus() {
  var doc = DocumentApp.getActiveDocument();
  var body = doc.getBody();
  var selection = doc.getSelection();
  var parts = [], first = null;
  if (selection) {
    selection.getRangeElements().forEach(function (r) {
      var el = r.getElement();
      var block = block_(el);
      if (!block || !checkable_(block)) return;
      var text = el.editAsText ? el.asText().getText() : '';
      if (r.isPartial()) text = text.substring(r.getStartOffset(), r.getEndOffsetInclusive() + 1);
      text = text.trim();
      if (!text) return;
      if (!first) first = block;
      parts.push(text);
    });
  } else if (doc.getCursor()) {
    var block = block_(doc.getCursor().getElement());
    if (block && checkable_(block)) {
      first = block;
      parts.push(block.asText().getText().trim());
    }
  }
  if (!first) return null;
  var text = parts.join('\n');
  return {text: text, selected: !!selection, section: section_(body, body.getChildIndex(first)),
          question: parts.length === 1 ? question_(text) : null};
}

/** POST to the API. Returns its JSON, or {error} for the sidebar to show. */
function call_(path, payload) {
  var props = PropertiesService.getScriptProperties();
  var url = props.getProperty('TESSERACT_API_URL');
  var key = props.getProperty('TESSERACT_API_KEY');
  if (!url || !key) return {error: 'Set TESSERACT_API_URL and TESSERACT_API_KEY in Script Properties.'};
  payload.viewer = viewer_();
  var response = UrlFetchApp.fetch(url.replace(/\/$/, '') + path, {
    method: 'post',
    contentType: 'application/json',
    headers: {'X-Addin-Key': key},
    payload: JSON.stringify(payload),
    muteHttpExceptions: true
  });
  var body;
  try {
    body = JSON.parse(response.getContentText());
  } catch (e) {
    return {error: 'The Tesseract API returned ' + response.getResponseCode() + '.'};
  }
  if (response.getResponseCode() !== 200) return {error: body.error || ('HTTP ' + response.getResponseCode())};
  return body;
}

/** Answer a question: one of the document's, or one typed in the sidebar, about the selection. */
function ask(question, about) {
  return call_('/answer', {question: question, about: about || null});
}

/** What the writer should know about the text they selected. */
function askContext(paragraph, section) {
  return call_('/context', {paragraph: paragraph, section: section, doc: DocumentApp.getActiveDocument().getName()});
}

/**
 * Put an answer under its question: the answer shaded, then a small "Sources" line whose
 * entries link to the items cited. Docs has no API for suggestions or anchored comments, so
 * the shading marks what Tesseract wrote and the links carry the provenance.
 */
function insertAnswer(question, answer, sources) {
  var paragraph = findParagraph_(question);
  if (!paragraph) throw new Error('That question is no longer in the document.');
  var body = DocumentApp.getActiveDocument().getBody();
  var at = body.getChildIndex(paragraph) + 1;

  var line = body.insertParagraph(at, answer);
  line.setHeading(DocumentApp.ParagraphHeading.NORMAL);
  line.editAsText().setBackgroundColor(ANSWER_BG).setBold(false).setItalic(false);

  if (sources && sources.length) {
    var cite = body.insertParagraph(at + 1, 'Sources: ');
    cite.setHeading(DocumentApp.ParagraphHeading.NORMAL);
    var text = cite.editAsText();
    text.setBackgroundColor(null).setFontSize(9).setForegroundColor(SOURCE_COLOR).setBold(false).setItalic(false);
    sources.forEach(function (s, i) {
      var start = text.getText().length;
      var piece = '[' + s.n + '] ' + s.label;
      text.appendText(piece + (i < sources.length - 1 ? '; ' : ''));
      if (s.url) text.setLinkUrl(start, start + piece.length - 1, s.url);
    });
    text.setFontSize(9).setForegroundColor(SOURCE_COLOR).setBackgroundColor(null);
  }
  return true;
}
