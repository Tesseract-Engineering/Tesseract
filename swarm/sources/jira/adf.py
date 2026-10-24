"""
adf.py - Atlassian Document Format to plain text.

Jira's v3 API returns descriptions and comment bodies as ADF, a JSON tree.
This turns one into the text an item record carries, and collects the
account ids it @-mentions. Plain code, no model.

  paragraphs, headings      one block each, separated by a newline
  lists                     "- " or "1. " per item, nested items indented
  code blocks               fenced with ```, so link scanning skips them
  quotes                    "> " per line
  tables                    one row per line, cells joined by " | "
  links                     the text, then the URL in parentheses when they differ
  smart links (cards)       the URL
  mentions                  "@Name"; the account id goes to `mentions`, the name to `names`
  emoji, status, dates      their text
  media                     dropped: there is no text in an image
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

_BLOCKS = {"paragraph", "heading", "blockquote", "codeBlock", "bulletList", "orderedList", "table", "panel",
           "rule", "expand", "nestedExpand", "taskList", "decisionList", "mediaSingle", "mediaGroup"}


@dataclass
class Rendered:
    text: str
    mentions: list[str] = field(default_factory=list)      # account ids, in first-seen order
    names: dict[str, str] = field(default_factory=dict)    # account id -> the name the mention shows


def to_text(doc: Any) -> Rendered:
    """`doc` is an ADF document, or None, or (from older payloads) a string."""
    if doc is None:
        return Rendered("")
    if isinstance(doc, str):
        return Rendered(doc.strip())
    mentions: list[tuple[str, str]] = []
    text = _blocks(doc.get("content") or [], mentions)
    names = {account: name for account, name in mentions if name}
    return Rendered(text.strip(), list(dict.fromkeys(a for a, _ in mentions)), names)


def _blocks(nodes: list[dict[str, Any]], mentions: list[tuple[str, str]]) -> str:
    out = []
    for node in nodes:
        block = _block(node, mentions)
        if block:
            out.append(block)
    return "\n".join(out)


def _block(node: dict[str, Any], mentions: list[tuple[str, str]]) -> str:
    kind, content = node.get("type"), node.get("content") or []
    if kind in ("paragraph", "heading"):
        return _inline(content, mentions)
    if kind == "codeBlock":
        return "```\n" + "".join(n.get("text", "") for n in content) + "\n```"
    if kind == "blockquote":
        return "\n".join(f"> {line}" for line in _blocks(content, mentions).splitlines())
    if kind in ("bulletList", "orderedList", "taskList", "decisionList"):
        lines = []
        for i, item in enumerate(content, start=1):
            marker = f"{i}. " if kind == "orderedList" else "- "
            if kind == "taskList":
                marker += "[x] " if (item.get("attrs") or {}).get("state") == "DONE" else "[ ] "
            body = _inline(item["content"], mentions) if item.get("type") in ("taskItem", "decisionItem") \
                else _blocks(item.get("content") or [], mentions)
            first, *rest = body.splitlines() or [""]
            lines.append(marker + first)
            lines += ["  " + line for line in rest]
        return "\n".join(lines)
    if kind == "table":
        rows = []
        for row in content:
            cells = [_blocks(cell.get("content") or [], mentions).replace("\n", " ") for cell in row.get("content") or []]
            rows.append(" | ".join(cells))
        return "\n".join(rows)
    if kind in ("expand", "nestedExpand"):
        title = (node.get("attrs") or {}).get("title") or ""
        body = _blocks(content, mentions)
        return f"{title}\n{body}".strip() if title else body
    if kind in ("blockCard", "embedCard"):
        return (node.get("attrs") or {}).get("url") or ""
    if kind in ("rule", "mediaSingle", "mediaGroup"):
        return ""
    if kind in _BLOCKS or content and isinstance(content[0], dict) and content[0].get("type") in _BLOCKS:
        return _blocks(content, mentions)                   # panel, layout and other containers
    return _inline(content, mentions) if content else _inline([node], mentions)


def _inline(nodes: list[dict[str, Any]], mentions: list[tuple[str, str]]) -> str:
    parts = []
    for node in nodes:
        kind, attrs = node.get("type"), node.get("attrs") or {}
        if kind == "text":
            text = node.get("text", "")
            href = next(((m.get("attrs") or {}).get("href") for m in node.get("marks") or []
                         if m.get("type") == "link"), None)
            parts.append(f"{text} ({href})" if href and href != text else text)
        elif kind == "hardBreak":
            parts.append("\n")
        elif kind == "mention":
            name = (attrs.get("text") or "").lstrip("@")
            if attrs.get("id"):
                mentions.append((attrs["id"], name))
            parts.append(f"@{name}" if name else "@someone")
        elif kind == "emoji":
            parts.append(attrs.get("text") or attrs.get("shortName") or "")
        elif kind == "inlineCard":
            parts.append(attrs.get("url") or "")
        elif kind == "status":
            parts.append(f"[{attrs.get('text', '')}]")
        elif kind == "date" and str(attrs.get("timestamp", "")).isdigit():
            parts.append(datetime.fromtimestamp(int(attrs["timestamp"]) / 1000, timezone.utc).strftime("%Y-%m-%d"))
        elif node.get("content"):
            parts.append(_inline(node["content"], mentions))
    return "".join(parts).strip()
