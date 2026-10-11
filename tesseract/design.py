"""design.py - a system design diagram of the existing work a build request should reuse.

When someone asks to build something that already exists, "it exists" is not
enough; they need to see how it works and where their use case plugs in. This
draws that from the cited records:

    records the viewer may read  ->  model  ->  {title, inputs, stages, outputs, loop}

Grounding rules match the rest of the layer: the model sees only records the
viewer can read (fetched with the viewer's ACL), each stage names the record it
came from, and anything not in the records is left out. The one invented
element is the asker's own use case, which is marked `new` so the UI can show
it as the plug-in point rather than as existing infrastructure.
"""

from __future__ import annotations

import json
import re
from typing import Callable

from . import graph, llm

MAX_TEXT = 6_000
MAX_IDS = 5

Q_READABLE = """
MATCH (d) WHERE d.ns = $ns AND d.id IN $ids
  AND (coalesce(d.visibility, 'internal') IN ['internal', 'public']
       OR any(g IN coalesce(d.aclGroups, []) WHERE g IN $acl))
RETURN d.id AS id, coalesce(d.title, '') AS title, coalesce(d.text, d.body, '') AS text
"""

SYSTEM = """You draw system design diagrams from company documents. A colleague wants to build something,
and the company already runs a system that does it. From the documents ONLY, describe that existing
system's architecture, and show where the colleague's new use case plugs in.

Return ONLY a JSON object, no prose:
{"title": "<the existing system's name>",
 "inputs": [{"label": "...", "detail": "...", "new": false, "source": "<doc id>"}],
 "stages": [{"label": "...", "detail": "...", "kind": "step"|"gate", "source": "<doc id>"}],
 "outputs": [{"label": "...", "detail": "...", "source": "<doc id>"}],
 "loop": {"label": "...", "detail": "...", "source": "<doc id>"},
 "plug_in": "<one sentence: what the colleague adds, and which stages they reuse unchanged>"}
Rules:
- stages: the pipeline in order, 4 to 9 of them. kind "gate" for checks that can stop a run.
- inputs: 2 to 4 things feeding the pipeline. Add exactly ONE input with "new": true for the
  colleague's use case (for example their model plus its config); its source is "".
- outputs: 1 to 3. loop: the feedback or rollback path, or null if the documents name none.
- labels at most 4 words; details at most 12 words, with the numbers the documents give.
- Use only components, numbers and names that appear in the documents. Every non-new item needs
  the id of the document it came from."""

Run = Callable[..., list[dict]]


def _json(raw: str | None) -> dict | None:
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _match(source: str, ids: set[str]) -> str:
    """The cited record a source names: exact id, or the one id it is part of or
    contains (models shorten 'doc:engineering/x/docs/ct.md' to 'ct.md')."""
    if source in ids:
        return source
    hits = [i for i in ids if source and (source in i or i in source)]
    return hits[0] if len(hits) == 1 else ""


def _items(values, ids: set[str], limit: int, allow_new: bool = False) -> list[dict]:
    out = []
    for v in values or []:
        if not isinstance(v, dict) or not str(v.get("label") or "").strip():
            continue
        new = bool(v.get("new")) and allow_new
        source = _match(str(v.get("source") or ""), ids)
        if not new and not source:
            continue                      # an existing component must come from a cited record
        out.append({"label": str(v["label"]).strip()[:40], "detail": str(v.get("detail") or "").strip()[:110],
                    "kind": "gate" if v.get("kind") == "gate" else "step", "new": new,
                    "source": "" if new else source})
        if len(out) >= limit:
            break
    return out


def diagram(viewer: dict, question: str, node_ids: list[str], run: Run = graph.run) -> dict:
    """{"diagram": {...}} or {"diagram": None, "why": ...}. Never raises for model trouble."""
    if not llm.available():
        return {"diagram": None, "why": "no model configured"}
    rows = run(Q_READABLE, ids=list(node_ids)[:MAX_IDS], acl=viewer.get("acl_groups") or [])
    if not rows:
        return {"diagram": None, "why": "none of these records are readable by this viewer"}
    ids = {r["id"] for r in rows}
    docs = "\n\n".join(f"=== {r['id']} ({r['title']}) ===\n{(r['text'] or '')[:MAX_TEXT]}" for r in rows)
    for _ in range(2):                    # one retry: a cut-off or malformed reply is the usual failure
        raw = llm.generate(SYSTEM, f"Colleague's request: {question.strip()}\n\nDOCUMENTS:\n{docs}",
                           max_tokens=3000, temperature=0.0)
        data = _json(raw) or {}
        stages = _items(data.get("stages"), ids, 9)
        if len(stages) >= 3:
            break
    else:
        return {"diagram": None, "why": "the model did not return a grounded pipeline"}
    inputs = _items(data.get("inputs"), ids, 4, allow_new=True)
    loop = _items([data.get("loop")] if isinstance(data.get("loop"), dict) else [], ids, 1)
    titles = {r["id"]: r["title"] for r in rows}
    return {"diagram": {
        "title": str(data.get("title") or "Existing system").strip()[:80],
        "inputs": inputs, "stages": stages, "outputs": _items(data.get("outputs"), ids, 3),
        "loop": loop[0] if loop else None,
        "plug_in": str(data.get("plug_in") or "").strip()[:240],
        "sources": [{"id": i, "title": titles[i]} for i in titles]}}
