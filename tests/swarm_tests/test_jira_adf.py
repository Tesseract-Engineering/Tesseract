"""ADF to text: what a person reads, and who is @-mentioned."""

from swarm.sources.jira.adf import to_text


def doc(*blocks):
    return {"type": "doc", "version": 1, "content": list(blocks)}


def para(*inline):
    return {"type": "paragraph", "content": list(inline)}


def t(text, **marks):
    node = {"type": "text", "text": text}
    if "href" in marks:
        node["marks"] = [{"type": "strong"}, {"type": "link", "attrs": {"href": marks["href"]}}]
    return node


def test_paragraphs_headings_breaks_and_links():
    rendered = to_text(doc(
        {"type": "heading", "attrs": {"level": 2}, "content": [t("Plan")]},
        para(t("Line one"), {"type": "hardBreak"}, t("see "), t("the doc", href="https://example.com/doc")),
        para(t("https://example.com/same", href="https://example.com/same")),
        {"type": "rule"},
    ))
    assert rendered.text == "Plan\nLine one\nsee the doc (https://example.com/doc)\nhttps://example.com/same"


def test_mentions_are_named_in_text_and_collected_once():
    m = {"type": "mention", "attrs": {"id": "712020:abc", "text": "@Sam Lee"}}
    rendered = to_text(doc(para(m, t(" and "), m, t(" and "), {"type": "mention", "attrs": {"id": "x:2"}})))
    assert rendered.text == "@Sam Lee and @Sam Lee and @someone"
    assert rendered.mentions == ["712020:abc", "x:2"]
    assert rendered.names == {"712020:abc": "Sam Lee"}


def test_lists_nest_and_tasks_show_state():
    nested = {"type": "bulletList", "content": [{"type": "listItem", "content": [para(t("inner"))]}]}
    rendered = to_text(doc(
        {"type": "orderedList", "content": [
            {"type": "listItem", "content": [para(t("first")), nested]},
            {"type": "listItem", "content": [para(t("second"))]}]},
        {"type": "taskList", "content": [
            {"type": "taskItem", "attrs": {"state": "DONE"}, "content": [t("ship it")]},
            {"type": "taskItem", "attrs": {"state": "TODO"}, "content": [t("tell sales")]}]},
    ))
    assert rendered.text == "1. first\n  - inner\n2. second\n- [x] ship it\n- [ ] tell sales"


def test_code_is_fenced_quotes_marked_and_tables_flattened():
    rendered = to_text(doc(
        {"type": "codeBlock", "content": [t("make deploy\nmake verify")]},
        {"type": "blockquote", "content": [para(t("quoted")), para(t("twice"))]},
        {"type": "table", "content": [
            {"type": "tableRow", "content": [{"type": "tableHeader", "content": [para(t("Owner"))]},
                                             {"type": "tableHeader", "content": [para(t("Service"))]}]},
            {"type": "tableRow", "content": [{"type": "tableCell", "content": [para(t("Sam"))]},
                                             {"type": "tableCell", "content": [para(t("auth"))]}]}]},
    ))
    assert rendered.text == "```\nmake deploy\nmake verify\n```\n> quoted\n> twice\nOwner | Service\nSam | auth"


def test_cards_emoji_status_dates_panels_and_media():
    rendered = to_text(doc(
        para({"type": "inlineCard", "attrs": {"url": "https://github.com/acme/api/pull/2"}},
             t(" "), {"type": "emoji", "attrs": {"shortName": ":tada:", "text": "🎉"}},
             t(" "), {"type": "status", "attrs": {"text": "BLOCKED"}},
             t(" due "), {"type": "date", "attrs": {"timestamp": "1792022400000"}}),
        {"type": "panel", "attrs": {"panelType": "info"}, "content": [para(t("inside a panel"))]},
        {"type": "mediaSingle", "content": [{"type": "media", "attrs": {"id": "x"}}]},
        {"type": "blockCard", "attrs": {"url": "https://docs.google.com/document/d/1AbC/edit"}},
    ))
    assert rendered.text == ("https://github.com/acme/api/pull/2 🎉 [BLOCKED] due 2026-10-15\n"
                             "inside a panel\nhttps://docs.google.com/document/d/1AbC/edit")


def test_empty_and_legacy_bodies():
    assert to_text(None).text == "" and to_text(doc()).text == ""
    assert to_text("  wiki text from an old payload ").text == "wiki text from an old payload"
