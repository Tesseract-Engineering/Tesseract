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

# A thread the writer picked (core/threads.py): what they are in the middle of. It helps read the
# question and the paragraph; it is not a source, so it gets no key and nothing may cite it. Only
# prompts with a thread carry it, so recordings made without one keep replaying.
WORKING_NOTE = ("Working context, what the writer is in the middle of (a call, decisions, open questions). Use "
                "it only to understand what they are doing; it is not a source, so never cite it or state a fact "
                "from it:")


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


def _sources(ctx: Context, who: str) -> list[str]:
    """The readable sources under their keys, then the locked note: the part of every prompt that
    carries evidence."""
    parts = [f"Sources (only what the {who} may read):"]
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
        parts.append(f"Locked: the sources above link to {ctx.locked.count} more items ({where}) the {who} "
                     f"cannot read. People who could share them: {owners}. Do not guess at their contents.")
    return parts


def prompt(question: str, ctx: Context, viewer_name: str | None, today: str, about: str | None = None,
           working: str | None = None) -> str:
    parts = [f"Today: {today}", f"Asked by: {viewer_name or ctx.viewer['email']}"]
    if working:
        parts.append(f"{WORKING_NOTE}\n{working}")
    if about:
        parts.append(f"About this passage of the document they are writing: {about}")
    parts += [f"Question: {question}", ""]
    return "\n".join(parts + _sources(ctx, "asker"))


class Citations:
    """[S#] markers across one or more texts, renumbered [1]..[n] in order of first use; markers
    naming no source are dropped, so a source the model invents cannot reach the sidebar."""

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self.order: list[int] = []

    def _one(self, m: re.Match) -> str:
        k = int(m.group(1))
        if not 1 <= k <= len(self.ctx.sources):
            return ""
        if k not in self.order:
            self.order.append(k)
        return f"[{self.order.index(k) + 1}]"

    def text(self, raw: str) -> str:
        text = MARKER.sub(self._one, raw or "").strip()
        return re.sub(r"\s+([.,;])", r"\1", re.sub(r"[ \t]{2,}", " ", text))

    def sources(self) -> list[dict]:
        return [{"n": i, "title": s.title, "label": label(s), "source": s.source, "kind": s.kind,
                 "alias": s.alias, "url": s.url, "updated": s.updated, "people": s.people}
                for i, s in enumerate((self.ctx.sources[k - 1] for k in self.order), 1)]


def _ask(data: dict, people: dict[str, str]) -> dict | None:
    """The person the model says to ask, matched to someone the graph knows, or None."""
    name = (data.get("ask_person") or "").strip()
    match = next((n for n in people if name and (n.lower() == name.lower() or name.lower() in n.lower())), None)
    if not match:
        return None
    return {"name": match, "email": people[match], "reason": (data.get("ask_reason") or "").strip().rstrip(".")}


def _locked(ctx: Context) -> dict:
    return {"count": ctx.locked.count, "by_source": ctx.locked.by_source, "owners": ctx.locked.owners}


def finish(data: dict, ctx: Context, people: dict[str, str]) -> dict:
    """The model's JSON, made safe to show: markers renumbered to the cited sources, unknown
    markers removed, the person to ask matched to someone the graph knows."""
    cite = Citations(ctx)
    text = cite.text(data.get("answer", ""))
    status = data.get("status", "none")
    if status != "none" and not cite.order:
        status = "none"           # an answer that cites nothing is not one we can show
        text = ""
    return {
        "status": status,
        "answer": text,
        "sources": cite.sources(),
        "read": len(ctx.sources),
        "gaps": [g.strip() for g in data.get("gaps", []) if g and g.strip()],
        "ask": _ask(data, people),
        "locked": _locked(ctx),
    }


def answer(model: Model, question: str, ctx: Context, people: dict[str, str], today: str | None = None,
           about: str | None = None, working: str | None = None) -> dict:
    """`about` is the passage the asker had selected, when the question is about it. `working` is
    the writer's thread, as context only."""
    viewer_name = next((n for n, e in people.items() if e == ctx.viewer["email"]), None)
    done = model.extract(SYSTEM, prompt(question, ctx, viewer_name, today or date.today().isoformat(), about,
                                        working), SCHEMA)
    if done.data is None:
        raise RuntimeError(f"no answer from {done.model} (stop reason {done.stop_reason})")
    out = finish(done.data, ctx, people)
    out.update(question=question, model=done.model, prompt=PROMPT_VERSION,
               tokens={"input": done.tokens.input, "output": done.tokens.output})
    return out


# -- context for the paragraph being written ---------------------------------------------------- #

CONTEXT_VERSION = "context-v2"

CONTEXT_SYSTEM = """You sit beside an engineer writing a design document. Given the paragraph they \
are on (and its section), and the company's records the writer may read (Jira, GitHub, Slack, \
Drive), say what they would want to know before they go on: what it contradicts, what went wrong \
last time someone did this, what was already decided that it should follow, and who to talk to.

Return at most three notes, the most important first. Each note has:
- kind: "conflict" when the paragraph contradicts a current decision, a deprecation, or a plan \
(say what and why it matters); "incident" when it repeats something that caused an incident or \
bug before; "decided" when a settled decision governs what the paragraph is about and the \
paragraph does not already follow it; "owner" when a specific person or team owns what it touches \
and the writer should talk to them.
- title: at most eight words, plain ("JWKS fetched once broke web-app").
- text: at most 45 words, specific (tickets, PRs, dates, numbers, names), every fact followed by \
the keys of its sources, like [S3] or [S2][S5].

Rules:
- Use only the sources. Never invent a ticket, date, number or name.
- Only what bears on this paragraph: each note is about what this paragraph itself proposes or \
claims. A risk that belongs to another part of the design (one the writer will reach in its own \
section) is not a note here. Do not restate what the paragraph already says, and do not flag a \
paragraph for agreeing with a decision. Background the writer clearly knows is not a note.
- A paragraph about the document itself (reviewers, deadlines, status, formatting) gets no notes.
- "owner" only when the paragraph proposes changing something that person or team owns.
- Prefer the current state: drafts, "Copy of" documents and old overviews are history.
- Refer to people by name; where a pronoun is needed, use they/them.
- If nothing in the sources would change what the writer does next, return no notes. Silence is \
better than noise."""

CONTEXT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["notes"],
    "properties": {
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["kind", "title", "text"],
                "properties": {
                    "kind": {"type": "string", "enum": ["conflict", "incident", "decided", "owner"]},
                    "title": {"type": "string"},
                    "text": {"type": "string"},
                },
            },
        },
    },
}

MAX_NOTES = 3


def context_prompt(paragraph: str, section: str | None, doc: str | None, ctx: Context,
                   viewer_name: str | None, today: str, working: str | None = None) -> str:
    parts = [f"Today: {today}", f"Writer: {viewer_name or ctx.viewer['email']}"]
    if working:
        parts.append(f"{WORKING_NOTE}\n{working}")
    if doc:
        parts.append(f"Document: {doc}")
    if section:
        parts.append(f"Section: {section}")
    parts += [f"Paragraph: {paragraph}", ""]
    return "\n".join(parts + _sources(ctx, "writer"))


def finish_context(data: dict, ctx: Context) -> dict:
    """Notes made safe to show: one numbering of sources across all notes, and a note that cites
    nothing is dropped (it is the model's opinion, not the company's record)."""
    cite = Citations(ctx)
    notes = []
    for n in (data.get("notes") or [])[:MAX_NOTES]:
        before = len(cite.order)
        text = cite.text(n.get("text", ""))
        if not re.search(r"\[\d+\]", text):
            del cite.order[before:]
            continue
        notes.append({"kind": n.get("kind", "decided"), "title": (n.get("title") or "").strip(), "text": text})
    return {"notes": notes, "sources": cite.sources(), "read": len(ctx.sources), "locked": _locked(ctx)}


def context(model: Model, paragraph: str, section: str | None, doc: str | None, ctx: Context,
            people: dict[str, str], today: str | None = None, working: str | None = None) -> dict:
    viewer_name = next((n for n, e in people.items() if e == ctx.viewer["email"]), None)
    done = model.extract(CONTEXT_SYSTEM, context_prompt(paragraph, section, doc, ctx, viewer_name,
                                                        today or date.today().isoformat(), working), CONTEXT_SCHEMA)
    if done.data is None:
        raise RuntimeError(f"no answer from {done.model} (stop reason {done.stop_reason})")
    out = finish_context(done.data, ctx)
    out.update(paragraph=paragraph, model=done.model, prompt=CONTEXT_VERSION,
               tokens={"input": done.tokens.input, "output": done.tokens.output})
    return out
