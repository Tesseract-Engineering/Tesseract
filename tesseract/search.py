"""
search.py - the language-model layer over the keyword search path.

The keyword path in answer.py is honest but literal: "what is this company
about" has one usable term ("company"), which is in thousands of records, so
nothing clears the gate and the answer is NOT_FOUND even though the company's
about page is sitting in the graph. This module lets a model fix that in a few
narrow places, and nowhere else:

    1. expand    question -> search terms + likely entity names (JSON)
    2. select    question + readable candidates -> which candidates answer it,
                 a verbatim quote from each, and a one-line headline (JSON)
    3. verify    question + selected quotes -> which quotes state the answer

The model never queries the graph, never sees a document the viewer may not
read (candidates are fetched with the viewer's ACL), and never invents a
citation: every pick must be a candidate id and every quote must be a verbatim
substring of that candidate's text, or it is replaced by the deterministic
quote() helper. If the model is disabled, errors, or says the question is not
answerable from the candidates, the caller keeps the keyword result.
"""

from __future__ import annotations

import json
import re
import time

from . import llm

EXPAND_MAX_TOKENS = 300
SELECT_MAX_TOKENS = 700
CANDIDATES = 24
SNIPPET = 420
MAX_PICKS = 5

EXPAND_SYSTEM = """You turn an employee's question about their company into search terms for a
full-text index over the company's documents, tickets, chat and email.
Return ONLY a JSON object, no prose, shaped exactly:
{"terms": ["..."], "entities": ["..."], "intent": "..."}
- terms: 4 to 10 single words or short phrases likely to appear IN the documents that answer the
  question (synonyms, the nouns the answer would contain). Lowercase. No stopwords.
  For "what is this company about" use terms like "about", "mission", "overview", "founded",
  "company profile", "products", "customers", "platform".
- entities: proper nouns the question names (people, customers, services, ticket keys), else [].
- intent: 3-8 words saying what the asker wants."""

SELECT_SYSTEM = """You are the evidence selector for a company knowledge system. You are given a question
and numbered candidate records (id, title, source, date, snippet). The viewer is allowed to
read every candidate shown. Decide which candidates actually answer the question.
Return ONLY a JSON object, no prose, shaped exactly:
{"answerable": true|false,
 "picks": [{"id": "<candidate id>", "quote": "<verbatim text copied from that candidate's snippet>"}],
 "headline": "<one or two plain sentences answering the question, using only facts in the picks>"}
Rules:
- A pick must directly answer the question. Topical overlap is not enough.
- If the question asks for a judgement or a decision (can we, should we, is it safe to), no record
  will say yes or no. Then answerable=true when the candidates state the facts the decision turns
  on (what was agreed, which party was which, what a policy or playbook requires, what someone
  with authority said about it), pick each of those, and let the headline state those facts and
  what they add up to. Records that are merely about the same subject are still not picks.
- If the question asks for a specific fact (an amount, a percentage, a name, a date, a clause,
  a policy value), a pick must STATE that fact. A record about a related but different thing
  (a different remedy, a different customer, a different policy) is not an answer: leave it out,
  and if nothing states the fact set answerable=false.
- For a question about the company as a whole (what it does, its mission, products, customers,
  history), prefer company-profile / about / overview / handbook records over announcements,
  audit workpapers or individual project documents.
- quote MUST be copied verbatim from the snippet of that candidate, 10 to 220 characters.
- At most 5 picks, best first. If nothing answers the question set answerable=false, picks=[]
  and headline="".
- The headline states facts from the picks only. No speculation, no advice, no mention of
  "candidates" or "snippets". Name the company or entity the facts are about."""


def _json(text: str | None) -> dict | None:
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    m = re.search(r"\{.*\}", t, re.DOTALL)
    if not m:
        return None
    try:
        out = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return out if isinstance(out, dict) else None


def _clean_terms(values, limit: int) -> list[str]:
    out: list[str] = []
    for v in values or []:
        if not isinstance(v, str):
            continue
        v = re.sub(r"[^A-Za-z0-9 _\-/.]+", " ", v).strip().lower()
        if 2 <= len(v) <= 40 and v not in out:
            out.append(v)
        if len(out) >= limit:
            break
    return out


def expand(question: str) -> dict:
    """Search terms and entity names for `question`. {} when disabled or on error."""
    raw = llm.generate(EXPAND_SYSTEM, f"Question: {question}", max_tokens=EXPAND_MAX_TOKENS,
                       temperature=0.0)
    data = _json(raw) or {}
    return {"terms": _clean_terms(data.get("terms"), 10),
            "entities": _clean_terms(data.get("entities"), 6),
            "intent": str(data.get("intent") or "")[:80]}


def lucene(terms: list[str]) -> str:
    parts = []
    for t in terms:
        t = t.replace('"', "").replace("\\", "").strip()
        if t:
            parts.append('"' + t + '"')
    return " OR ".join(parts)


def _snippet(text: str, terms: list[str]) -> str:
    """The SNIPPET characters of `text` around the first term found, else its head."""
    text = " ".join((text or "").split())
    low = text.lower()
    for t in terms:
        pos = low.find(t.lower())
        if pos >= 0:
            start = max(0, pos - SNIPPET // 3)
            return text[start:start + SNIPPET]
    return text[:SNIPPET]


def select(question: str, candidates: list[dict], terms: list[str]) -> dict:
    """Ask the model which readable candidates answer the question. Every pick
    is validated against the candidate list; quotes are checked verbatim."""
    if not candidates:
        return {"answerable": False, "picks": [], "headline": ""}
    by_id = {c["id"]: c for c in candidates}
    lines = []
    for i, c in enumerate(candidates, 1):
        lines.append(f"[{i}] id={c['id']}\n    title: {c.get('title') or ''}\n"
                     f"    source: {c.get('source') or ''}   date: {c.get('date') or ''}\n"
                     f"    snippet: {c['snippet']}")
    user = f"Question: {question}\n\nCandidates:\n" + "\n".join(lines)
    raw = llm.generate(SELECT_SYSTEM, user, max_tokens=SELECT_MAX_TOKENS, temperature=0.0)
    data = _json(raw) or {}
    picks = []
    raw_picks = data.get("picks")
    for p in raw_picks if isinstance(raw_picks, list) else []:
        if not isinstance(p, dict):
            continue
        cid = str(p.get("id") or "").strip()
        c = by_id.get(cid)
        if not c:
            continue
        q = " ".join(str(p.get("quote") or "").split())
        verbatim = bool(q) and q.lower() in " ".join((c.get("text") or "").split()).lower()
        picks.append({"id": cid, "quote": q if verbatim else "", "verbatim": verbatim})
        if len(picks) >= MAX_PICKS:
            break
    headline = " ".join(str(data.get("headline") or "").split())[:400]
    answerable = data.get("answerable") is True and bool(picks)
    return {"answerable": answerable, "picks": picks, "headline": headline, "raw_ok": bool(data)}


HEADLINE_SYSTEM = """You write one or two plain sentences that answer an employee's question using ONLY the
quoted records given. State facts from the quotes; no speculation, no advice, no mention of
records or quotes. Name the company or entity the facts are about. Return the sentences only."""


def headline(question: str, claims: list[dict]) -> str:
    """A grounded headline over claims the keyword gate already admitted."""
    if not claims:
        return ""
    lines = [f"- [{c.get('source')}] {c.get('title')}: {c.get('quote')}" for c in claims[:6]]
    raw = llm.generate(HEADLINE_SYSTEM, f"Question: {question}\n\nRecords:\n" + "\n".join(lines),
                       max_tokens=200, temperature=0.0)
    text = " ".join((raw or "").split())
    if not text or text.lower().startswith(("(empty", "(generation", "(no usable", "(all ",
                                             "error", "429", "400", "500")):
        return ""
    return text[:400]


VERIFY_SYSTEM = """You check evidence. You get a question and numbered quotes taken verbatim from company
records. For each quote decide: does this quote, by itself, STATE the answer to the question?
- "States" means the specific thing asked for is present in the quote: the amount, percentage,
  name, date, clause, policy value, or the direct description the question asks about.
- A quote about a related matter (a different remedy, a different product, a different person,
  background context) does NOT state the answer.
- For a broad question (what does the company do, what are our values), a quote that gives the
  substance of the answer counts.
- For a question that asks for a judgement or a decision (can we, should we, is it safe to, does
  it make sense to), nothing will literally say yes or no. Then a quote counts when it states a
  fact the decision turns on: a term that was agreed, which party was which, what a policy or
  playbook requires, what someone with authority said about reusing or changing the thing.
  Background about the same subject that the decision does not turn on still does not count.
Output exactly one line and nothing else, no reasoning, no preamble:
{"answers": [<numbers of the quotes that state the answer>]}"""


def verify(question: str, picks: list[dict], by_id: dict) -> list[dict]:
    """Keep only the picks whose quote states the answer. Empty when the model
    rejects all of them or returns nothing usable (fail closed)."""
    if not picks:
        return []
    lines = []
    for i, p in enumerate(picks, 1):
        c = by_id[p["id"]]
        q = p["quote"] or c["snippet"]
        lines.append(f"[{i}] ({c.get('title') or c['id']}) {q}")
    raw = llm.generate(VERIFY_SYSTEM, f"Question: {question}\n\nQuotes:\n" + "\n".join(lines),
                       max_tokens=300, temperature=0.0)
    data = _json(raw) or {}
    nums = data.get("answers")
    if not isinstance(nums, list):
        # A model that explained instead of answering: take the quote numbers
        # it affirmed in prose ("[1] ... counts"), and nothing if it named none.
        text = raw or ""
        if text.startswith("(") or "answers" in text.lower() and "[]" in text:
            return []
        nums = [int(n) for n in re.findall(r"\[(\d+)\]", text)]
        if not nums:
            return []
    keep = set()
    for n in nums:
        try:
            keep.add(int(n))
        except (TypeError, ValueError):
            continue
    return [p for i, p in enumerate(picks, 1) if i in keep]


def candidates_from_rows(rows: list[dict], terms: list[str], date_of) -> list[dict]:
    seen, out = set(), []
    for r in rows:
        rid = r.get("id")
        if not rid or rid in seen:
            continue
        seen.add(rid)
        out.append({"id": rid, "label": r.get("label") or "", "title": r.get("title") or "",
                    "source": r.get("source") or (r.get("label") or "").lower(),
                    "date": date_of(r.get("at")), "visibility": r.get("visibility") or "",
                    "text": r.get("text") or "", "snippet": _snippet(r.get("text") or "", terms),
                    "score": float(r.get("score") or 0)})
        if len(out) >= CANDIDATES:
            break
    return out


def info() -> dict:
    return {"provider": llm.provider(), "model": llm.model_name() if llm.available() else None}


def timed(fn, *a, **kw):
    t0 = time.perf_counter()
    out = fn(*a, **kw)
    return out, int((time.perf_counter() - t0) * 1000)
