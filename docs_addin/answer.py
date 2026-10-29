"""
answer.py - one question, answered from what retrieve.py found, with every claim cited.

The model sees the question and the viewer's readable sources, each under a key (S1, S2...), and
returns JSON: the answer with [S#] markers after each claim, whether the sources answer it fully,
and what they leave open. Code then keeps only markers that name a real source, numbers the cited
sources 1..n in the order the answer uses them, and drops the rest; a source the model invents
cannot reach the sidebar.

The model is swarm/model.py's, configured separately:

  ADDIN_MODEL          claude-opus-5-5 (default)
  ADDIN_MODEL_EFFORT   low | medium (default) | high
  ADDIN_MODEL_REPLAY   a directory of recorded answers, as SWARM_MODEL_REPLAY; with
                       ADDIN_MODEL_BACKEND=replay only recordings answer (the eval in CI)
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

from swarm.model import ClaudeApi, Model, Replay

from .retrieve import Context, Source

PROMPT_VERSION = "answer-v2"

SYSTEM = """You answer questions inside a company document (an audit response, a design review, a \
questionnaire) from the company's own records: Jira tickets, GitHub pull requests and commits, \
Slack threads and Drive documents. The person asking will paste your answer into the document, \
where people outside the team may read it.

Rules:
- Use only the sources given. Every sentence that states a fact ends with the keys of the sources \
that support it, like [S3] or [S2][S7]. Never state a fact no source supports, and never guess a \
date, number, name or schedule.
- Prefer the current state. A draft, a "Copy of" a document, a "FINAL (1)" duplicate, a proposal, \
or an overview dated before later decisions is history, not the current state; where it disagrees \
with a later decision or ticket, follow the later one. Do not cite such a source at all when a \
current one supports the same sentence; cite it only to say what used to be true, and say so.
- A source that only repeats the question (the questionnaire itself) is not evidence.
- Answer the question that was asked, within its scope. If it asks about authentication \
endpoints, other endpoints that were removed are out of scope.
- Write plainly and specifically for the document: who, what, which ticket or PR, what is still \
open. At most 120 words. No preamble, no "based on the sources". Refer to people by name; where a \
pronoun is needed, use they/them.
- status: "answered" when the sources answer every part of the question; "partial" when some \
part is not established by any source; "none" when no source bears on it (then answer is empty).
- gaps: each part of the question no source establishes, one short sentence each (for example \
"No rotation schedule is defined."). Empty when status is "answered".
- ask: the one person, named in the sources or in the locked note, best placed to fill the gaps \
or share what is locked, with a short reason; empty strings when nobody fits."""

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["status", "answer", "gaps", "ask_person", "ask_reason"],
    "properties": {
        "status": {"type": "string", "enum": ["answered", "partial", "none"]},
        "answer": {"type": "string"},
        "gaps": {"type": "array", "items": {"type": "string"}},
        "ask_person": {"type": "string"},
        "ask_reason": {"type": "string"},
    },
}

SOURCE_LABEL = {"github": "GitHub", "jira": "Jira", "slack": "Slack", "gdrive": "Drive"}
MARKER = re.compile(r"\[S(\d+)\]")


def model_from_env() -> Model:
    name = os.environ.get("ADDIN_MODEL", "claude-opus-5-5")
    effort = os.environ.get("ADDIN_MODEL_EFFORT", "medium")
    replay = os.environ.get("ADDIN_MODEL_REPLAY", "").strip()
    if os.environ.get("ADDIN_MODEL_BACKEND") == "replay":
        return Replay(Path(replay) / name)
    live = ClaudeApi(name, effort=effort, batch=False)
    return Replay(Path(replay) / name, record=live) if replay else live


def label(s: Source) -> str:
    """How a source is named in the sidebar: the Jira key, the PR, the channel, the doc title."""
    if s.source == "jira" and s.alias:
        return f"{s.alias} {s.title}"
    if s.source == "slack":
        return f"Slack{' #' + s.container.lstrip('#') if s.container else ''}: {s.title}"
    if s.source == "github" and s.kind in ("pull_request", "issue") and s.alias:
        return f"{s.alias}: {s.title}"
    return s.title


def prompt(question: str, ctx: Context, viewer_name: str | None, today: str) -> str:
    parts = [f"Today: {today}", f"Asked by: {viewer_name or ctx.viewer['email']}", f"Question: {question}", "",
             "Sources (only what the asker may read):"]
    for i, s in enumerate(ctx.sources, 1):
        head = [f"[S{i}] {SOURCE_LABEL.get(s.source, s.source)} {s.kind}: {label(s)}"]
        if s.updated:
            head.append(f"updated {s.updated}")
        if s.people:
            head.append("by " + ", ".join(s.people))
        if s.decisions:
            head.append("decisions recorded here: " + "; ".join(s.decisions))
        parts += [" | ".join(head), s.text.strip(), ""]
    if not ctx.sources:
        parts.append("(none)")
    if ctx.locked.count:
        owners = ", ".join(o["name"] for o in ctx.locked.owners if o["name"]) or "unknown"
        where = ", ".join(f"{n} {SOURCE_LABEL.get(k, k)}" for k, n in ctx.locked.by_source.items())
        parts.append(f"Locked: the sources above link to {ctx.locked.count} more items ({where}) the asker "
                     f"cannot read. People who could share them: {owners}. Do not guess at their contents.")
    return "\n".join(parts)


def finish(data: dict, ctx: Context, people: dict[str, str]) -> dict:
    """The model's JSON, made safe to show: markers renumbered to the cited sources, unknown
    markers removed, the person to ask matched to someone the graph knows."""
    order: list[int] = []

    def renumber(m: re.Match) -> str:
        k = int(m.group(1))
        if not 1 <= k <= len(ctx.sources):
            return ""
        if k not in order:
            order.append(k)
        return f"[{order.index(k) + 1}]"

    text = MARKER.sub(renumber, data.get("answer", "")).strip()
    text = re.sub(r"\s+([.,;])", r"\1", re.sub(r"[ \t]{2,}", " ", text))
    cited = [ctx.sources[k - 1] for k in order]
    status = data.get("status", "none")
    if status != "none" and not cited:
        status = "none"           # an answer that cites nothing is not one we can show
        text = ""
    ask = None
    name = (data.get("ask_person") or "").strip()
    match = next((n for n in people if name and (n.lower() == name.lower() or name.lower() in n.lower())), None)
    if match:
        ask = {"name": match, "email": people[match], "reason": (data.get("ask_reason") or "").strip().rstrip(".")}
    return {
        "status": status,
        "answer": text,
        "sources": [{"n": i, "title": s.title, "label": label(s), "source": s.source, "kind": s.kind,
                     "alias": s.alias, "url": s.url, "updated": s.updated, "people": s.people}
                    for i, s in enumerate(cited, 1)],
        "read": len(ctx.sources),
        "gaps": [g.strip() for g in data.get("gaps", []) if g and g.strip()],
        "ask": ask,
        "locked": {"count": ctx.locked.count, "by_source": ctx.locked.by_source, "owners": ctx.locked.owners},
    }


def answer(model: Model, question: str, ctx: Context, people: dict[str, str], today: str | None = None) -> dict:
    viewer_name = next((n for n, e in people.items() if e == ctx.viewer["email"]), None)
    done = model.extract(SYSTEM, prompt(question, ctx, viewer_name, today or date.today().isoformat()), SCHEMA)
    if done.data is None:
        raise RuntimeError(f"no answer from {done.model} (stop reason {done.stop_reason})")
    out = finish(done.data, ctx, people)
    out.update(question=question, model=done.model, prompt=PROMPT_VERSION,
               tokens={"input": done.tokens.input, "output": done.tokens.output})
    return out
