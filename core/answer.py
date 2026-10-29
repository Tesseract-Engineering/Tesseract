"""
answer.py - a question, asked as one viewer, to a cited answer.

    question ──▶ search terms ──▶ graph.search (readable, and not) ──▶ model picks quotes ──▶ code checks quotes
                 (model, or                                             (readable items only)   against the stored text
                  plain keywords)

Every answer is INFO, with claims that each quote a readable record word for word, or NOT_FOUND, with
what was searched. A quote the code cannot find in its record is dropped, and an answer left with no
claims is NOT_FOUND: the bot never answers from nowhere.

Matching items the viewer may not read are `locked`: counted, and their owners named, so the card can
offer to ask. The model never sees their text, so nothing it writes can carry it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .graph import Graph, Hit, keywords

MAX_RECORD_CHARS = 6000
READ_LIMIT = 6
# A locked item counts only if it matched about as well as what the viewer can read, so a stray word
# in some private file does not put a lock line on every answer.
LOCKED_SCORE_SHARE = 0.35

EXPAND_SYSTEM = """You turn a question someone asked at work into search terms for a keyword index over their
company's Slack threads, Jira tickets, GitHub pull requests and docs, and Google Drive files.
Return up to 8 terms: the names, systems, tickets and phrases a record answering the question would contain,
including likely synonyms and abbreviations. Short terms; no full sentences.
The question may point at a conversation ("what do you think of this?"); when one is given, take the terms
from what that conversation is about."""

CONTEXT_NOTE = ("What they were looking at when they asked. Use it only to understand the question; it is not a "
                "record, so never quote it or cite it:")

EXPAND_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["terms"],
                 "properties": {"terms": {"type": "array", "items": {"type": "string"}}}}

ANSWER_SYSTEM = """You answer a question for one person at a company, using only the numbered records given,
which are exactly the records this person is allowed to read.

Rules:
- Every claim must be supported by a quote copied word for word from one record's text: an exact,
  contiguous span, at least a few words long, no ellipses, no paraphrase. Name the record by its number.
- Say only what the records say. Do not generalise beyond them: three documents agreeing is "three documents
  say", never "company policy". If records disagree, say so.
- If the records do not answer the question, set status to NOT_FOUND and give no claims.
- A question like "what do you think of this?" asks what the records say about the conversation shown with it:
  related decisions, prior work, owners, conflicts. Answer that from the records; give no opinion of your own.
- headline: one short line, like a card title, saying what the answer is and where it was read from.
- Never mention record numbers in the headline or claims; the card cites each record by name.
- At most 4 claims, most important first."""

ANSWER_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["status", "headline", "claims"],
    "properties": {
        "status": {"type": "string", "enum": ["INFO", "NOT_FOUND"]},
        "headline": {"type": "string"},
        "claims": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["text", "record", "quote"],
            "properties": {"text": {"type": "string"}, "record": {"type": "integer"},
                           "quote": {"type": "string"}}}},
    },
}


@dataclass
class Claim:
    text: str
    hit: Hit
    quote: str


@dataclass
class Answer:
    question: str
    status: str                          # INFO | NOT_FOUND
    headline: str
    claims: list[Claim] = field(default_factory=list)
    locked: list[Hit] = field(default_factory=list)
    searched: list[str] = field(default_factory=list)
    misses: list[Hit] = field(default_factory=list)   # readable near misses, labelled as misses
    context: str | None = None           # the conversation the question pointed at, never a source
    for_group: bool = False              # answered for everyone in an audience: locked items are only counted

    @property
    def cited(self) -> list[Hit]:
        seen: dict[str, Hit] = {}
        for c in self.claims:
            seen.setdefault(c.hit.id, c.hit)
        return list(seen.values())


def _squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def verify(quote: str, text: str) -> str | None:
    """The quote as it appears in the text (whitespace may differ), or None if it is not there."""
    q = _squash(quote).strip(" .\"'“”")
    if len(q) < 12:
        return None
    return q if q in _squash(text) else None


def _asked(question: str, context: str | None) -> str:
    return question if not context else f"Question: {question}\n\n{CONTEXT_NOTE}\n{context}"


def _terms(question: str, model, context: str | None = None) -> list[str]:
    terms = keywords(question)
    if context:      # "what do you think of this?" names nothing; the conversation does
        terms += [k for k in keywords(context) if k.lower() not in {t.lower() for t in terms}][:10]
    if model is None:
        return terms
    try:
        done = model.extract(EXPAND_SYSTEM, _asked(question, context), EXPAND_SCHEMA)
    except Exception:          # the model is a nicety here; keywords alone still search
        return terms
    extra = [t for t in (done.data or {}).get("terms", []) if isinstance(t, str)]
    return terms + [t for t in extra if t.lower() not in {x.lower() for x in terms}]


def _plain(question: str, readable: list[Hit], terms: list[str]) -> tuple[str, list[Claim]]:
    """No model: the sentence of the best record that shares the most terms with the question."""
    lowered = [t.lower() for t in terms]
    for hit in readable:
        sentences = re.split(r"(?<=[.!?])\s+|\n+", hit.text)
        # A heading only names the topic; on a tie the sentence under it says something.
        scored = sorted(((sum(t in s.lower() for t in lowered), s) for s in sentences if len(s.strip()) >= 12),
                        key=lambda x: (-x[0], x[1].lstrip().startswith("#")))
        if scored and scored[0][0] > 0:
            quote = _squash(scored[0][1])
            return hit.title, [Claim(text=quote, hit=hit, quote=quote)]
    return "", []


def answer(graph: Graph, v: dict, question: str, model=None, granted: list[str] | None = None,
           context: str | None = None, audience: list[dict] | None = None) -> Answer:
    """`context` is the conversation a question points at ("what do you think of this?"): it shapes the
    search and the model's reading of the question, and is never quoted as evidence.

    `audience` is everyone who will see the answer (a screen shared on a call): only records each of
    them may read are given to the model; the rest are counted, never named."""
    a = _answer(graph, v, question, model, granted, context, audience)
    a.context = context
    a.for_group = bool(audience)
    return a


def _answer(graph: Graph, v: dict, question: str, model, granted: list[str] | None, context: str | None,
            audience: list[dict] | None = None) -> Answer:
    terms = _terms(question, model, context)
    hits = graph.search(v, terms, granted=granted)
    if audience:
        ids = [h.id for h in hits if h.readable]
        shared = set(ids)
        for member in audience:
            shared &= graph.readable_ids(member, ids)
        for h in hits:
            h.readable = h.readable and h.id in shared
    readable = [h for h in hits if h.readable][:READ_LIMIT]
    best = max((h.score for h in hits), default=0.0)
    locked = [h for h in hits if not h.readable and h.score >= best * LOCKED_SCORE_SHARE]

    if not readable:
        return Answer(question, "NOT_FOUND", "Nothing you can read answers this", locked=locked, searched=terms)

    if model is None:
        headline, claims = _plain(question, readable, terms)
    else:
        records = "\n\n".join(
            f"[{i}] {h.title} ({h.source} {h.kind}{', ' + h.when.date().isoformat() if h.when else ''})\n"
            f"{h.text[:MAX_RECORD_CHARS]}" for i, h in enumerate(readable, 1))
        done = model.extract(ANSWER_SYSTEM, f"{_asked(question, context)}\n\nRecords:\n\n{records}", ANSWER_SCHEMA)
        data = done.data or {"status": "NOT_FOUND", "headline": "", "claims": []}
        headline, claims = data.get("headline", ""), []
        if data.get("status") == "INFO":
            for c in data.get("claims", []):
                i = c.get("record")
                if not isinstance(i, int) or not 1 <= i <= len(readable):
                    continue
                quote = verify(c.get("quote", ""), readable[i - 1].text)
                if quote:
                    claims.append(Claim(text=c.get("text", ""), hit=readable[i - 1], quote=quote))

    if not claims:
        return Answer(question, "NOT_FOUND", "Nothing you can read answers this", locked=locked,
                      searched=terms, misses=readable[:3])
    return Answer(question, "INFO", headline or claims[0].hit.title, claims=claims, locked=locked, searched=terms)
