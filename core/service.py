"""
service.py - the Tesseract core: the one place that reads the graph, and holds threads and approvals.

    Slack bot, meeting page ──(in process)──┐
    Docs sidebar (Apps Script) ──HTTPS──────┼──▶ Core ──▶ answers  (core/answer.py: quoted claims;
    ticketing bot ──HTTP────────────────────┘     │                 docs_addin/: cited notes and answers)
                                                  ├──▶ threads    (core/threads.py)
                                                  └──▶ approvals  (exact payload, hash, owner approves)

Every answer is read from the graph as the person asking (graph/swarm_queries.py), or for an
audience when everyone on a call sees it. A thread is context for an answer, never a source.
Bots perform the writes in their own tool (a Slack post, a doc insert, a Jira issue), and only
after the core says the thread's owner approved that exact payload.

Whatever changes a thread is announced to `subscribe`rs (the meeting page, the Slack bot), so a
ticket made by the ticketing bot shows up on the call's screen and in the owner's Slack.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from collections import Counter
from typing import Callable

from graph.store import Neo4jStore

from .answer import Answer, answer
from .graph import Graph
from .ledger import Ledger
from .threads import Denied, Threads, context_text

log = logging.getLogger("core")

PROPOSE_SYSTEM = """You read a stretch of a work call and list what the people on it decided and what \
they left open. Only what was said on the call; never infer a decision nobody made.
- decisions: at most 3, each one short sentence (under 15 words), with said_by, the speaker label as \
given, or "" if unclear.
- questions: at most 3 open questions the call raised and did not settle, each one short sentence.
Return empty lists when there are none."""

PROPOSE_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["decisions", "questions"],
    "properties": {
        "decisions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["text", "said_by"],
            "properties": {"text": {"type": "string"}, "said_by": {"type": "string"}}}},
        "questions": {"type": "array", "items": {"type": "string"}},
    },
}

DRAFT_SYSTEM = """You write a short Slack message, in the sender's voice, asking a colleague to review a \
design doc. Use only what the working notes say: what the doc is about, what was decided (only \
lines marked "Decided:"; records that were asked about are not decisions), what is still open (lines \
marked "Open question:"). When no line is marked, leave that part out; never say nothing was decided. \
Address the reviewer as {reviewer} (keep that placeholder exactly) and include the doc \
link exactly as given. At most 60 words, plain, no emoji, no greeting line of its own."""

DRAFT_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["text"],
                "properties": {"text": {"type": "string"}}}

SOURCE = {"gdrive": "Drive", "slack": "Slack", "jira": "Jira", "github": "GitHub"}
KIND = {"document": "doc", "thread": "thread", "issue": "ticket", "pull_request": "PR", "commit": "commit"}

REVIEW_INTENT =re.compile(r"\bdraft\b.*\b(review|message|post|request)\b", re.I)
TICKET_INTENT = re.compile(r"\b(make|create|open|file)\b.*\b(ticket|issue)\b", re.I)


def chat_model_from_env():
    """The swarm's model client, called directly (no batches: someone is waiting) at low effort."""
    from swarm import model as m

    backend = os.environ.get("SWARM_MODEL_BACKEND", "anthropic")
    name = os.environ.get("SWARM_MODEL", m.DEFAULT_MODEL)
    effort = os.environ.get("SLACKBOT_MODEL_EFFORT", "low")
    if backend == "off":
        return None
    if backend == "anthropic":
        return m.ClaudeApi(name, effort=effort, batch=False)
    if backend == "bedrock":
        return m.Bedrock(name, effort=effort)
    return m.from_env()


def answer_json(a: Answer) -> dict:
    """The API's shape for a quoted answer: every claim with its quote and record, the locked
    items counted (and, for one person, who owns them), never their titles."""
    def where(h) -> str:
        return f"{SOURCE.get(h.source, h.source)} {KIND.get(h.kind, h.kind)}".strip()

    owners = []
    if not a.for_group:
        for h in a.locked:
            for o in h.owners[:1]:
                if o.name not in owners:
                    owners.append(o.name)
    return {
        "question": a.question, "status": a.status, "headline": a.headline,
        "claims": [{"text": c.text if c.text.strip() != c.quote else "", "quote": c.quote, "record": c.hit.id,
                    "title": c.hit.title, "url": c.hit.url, "where": where(c.hit),
                    "when": c.hit.when.date().isoformat() if c.hit.when else None, "authors": c.hit.authors[:2]}
                   for c in a.claims],
        "locked": {"count": len(a.locked), "owners": owners} if not a.for_group else {"count": 0, "owners": []},
        "hidden": len(a.locked) if a.for_group else 0,
        "searched": a.searched[:8], "misses": [{"title": h.title, "url": h.url} for h in a.misses],
        "context": bool(a.context),
    }


class Core:
    def __init__(self, store: Neo4jStore | None = None, threads: Threads | None = None, ledger: Ledger | None = None,
                 model=None, docs=None):
        self.ns = os.environ.get("TESSERACT_NS") or os.environ.get("SLACKBOT_NS") or "kg:swarm"
        self.store = store or Neo4jStore()
        self.graph = Graph(self.store, self.ns)
        self.ledger = ledger or Ledger(os.environ.get("SLACKBOT_DB", "data/slackbot/tesseract.db"))
        self.threads = threads or Threads(os.environ.get("TESSERACT_THREADS_DB", "data/core/threads.db"))
        self.model = model if model is not None else chat_model_from_env()
        self._docs = docs
        self.listeners: list[Callable[[dict], None]] = []
        self.slack: dict | None = None        # {"team", "bot_user"} once the Slack bot is up, for handoff links
        self.lock = threading.Lock()

    # -- events -------------------------------------------------------------------------------- #

    def subscribe(self, fn: Callable[[dict], None]) -> None:
        self.listeners.append(fn)

    def emit(self, event: dict) -> None:
        for fn in list(self.listeners):
            try:
                fn(event)
            except Exception:
                log.exception("listener failed on %s", event.get("type"))

    def _changed(self, tid: str | None, what: str, **more) -> None:
        if tid:
            self.emit({"type": "thread", "what": what, "thread": tid, **more})

    # -- answers ------------------------------------------------------------------------------- #

    def granted(self, email: str) -> list[str]:
        return self.ledger.granted(email)

    def answer(self, asker: str, question: str, context: str | None = None, thread: str | None = None,
               audience: list[str] | None = None, granted_extra: list[str] | None = None) -> Answer:
        """A quoted answer as `asker`, or for everyone in `audience` (who all see it). `context` and
        the thread steer the search and the reading; neither is ever quoted."""
        if thread:
            working = context_text(self.threads.get(thread, asker, proposed=False))
            context = "\n\n".join(c for c in (context, working) if c)
        v = self.graph.viewer(asker)
        granted = sorted(set(self.granted(asker)) | set(granted_extra or []))
        group = [self.graph.viewer(e) for e in audience] if audience else None
        return answer(self.graph, v, question, self.model, granted=granted, context=context or None, audience=group)

    def working(self, person: str) -> str | None:
        """The context of the thread a person is working in, if any."""
        t = self.threads.active(person)
        return context_text(self.threads.get(t["id"], person, proposed=False)) if t else None

    def everyone_can_read(self, emails: list[str], ids: list[str]) -> bool:
        for e in emails:
            v = self.graph.viewer(e)
            if not set(ids) <= self.graph.readable_ids(v, ids, self.granted(e)):
                return False
        return True

    # -- the Docs engine (docs_addin/): notes on a passage, cited answers, a drafted section ---- #

    @property
    def docs(self):
        with self.lock:
            if self._docs is None:
                from docs_addin.api import App

                self._docs = App(self.ns, key="", store=self.store)
            return self._docs

    def _thread_text(self, thread: str | None, viewer: str) -> str | None:
        return context_text(self.threads.get(thread, viewer, proposed=False)) if thread else None

    def docs_answer(self, viewer: str, question: str, about: str | None = None, thread: str | None = None) -> dict:
        return self.docs.ask(question, viewer, about, working=self._thread_text(thread, viewer))

    def docs_context(self, viewer: str, paragraph: str, section: str | None, doc: str | None,
                     thread: str | None = None) -> dict:
        return self.docs.context(paragraph, section, doc, viewer, working=self._thread_text(thread, viewer))

    def docs_draft(self, viewer: str, thread: str, doc_url: str | None = None, doc_title: str | None = None) -> dict:
        """A Background section for the doc from what the records say about the thread's topic, cited,
        and the thread's open questions. Inserting it is an action the owner approves by its hash."""
        t = self.threads.get(thread, viewer, proposed=False)
        out = self.docs.ask(f"What do the company's records already establish about {t['title']}? "
                            "Write it as the Background section of a design doc.", viewer,
                            working=context_text(t))
        questions = [e.get("text", "") for e in t["entries"] if e["kind"] == "question"]
        payload = {"doc": doc_url, "heading": "Background", "text": out.get("answer", ""),
                   "sources": [{"n": s["n"], "label": s["label"], "url": s.get("url")} for s in out.get("sources", [])],
                   "questions": questions}
        out["questions"] = questions
        if out.get("status") != "none" or questions:
            out["approval"] = self.propose(viewer, "docs", "insert_section", payload, thread)
        return out

    # -- threads ------------------------------------------------------------------------------- #

    def open_thread(self, owner: str, title: str, audience: list[str] | None = None, surface: str = "zoom") -> dict:
        t = self.threads.create(owner, title, audience)
        self.threads.activate(t["id"], owner)
        self._changed(t["id"], "opened", surface=surface)
        return self.threads.get(t["id"], owner)

    def append(self, thread: str, by: str, surface: str, kind: str, **data) -> dict:
        e = self.threads.append(thread, by, surface, kind, **data)
        self._changed(thread, "entry", entry=e)
        return e

    def decide(self, thread: str, entry: str, by: str, keep: bool) -> dict:
        e = self.threads.decide(thread, entry, by, keep)
        self._changed(thread, "entry", entry=e)
        return e

    def close(self, thread: str, by: str) -> dict:
        t = self.threads.close(thread, by)
        self._changed(thread, "closed")
        return t

    def handoff(self, thread: str, by: str, to: str, surface: str) -> dict:
        """Continue in Docs, Continue in Slack: the thread becomes the one `by` is working in, and the
        surface it goes to is told (the Slack bot DMs them)."""
        t = self.threads.activate(thread, by)
        names = {"docs": "Docs", "slack": "Slack", "tickets": "ticketing"}
        e = self.append(thread, by, surface, "note", text=f"Continued in {names.get(to, to)}")
        self.emit({"type": "handoff", "thread": thread, "title": t["title"], "by": by, "to": to, "from": surface})
        return {"thread": t["id"], "entry": e, "slack": self.slack if to == "slack" else None}

    def suggest(self, thread: str, by: str, transcript: str, surface: str = "zoom") -> list[dict]:
        """Decisions and open questions the call proposes, waiting for the owner to accept them."""
        if not transcript.strip() or self.model is None:
            return []
        done = self.model.extract(PROPOSE_SYSTEM, transcript, PROPOSE_SCHEMA)
        data = done.data or {}
        out = []
        for d in (data.get("decisions") or [])[:3]:
            if d.get("text", "").strip():
                out.append(self.append(thread, by, surface, "decision", text=d["text"].strip(),
                                       said_by=d.get("said_by") or None))
        for q in (data.get("questions") or [])[:3]:
            if isinstance(q, str) and q.strip():
                out.append(self.append(thread, by, surface, "question", text=q.strip()))
        return out

    # -- approvals ----------------------------------------------------------------------------- #

    def propose(self, by: str, surface: str, action: str, payload: dict, thread: str | None = None) -> dict:
        a = self.threads.propose(by, surface, action, payload, thread)
        self._changed(thread, "approval", approval=a)
        return a

    def approve(self, aid: str, by: str, h: str) -> dict:
        a = self.threads.approve(aid, by, h)
        self._changed(a["thread_id"], "approval", approval=a)
        self.emit({"type": "approved", "approval": a})
        return a

    def done(self, aid: str, surface: str, result: dict, ok: bool = True) -> dict:
        a = self.threads.done(aid, surface, result, ok)
        self._changed(a["thread_id"], "approval", approval=a)
        self.emit({"type": "done", "approval": a})
        return a

    def cancel(self, aid: str, by: str) -> dict:
        a = self.threads.cancel(aid, by)
        self._changed(a["thread_id"], "approval", approval=a)
        return a

    # -- what a thread hands on: a review request, a ticket brief ------------------------------ #

    def _records(self, t: dict) -> list[str]:
        ids: list[str] = []
        for e in t["entries"]:
            if e["kind"] == "answer":
                ids += [r for r in e.get("records", []) if r not in ids]
        return ids

    def shared_records(self, t: dict):
        """The records the thread's answers rest on, as items everyone in its audience may read: a
        brief or a review request goes wider than any one asker."""
        ids = self._records(t)
        if not ids:
            return []
        ok = set(ids)
        for e in t["audience"]:
            ok &= self.graph.readable_ids(self.graph.viewer(e), ids, self.granted(e))
        return [h for h in self.graph.items([i for i in ids if i in ok])]

    def reviewer(self, t: dict, asker: str):
        """Who to ask for review: whoever owns most of the records the thread rests on, not the asker."""
        counts: Counter = Counter()
        people = {}
        for h in self.shared_records(t):
            for o in h.owners:
                if o.email and o.email.lower() == asker.lower():
                    continue
                counts[o.name] += 1
                people[o.name] = o
        return people[counts.most_common(1)[0][0]] if counts else None

    def draft_review(self, thread: str, by: str, channel: str) -> dict:
        """A review request for the thread's doc, in `by`'s voice, as an approval: nothing posts until
        the thread's owner approves this exact text."""
        t = self.threads.get(thread, by, proposed=False)
        links = [e for e in t["entries"] if e["kind"] == "link" and e.get("url")]
        if not links:
            raise Denied("This thread has no doc yet. Pick it in the Docs sidebar first.")
        doc = links[-1]
        who = self.reviewer(t, by)
        tag = f"<@{who.slack_id}>" if who and who.slack_id else (who.name if who else "team")
        notes = context_text(t) + f"\nDoc link: {doc['url']}"
        text = None
        if self.model is not None:
            done = self.model.extract(DRAFT_SYSTEM, notes, DRAFT_SCHEMA)
            text = (done.data or {}).get("text")
        if not text or "{reviewer}" not in text or doc["url"] not in text:
            text = f"{{reviewer}} could you review {doc.get('title') or 'the design doc'} for {t['title']}? {doc['url']}"
        text = text.replace("{reviewer}", tag)
        payload = {"channel": channel, "text": text, "doc": doc["url"], "reviewer": who.name if who else None}
        return self.propose(by, "slack", "post_message", payload, thread)

    def brief(self, thread: str, caller: str) -> dict:
        """The thread condensed for a ticket and the coding agent that picks it up: the decision, the
        open questions, the doc, the owner, and the records it rests on, as links."""
        t = self.threads.get(thread, caller, proposed=False)
        decisions = [e.get("text", "") for e in t["entries"] if e["kind"] == "decision"]
        questions = [e.get("text", "") for e in t["entries"] if e["kind"] == "question"]
        latest = {}            # a doc linked again (moved, re-opened) counts once, at its newest URL
        for e in t["entries"]:
            if e["kind"] == "link":
                latest.pop(e.get("title") or e.get("url"), None)
                latest[e.get("title") or e.get("url")] = {"title": e.get("title"), "url": e.get("url")}
        docs = list(latest.values())
        records = [{"id": h.id, "title": h.title, "url": h.url, "source": h.source} for h in self.shared_records(t)]
        lines = [f"## {t['title']}", "", f"Owner: {t['owner']}"]
        if decisions:
            lines += ["", "### Decided", *(f"- {d}" for d in decisions)]
        if questions:
            lines += ["", "### Open questions", *(f"- {q}" for q in questions)]
        if docs:
            lines += ["", "### Design doc", *(f"- [{d['title'] or d['url']}]({d['url']})" for d in docs)]
        if records:
            lines += ["", "### Records this rests on",
                      *(f"- [{r['title']}]({r['url']})" if r["url"] else f"- {r['title']}" for r in records)]
        lines += ["", f"Thread {t['id']} (Tesseract)."]
        return {"thread": t["id"], "title": t["title"], "owner": t["owner"], "decisions": decisions,
                "questions": questions, "docs": docs, "records": records, "markdown": "\n".join(lines)}

    def propose_ticket(self, thread: str, by: str) -> dict:
        b = self.brief(thread, by)
        payload = {"summary": b["title"], "description": b["markdown"], "owner": b["owner"],
                   "records": [r["url"] for r in b["records"] if r["url"]]}
        return self.propose(by, "tickets", "create_ticket", payload, thread)
