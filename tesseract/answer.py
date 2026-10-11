"""answer.py - one question in, a cited verdict out.

    from tesseract.answer import answer
    answer("Who owns the billing service?", viewer="employee")

The path, in order:

  1. ground     resolve the people, services, teams, documents, tickets and
                customers the question names, against this company's graph
  2. search     full-text search over what the viewer may read, scored by rank,
                coverage of what the question asks, and links to resolved entities
  3. gate       INFO only when an entity resolved and a linked hit clears
                THRESHOLD; otherwise NOT_FOUND, with what was searched and the
                nearest misses labelled as misses
  4. model      (when a key is configured) expand the search terms, select
                evidence from readable candidates, and verify that each quote
                states the answer; it may add evidence, never remove the gate's
  5. hands      owners and draft actions for the approval door (hands.py)

Every answer is for a viewer. Records outside the viewer's ACL groups are
counted and named (restricted_matches), never quoted. A model never touches
the graph: it reads candidate text and returns ids and quotes.
"""

from __future__ import annotations

import math
import re
import time

from . import access, grounding, hands, llm, search
from .access import READABLE
from .config import settings
from .graph import date as _date
from .graph import run as _run

QUOTE_MAX = 200


def quote(text: str | None, *anchors: str) -> str:
    """The line of `text` holding the first anchor found, cut to QUOTE_MAX
    characters around it. Deterministic: same text, same quote."""
    text = text or ""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    for anchor in anchors:
        for ln in lines:
            pos = ln.find(anchor)
            if pos < 0:
                continue
            if len(ln) <= QUOTE_MAX:
                return ln
            start = max(0, min(pos - 60, len(ln) - QUOTE_MAX))
            if start:                      # never open mid-word
                back = ln.rfind(" ", max(0, start - 20), start + 1)
                start = back + 1 if back >= 0 else (0 if start <= 20 else start)
            end = min(len(ln), start + QUOTE_MAX - 2)
            if end < len(ln):              # nor close mid-word
                fwd = ln.rfind(" ", start + QUOTE_MAX // 2, end)
                end = fwd if fwd > 0 else end
            cut = ln[start:end].strip()
            return ("…" if start else "") + cut + ("…" if end < len(ln) else "")
    first = lines[0] if lines else ""
    return first[:QUOTE_MAX - 1] + ("…" if len(first) > QUOTE_MAX - 1 else "")


def _first(rows: list[dict]) -> dict:
    return rows[0] if rows else {}


# --------------------------------------------------------------------------- #
# scoring
# --------------------------------------------------------------------------- #
#
# Mapping a question onto the nearest-looking records and calling that an answer
# is the failure mode. So: resolve the entities the question names first, search
# only what the viewer may read, score each hit, and answer INFO only when an
# entity resolved AND the best hit clears THRESHOLD.
#
# final = W_RANK * rank + W_COVER * cover + LINK_BONUS (linked) - DUP_PENALTY (dup)
#   rank   fulltext score / the top readable hit's score
#   cover  share of the question's ASK terms the hit contains, weighted by
#          rarity (idf). Ask terms are what the question wants to know: the
#          terms left after the named entities are taken out. A record about a
#          customer that never says "renewal" does not answer "when is their renewal".
#
# A restricted record counts as a match when it is linked to a resolved entity
# (if any resolved), contains half the question's terms, covers
# RESTRICTED_MIN_COVER of the ask and ranks within half the top score. Matching
# is a weaker claim than answering, hence the lower bar.

THRESHOLD = 0.8
MIN_COVER = 0.8
RESTRICTED_MIN_COVER = 0.6
W_RANK, W_COVER = 0.25, 0.75
LINK_BONUS = 0.15         # hit MENTIONS / is ABOUT / ASSIGNED_TO / FOR_CUSTOMER a resolved entity
DUP_PENALTY = 0.25        # a title already seen higher up
SEARCH_LIMIT = 40

STOP = set("""a an and are as at be by can could did do does for from has have how i if in
is it its me my of on or our should so that the their them then there these they this to
was we were what when where which who whom why will with would you your next""".split())
# Words that frame a question without saying what it asks for.
FILLER = set("""about say said says get gets got tell know need needs show give find any all much
many there here like mean means happen happened happening use used using thing things
fix fixes fixing resolve handle deal solve work works working go goes going
anyone someone anybody already built build building existing exist exists""".split())

# Coverage is counted inside the database so a restricted record's text never
# leaves it: only how many question terms it contains.
_BLOB = ("toLower(coalesce(node.title, '') + ' ' + coalesce(node.key, '') + ' ' + "
         "coalesce(node.text, node.body, ''))")
COVER = (f"size([t IN $terms WHERE {_BLOB} CONTAINS t]) AS hits, "
         f"[t IN $ask WHERE {_BLOB} CONTAINS t] AS asked")
_ROW = ("node.id AS id, labels(node)[0] AS label, coalesce(node.title, node.key, node.id) AS title, "
        "coalesce(node.source, '') AS source, coalesce(node.text, node.body, '') AS text, "
        "node.createdAt AS at, node.visibility AS visibility")

Q_SEARCH = f"""
CALL db.index.fulltext.queryNodes('knowledgeSearch', $term) YIELD node, score
WHERE node.ns = $ns AND {READABLE}
RETURN {_ROW}, score, {COVER}
ORDER BY score DESC LIMIT {SEARCH_LIMIT}
""".strip()

Q_ENTITY_NODES = f"""
MATCH (node) WHERE node.ns = $ns AND node.id IN $ids AND (node:Issue OR node:Document) AND {READABLE}
RETURN {_ROW}, 0.0 AS score, {COVER}
""".strip()

Q_RESTRICTED = f"""
CALL db.index.fulltext.queryNodes('knowledgeSearch', $term) YIELD node, score
WHERE node.ns = $ns AND NOT {READABLE}
RETURN node.id AS id, coalesce(node.source, '') AS source, node.visibility AS visibility,
       coalesce(node.aclGroups, []) AS acl, score, {COVER}
ORDER BY score DESC LIMIT {SEARCH_LIMIT}
""".strip()

Q_SOURCES = f"""
MATCH (node) WHERE node.ns = $ns AND (node:Document OR node:Issue OR node:Commit) AND {READABLE}
RETURN coalesce(node.source, toLower(labels(node)[0])) AS source, count(*) AS documents
ORDER BY documents DESC
""".strip()

Q_LINKED = """
MATCH (n {ns: $ns}) WHERE n.id IN $hits
MATCH (n)-[:MENTIONS|ABOUT|ASSIGNED_TO|FOR_CUSTOMER|REFERENCES]-(e {ns: $ns})
WHERE e.id IN $entities
RETURN DISTINCT n.id AS id
""".strip()

Q_DF = """
UNWIND $terms AS t
CALL (t) {
  CALL db.index.fulltext.queryNodes('knowledgeSearch', '"' + t + '"') YIELD node
  WHERE node.ns = $ns
  RETURN count(node) AS df
}
RETURN t, df
""".strip()

Q_TITLE = f"""
MATCH (node:Document) WHERE node.ns = $ns AND {READABLE}
  AND any(t IN $terms WHERE toLower(coalesce(node.title, '')) CONTAINS t
                          OR toLower(replace(node.id, '-', ' ')) CONTAINS t)
RETURN {_ROW}, 1.0 AS score
LIMIT 12
""".strip()

Q_PROFILE = f"""
MATCH (node:Document) WHERE node.ns = $ns AND node.id STARTS WITH $prefix AND {READABLE}
RETURN {_ROW}, 1.0 AS score
LIMIT 12
""".strip()

Q_CUSTOMERS = "MATCH (c:Customer {ns: $ns}) RETURN c.id AS id, c.name AS name"

Q_ENDPOINT = f"""
MATCH (node) WHERE node.ns = $ns AND (node:Document OR node:Service OR node:Issue) AND {READABLE}
  AND (node.text CONTAINS $p OR node.body CONTAINS $p OR node.description CONTAINS $p)
RETURN count(node) AS n
""".strip()

ENDPOINT = re.compile(r"(?<![\w/])(/[a-z0-9][\w\-./{}]*[\w}])", re.IGNORECASE)
# First words of customer names that are ordinary English and name nothing.
COMMON_FIRST = {"urban", "peak", "luxe", "global", "modern", "direct", "research", "artisanal"}

# A question about the company as a whole: "what does the company do", "who is
# the ceo", "where are we headquartered", "who do I ask about security". These
# always see the company profile documents (Company.profile_prefix).
_COMPANY_TOPIC = (r"\b(do|does|about|sell|make|build|offer|mission|values?|founded|founders?|headquarter\w*|"
                  r"located|based|revenue|arr|funding|raised|investors?|employees|headcount|size|customers?|"
                  r"clients?|competitors?|products?|pricing|price|cost|leadership|executives?|ceo|cto|cfo|"
                  r"coo|vp|org chart|history|offices?|tools?|stack|deploy\w*)\b")
_COMPANY_WIDE_REST = (r"|\b(who|what|where|when|how)\b.{0,20}\b(ceo|cto|cfo|coo|founders?|founded|"
                      r"headquarter\w*|leadership|executive team|org chart|mission|revenue|arr|headcount|"
                      r"pricing|competitors?)\b"
                      r"|\b(who|where|whom)\b.{0,25}\b(ask|contact|talk to|go to|reach|owns?|responsible|"
                      r"in charge)\b")


def _company_wide(question: str) -> bool:
    words = settings().company.name.split()
    subject = "|".join(re.escape(w) for w in ("company", "we", "our", "us", *words[:1]) if w)
    pattern = rf"\b({subject})\b.{{0,30}}{_COMPANY_TOPIC}{_COMPANY_WIDE_REST}"
    return re.search(pattern, question, re.IGNORECASE) is not None


def _terms(question: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9][A-Za-z0-9_\-]{1,}", question)
    return [w for w in dict.fromkeys(w.lower() for w in words) if w not in STOP]


def _stem(t: str) -> str:
    for suffix in ("ing", "ed", "es", "s"):
        if t.endswith(suffix) and len(t) - len(suffix) >= 3:
            return t[: -len(suffix)]
    return t


def _idf(terms: list[str]) -> dict[str, float]:
    if not terms:
        return {}
    total = _first(_run("MATCH (n) WHERE n.ns = $ns AND (n:Document OR n:Issue OR n:Commit OR n:Claim) "
                        "RETURN count(n) AS n")).get("n") or 1
    rows = _run(Q_DF, terms=terms)
    return {r["t"]: math.log(1 + total / (1 + (r["df"] or 0))) for r in rows}


def _customers(question: str) -> list[dict]:
    """Full customer names, or a distinctive first word ('ACME', 'Nordic')."""
    rows = _run(Q_CUSTOMERS)
    found: dict[str, dict] = {}
    for r in rows:
        name = r["name"] or ""
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", question, re.IGNORECASE):
            found[r["id"]] = {"type": "Customer", "id": r["id"], "name": name, "how": "full name"}
    by_first: dict[str, list[dict]] = {}
    for r in rows:
        first = (r["name"] or "").split(" ")[0].lower()
        if len(first) >= 4 and first not in COMMON_FIRST:
            by_first.setdefault(first, []).append(r)
    for word in re.findall(r"[A-Za-z][A-Za-z\-]+", question):
        group = by_first.get(word.lower(), [])
        if 0 < len(group) <= 3:
            for r in group:
                found.setdefault(r["id"], {"type": "Customer", "id": r["id"], "name": r["name"],
                                           "how": f"'{word}'"})
    return list(found.values())


def ground_question(question: str, viewer: dict) -> dict:
    """Entities the question names, resolved against the graph."""
    resolved: list[dict] = []
    people = grounding.ground(question)
    for p in people["people"]:
        resolved.append({"type": "Person", "id": p["id"], "name": p["name"], "how": p.get("how")})
    ambiguous = [f"{tok} ({', '.join(c['name'] for c in cands)})"
                 for tok, cands in people["ambiguous"].items()]
    for o in grounding.ground_org(question):
        resolved.append({"type": o["label"], "id": o["id"], "name": o["name"],
                         "how": "exact" if o["certain"] else "short form"})
    docs = grounding.ground_docs(question)
    vis = access.visibility_of([d["id"] for d in docs])
    for d in docs:   # a title the viewer may not read is not resolved for them
        v, acl = vis.get(d["id"], ("internal", []))
        if access.readable(v, acl, viewer):
            resolved.append({"type": "Document", "id": d["id"], "name": d.get("title") or d["path"],
                             "how": "partial title" if d.get("partial") else "title"})
    for t in grounding.ground_tickets(question):
        resolved.append({"type": "Issue", "id": t["id"], "name": t["id"].split(":", 1)[-1], "how": "key"})
    resolved += _customers(question)
    unresolved_paths = []
    for p in dict.fromkeys(ENDPOINT.findall(question)):
        if (_first(_run(Q_ENDPOINT, p=p, acl=viewer["acl_groups"])).get("n") or 0) > 0:
            resolved.append({"type": "Endpoint", "id": f"endpoint:{p}", "name": p, "how": "path"})
        else:
            unresolved_paths.append(p)

    seen: set[str] = set()
    unique = []
    for r in resolved:
        if r["id"] not in seen:
            seen.add(r["id"])
            unique.append(r)
    covered: set[str] = set()
    for r in unique:
        covered |= set(re.findall(r"[a-z0-9][a-z0-9_\-]+", r["name"].lower()))
        covered |= set(re.findall(r"[a-z0-9]+", r["name"].lower()))
    terms = _terms(question)
    unresolved = [t for t in terms if t not in covered] + unresolved_paths
    return {"resolved": unique, "ambiguous": ambiguous, "unresolved": unresolved, "terms": terms}


def _cover(asked: list[str], ask_idf: dict[str, float]) -> float:
    if not ask_idf:
        return 1.0      # the question names things and asks nothing more of them
    return sum(ask_idf.get(t, 0) for t in asked) / (sum(ask_idf.values()) or 1.0)


def _score(rows: list[dict], ask_idf: dict[str, float], linked: set[str], entity_ids: set[str]) -> list[dict]:
    top = max((float(r["score"] or 0) for r in rows), default=0.0) or 1.0
    seen_titles: set[str] = set()
    out = []
    for r in rows:
        rank = float(r["score"] or 0) / top
        cover = _cover(r.get("asked") or [], ask_idf)
        link = r["id"] in linked or r["id"] in entity_ids
        title = (r.get("title") or "").strip().lower()
        dup = title in seen_titles
        seen_titles.add(title)
        final = W_RANK * rank + W_COVER * cover + (LINK_BONUS if link else 0) - (DUP_PENALTY if dup else 0)
        out.append({**r, "rank": round(rank, 3), "cover": round(cover, 3), "linked": link,
                    "duplicate": dup, "final": round(final, 3)})
    out.sort(key=lambda h: -h["final"])
    return out


# --------------------------------------------------------------------------- #
# usage: querylog.sync_to_graph writes n.askCount / n.lastAskedAt from what
# people asked. It may reorder answers that already passed the gate, never
# admit one: INFO still needs grounding + cover + THRESHOLD on `final`.
# --------------------------------------------------------------------------- #

USAGE_WEIGHT = 0.05
USAGE_CAP = 0.15


def usage_boost(ask_count: int | None) -> float:
    return min(USAGE_CAP, USAGE_WEIGHT * math.log1p(max(0, ask_count or 0)))


def _usage(ids: list[str]) -> dict[str, dict]:
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids:
        return {}
    try:
        rows = _run("MATCH (n {ns: $ns}) WHERE n.id IN $ids AND n.askCount IS NOT NULL "
                    "RETURN n.id AS id, n.askCount AS c, toString(n.lastAskedAt) AS at", ids=ids)
    except Exception:   # usage is decoration; an answer never fails for it
        return {}
    return {r["id"]: {"askCount": int(r["c"] or 0), "lastAskedAt": r["at"] or ""} for r in rows}


def _attach_usage(claims: list[dict]) -> None:
    usage = _usage([c.get("node_id") for c in claims])
    for c in claims:
        c.update(usage.get(c.get("node_id"), {"askCount": 0, "lastAskedAt": ""}))


def _rank_by_usage(answers: list[dict]) -> list[dict]:
    """Re-sort gated answers by final + usage_boost(askCount), marking rows
    whose position changed because of usage. Gate membership is untouched."""
    usage = _usage([r.get("id") for r in answers])
    before = [r.get("id") for r in answers]
    for r in answers:
        u = usage.get(r.get("id"), {})
        r["askCount"], r["lastAskedAt"] = u.get("askCount", 0), u.get("lastAskedAt", "")
        r["usage_boost"] = round(usage_boost(r["askCount"]), 3)
    ranked = sorted(answers, key=lambda r: -(r["final"] + r["usage_boost"]))
    for i, r in enumerate(ranked):
        r["boosted_by_usage"] = before[i] != r.get("id")
    return ranked


# --------------------------------------------------------------------------- #
# the keyword path
# --------------------------------------------------------------------------- #

def _restricted(term: str, terms: list[str], ask_idf: dict[str, float], viewer: dict,
                top_any: float, entity_ids: set[str]) -> list[dict]:
    """Restricted hits that match the question about as well as a real answer
    would. Only ids, sources, ACL groups and which terms matched come back:
    never text or titles."""
    rows = _run(Q_RESTRICTED, term=term, terms=terms, ask=list(ask_idf), acl=viewer["acl_groups"])
    rows = [r for r in rows if _cover(r.get("asked") or [], ask_idf) >= RESTRICTED_MIN_COVER
            and (r.get("hits") or 0) >= len(terms) / 2 and float(r["score"] or 0) >= 0.5 * top_any]
    if entity_ids and rows:
        linked = {x["id"] for x in _run(Q_LINKED, hits=[r["id"] for r in rows], entities=list(entity_ids))}
        rows = [r for r in rows if r["id"] in linked]
    return rows


def _keyword(question: str, cypher: list[str], viewer: dict) -> tuple[dict, list[dict], dict]:
    """The deterministic path. Returns (result, scored rows, context) so the
    model layer builds on the same search instead of redoing it."""
    g = ground_question(question, viewer)
    terms = g["terms"]
    entity_ids = {r["id"] for r in g["resolved"]}
    rows, restricted = [], []
    cypher += [Q_SEARCH, Q_RESTRICTED]
    paths = set(ENDPOINT.findall(question))
    ask = list(dict.fromkeys(_stem(t) for t in g["unresolved"] if t not in FILLER and t not in paths))
    cover_terms = [_stem(t) for t in terms]
    ask_idf: dict[str, float] = {}
    if terms:
        cypher.append(Q_DF)
        ask_idf = _idf(ask)
        lucene = " OR ".join('"' + t.replace('"', "").replace("\\", "") + '"' for t in terms)
        rows = _run(Q_SEARCH, term=lucene, terms=cover_terms, ask=ask, acl=viewer["acl_groups"])
        top_any = max([float(r["score"] or 0) for r in rows] or [0.0])
        # A ticket or document the question names is a candidate even when the
        # fulltext ranking missed it (ticket keys tokenise badly).
        have = {r["id"] for r in rows}
        named = [i for i in entity_ids if i not in have]
        if named:
            cypher.append(Q_ENTITY_NODES)
            rows += [{**r, "score": top_any or 1.0} for r in
                     _run(Q_ENTITY_NODES, ids=named, terms=cover_terms, ask=ask, acl=viewer["acl_groups"])]
        linked: set[str] = set()
        if rows and entity_ids:
            cypher.append(Q_LINKED)
            linked = {r["id"] for r in _run(Q_LINKED, hits=[r["id"] for r in rows], entities=list(entity_ids))}
        rows = _score(rows, ask_idf, linked, entity_ids)
        restricted = _restricted(lucene, cover_terms, ask_idf, viewer, top_any, entity_ids)
    cypher.append(Q_SOURCES)
    sources = _run(Q_SOURCES, acl=viewer["acl_groups"])
    restricted_info = {"restricted_matches": len(restricted),
                       "restricted_note": access.restricted_note(restricted),
                       "restricted_groups": list(dict.fromkeys(gr for r in restricted for gr in r["acl"]))}
    searched = {"sources": sources, "terms": terms,
                "resolved_entities": [{k: r[k] for k in ("type", "name", "id", "how")} for r in g["resolved"]],
                "unresolved_terms": g["unresolved"], "ask_terms": ask, "ambiguous": g["ambiguous"],
                "viewer": viewer["name"], "threshold": THRESHOLD}
    answers = [r for r in rows if r["linked"] and r["cover"] >= MIN_COVER and r["final"] >= THRESHOLD]
    best = rows[0]["final"] if rows else 0.0

    def hit(r: dict) -> dict:
        return {"title": r.get("title") or "", "source": r.get("source") or r.get("label", "").lower(),
                "node_id": r.get("id") or "", "score": r["final"],
                "why": {"rank": r["rank"], "cover": r["cover"], "linked": r["linked"],
                        "duplicate": r["duplicate"]}}

    base = {"owners": [], "approval_required": [], "proposed_actions": [], "viewer": viewer["name"],
            "searched": searched, **restricted_info}
    ctx = {"terms": terms, "cover_terms": cover_terms, "ask": ask, "ask_idf": ask_idf,
           "entity_ids": entity_ids, "resolved": g["resolved"], "sources": sources,
           "restricted_ids": [r["id"] for r in restricted]}
    if not g["resolved"] or not answers:
        reason = ("no entity in the question resolved to anything in the graph" if not g["resolved"]
                  else f"no record linked to {', '.join(r['name'] for r in g['resolved'][:3])} covers "
                       f"what the question asks ({', '.join(ask) or 'nothing'}); best score {best}")
        return ({"verdict": "NOT_FOUND", "headline": "I could not find a grounded answer.",
                 "reason": reason, "claims": [],
                 "nearest": [{**hit(r), "label": "nearest, not an answer"} for r in rows[:3]],
                 **base}, rows, ctx)

    answers = _rank_by_usage(answers)
    claims = []
    for r in answers[:8]:
        claims.append({"line": None, "claim": f"{r.get('label')} matching the question"
                       + (" and linked to a resolved entity." if r["linked"] else "."),
                       "source": r.get("source") or r.get("label", "").lower(),
                       "title": r.get("title") or "", "quote": quote(r.get("text"), *terms),
                       "node_id": r.get("id") or "", "date": _date(r.get("at")),
                       "visibility": r.get("visibility") or "", "score": r["final"],
                       "why": {**hit(r)["why"], "usage_boost": r["usage_boost"]},
                       "askCount": r["askCount"], "lastAskedAt": r["lastAskedAt"],
                       **({"boosted_by_usage": True} if r["boosted_by_usage"] else {})})
    names = ", ".join(r["name"] for r in g["resolved"][:4])
    return ({"verdict": "INFO",
             "headline": f"{len(claims)} grounded record{'s' if len(claims) != 1 else ''} about {names}.",
             "claims": claims, **base}, rows, ctx)


# --------------------------------------------------------------------------- #
# build requests: "help me architect X" becomes "has anyone here built X?"
# --------------------------------------------------------------------------- #

BUILD_REQUEST = re.compile(
    r"\b(help me (architect|design|build|plan|set up|implement|create)|"
    r"(i|we) (want|need|would like|'d like) to (build|work on|create|set up|design|architect|implement)|"
    r"how (should|would|do) (i|we) (build|design|architect|implement)|"
    r"(can|could) you help (me|us) (architect|design|build))\b", re.IGNORECASE)

PRECEDENT_REWRITE = """An engineer asks for help designing or building something. Rewrite the request as ONE
question asking whether anyone at the company has already built that thing. Ask only that, one
clause, no "and how does it work". Name the thing in plain words (for example "Has anyone here
already built a continuous retraining pipeline for ML models?"). Output only the question."""


def _as_precedent(question: str) -> list[str]:
    """The build request as 'has anyone already built this?' questions, tried in
    order until one is grounded: the model's rewrite (when a model is
    configured), a fixed planning frame, and the request as asked. The verifier
    is literal, so different wordings ground different evidence."""
    out = []
    if llm.available():
        text = " ".join((llm.generate(PRECEDENT_REWRITE, question.strip(), max_tokens=80,
                                      temperature=0.0) or "").split())
        if text.endswith("?") and 10 < len(text) < 300 and not text.startswith("("):
            out.append(text)
    out.append(f"I'm planning this: {question.strip()} Has anyone here already built this?")
    out.append(question)
    return out


def _answer_build_request(question: str, cypher: list[str], viewer: dict, asker: str | None) -> dict:
    tried: list[str] = []
    out: dict = {}
    for asked in _as_precedent(question):
        tried.append(asked)
        out = _search(asked, cypher, viewer, asker)
        if out.get("verdict") == "INFO" and out.get("claims"):
            break
    out["design_request"] = {"asked_as": tried[-1], "tried": tried}
    if out.get("verdict") == "INFO" and out.get("claims"):
        head = out.get("headline") or ""
        if re.match(r"\d+ (grounded )?records?\b", head):     # the keyword path's placeholder
            head = "existing work already covers this: " + "; ".join(
                c["title"] for c in out["claims"][:3] if c.get("title")) + "."
        out["headline"] = "Before you design this from scratch: " + head
    elif out.get("verdict") == "NOT_FOUND":
        out["reason"] = ("no existing work at the company matches this build request, so there is "
                         "nothing to reuse; " + (out.get("reason") or ""))
    return out


# --------------------------------------------------------------------------- #
# questions about the system itself
# --------------------------------------------------------------------------- #

META_ACCESS = re.compile(
    r"\b(what|which|how much|how many)\b.{0,40}\b(data|systems?|sources?|records?|documents?|tools?)\b"
    r".{0,40}\b(access|see|connected|have|read|available|know)\b"
    r"|\b(what|which)\b.{0,20}\b(can|do)\b.{0,10}\b(i|you|u)\b.{0,10}\b(see|access|read|know)\b",
    re.IGNORECASE)


def _meta_access(question: str, cypher: list[str], viewer: dict) -> dict | None:
    """'What data do you have access to?' is answered from the viewer's
    readable record counts, not from the company's documents."""
    if not META_ACCESS.search(question):
        return None
    cypher.append(Q_SOURCES)
    sources = _run(Q_SOURCES, acl=viewer["acl_groups"])
    total = sum(int(s.get("documents") or 0) for s in sources)
    parts = [f"{s['source']} ({int(s['documents']):,})" for s in sources]
    groups = viewer.get("acl_groups") or []
    extra = (f" Your access also opens restricted groups: {', '.join(groups)}." if groups
             else " You hold no restricted-group access, so sealed documents appear as locked stubs.")
    headline = (f"Using {viewer['name']}'s access, I can read {total:,} records across "
                f"{len(sources)} connected systems: {', '.join(parts)}.{extra}")
    searched = {"sources": sources, "terms": [], "resolved_entities": [], "unresolved_terms": [],
                "ask_terms": [], "ambiguous": [], "viewer": viewer["name"], "threshold": THRESHOLD}
    return {"verdict": "INFO", "headline": headline, "claims": [], "owners": [],
            "approval_required": [], "proposed_actions": [], "viewer": viewer["name"],
            "searched": searched, "restricted_matches": 0, "restricted_note": "",
            "restricted_groups": [], "path": "meta"}


# --------------------------------------------------------------------------- #
# the model layer
# --------------------------------------------------------------------------- #

def _search(question: str, cypher: list[str], viewer: dict, asker: str | None) -> dict:
    """Keyword path first; then, when a model is configured, the model layer
    expands the search and selects evidence from what the viewer may read. The
    keyword result stands whenever the model is off, fails, or adds nothing."""
    meta = _meta_access(question, cypher, viewer)
    if meta:
        return meta
    out, rows, ctx = _keyword(question, cypher, viewer)
    out["path"] = "search"

    def finish(res: dict) -> dict:
        """The hands: owners and draft actions from the graph. An answer is never lost to them."""
        try:
            return hands.attach(res, question, ctx, asker)
        except Exception as exc:      # noqa: BLE001
            return {**res, "hands_error": f"{type(exc).__name__}: {exc}"[:200]}

    if not llm.available():
        return finish(out)
    try:
        return finish(_with_llm(question, cypher, viewer, out, rows, ctx))
    except Exception as exc:          # noqa: BLE001 - the model is an upgrade, never a dependency
        out["llm"] = {**search.info(), "error": f"{type(exc).__name__}: {exc}"[:200]}
        return finish(out)


def _with_llm(question: str, cypher: list[str], viewer: dict, out: dict,
              rows: list[dict], ctx: dict) -> dict:
    expanded, ms_expand = search.timed(search.expand, question)
    terms = list(dict.fromkeys(expanded.get("terms", []) + expanded.get("entities", []) + ctx["terms"]))
    note = {**search.info(), "expanded_terms": expanded.get("terms", []),
            "entities": expanded.get("entities", []), "intent": expanded.get("intent", ""),
            "ms_expand": ms_expand}
    if not terms:
        out["llm"] = {**note, "used": False, "why": "no search terms"}
        return out

    # Claims the keyword gate admitted stay exactly as they are. The model may
    # ADD evidence the gate's literal terms missed, and words the headline over
    # the union; it never removes a gate claim.
    gate_claims = list(out.get("claims") or []) if out.get("verdict") == "INFO" else []

    def gate_only(why: str) -> dict:
        if not gate_claims:
            out["llm"] = {**note, "used": False, "why": why}
            return out
        text = search.headline(question, gate_claims)
        if text:
            return {**out, "headline": text, "path": "search+llm",
                    "llm": {**note, "used": True, "role": "headline only", "why": why}}
        out["llm"] = {**note, "used": False, "why": "headline call returned nothing"}
        return out

    lucene = search.lucene(terms)
    cypher.append(Q_SEARCH)
    cover_terms = [_stem(t) for t in terms if " " not in t]
    more = _run(Q_SEARCH, term=lucene, terms=cover_terms, ask=ctx["ask"], acl=viewer["acl_groups"])
    # Records whose TITLE or PATH carries a search term rarely rank by body
    # text, so they join the pool at the top score. The model still decides
    # whether they answer anything.
    cypher.append(Q_TITLE)
    title_terms = [t for t in terms if len(t) >= 4][:10]
    titled = _run(Q_TITLE, terms=title_terms, acl=viewer["acl_groups"]) if title_terms else []
    if _company_wide(question):
        cypher.append(Q_PROFILE)
        have = {r["id"] for r in titled}
        titled += [r for r in _run(Q_PROFILE, prefix=settings().company.profile_prefix,
                                   acl=viewer["acl_groups"]) if r["id"] not in have]
    top_more = max([float(r.get("score") or 0) for r in more] or [1.0])
    seen = {r["id"] for r in titled}
    merged = [{**r, "score": top_more + 0.01} for r in titled] + \
             [{**r, "score": float(r.get("score") or 0)} for r in more if r["id"] not in seen]
    seen |= {r["id"] for r in more}
    merged += [{**r, "score": float(r.get("score") or 0)} for r in rows if r["id"] not in seen]
    merged.sort(key=lambda r: -r["score"])
    cands = search.candidates_from_rows(merged, terms, _date)
    sel, ms_select = search.timed(search.select, question, cands, terms)
    note.update({"candidates": len(cands), "ms_select": ms_select, "answerable": sel["answerable"]})
    out.setdefault("searched", {})["llm_terms"] = terms

    if not sel["answerable"]:
        return gate_only("model found no candidate that answers the question")

    by_id = {c["id"]: c for c in cands}
    # A second opinion on narrower evidence: the verifier sees only the
    # question and the quotes, never the selector's reasoning, and must say
    # which quote states the answer. Fails closed to the keyword result.
    verified, ms_verify = search.timed(search.verify, question, sel["picks"], by_id)
    note.update({"ms_verify": ms_verify, "picked": len(sel["picks"]), "verified": len(verified)})
    if not verified:
        return gate_only("verifier rejected every selected quote")
    claims = []
    for p in verified:
        c = by_id[p["id"]]
        q = p["quote"] or quote(c["text"], *terms) or c["snippet"][:QUOTE_MAX]
        claims.append({"line": None, "claim": f"{c['label']} selected as evidence for the question.",
                       "source": c["source"], "title": c["title"], "quote": q,
                       "node_id": c["id"], "date": c["date"], "visibility": c["visibility"],
                       "score": round(c["score"], 3),
                       "why": {"selected_by": "llm", "verbatim_quote": p["verbatim"]}})
    _attach_usage(claims)
    claimed = {c.get("node_id") for c in gate_claims}
    added = [c for c in claims if c["node_id"] not in claimed]
    claims = gate_claims + added
    note["gate_claims"], note["added_by_llm"] = len(gate_claims), len(added)

    headline = sel["headline"]
    if gate_claims or note.get("verified", 0) < note.get("picked", 0):
        # The selector wrote its headline over every pick; a headline leaning
        # on evidence the verifier dropped would cite nothing, so it is
        # rewritten over the claims that survived.
        headline, ms_headline = search.timed(search.headline, question, claims)
        note["ms_headline"] = ms_headline
    headline = headline or f"{len(claims)} record{'s' if len(claims) != 1 else ''} answer this."
    result = {**out, "verdict": "INFO", "headline": headline, "claims": claims, "reason": None,
              "nearest": [], "path": "search+llm", "llm": {**note, "used": True}}
    if not ctx["entity_ids"]:
        # A restricted match is meaningful only when the question named
        # something the sealed record is linked to; on a vague question it is
        # a keyword coincidence, so it is not reported. Restricted matches
        # always come from the keyword path's own terms, never expanded ones.
        result.update({"restricted_matches": 0, "restricted_note": "", "restricted_groups": []})
    return result


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def _entity_ids(out: dict) -> list[str]:
    """Graph ids of the entities an answer is about (not its evidence). The
    query log links questions to these as well as to the cited claims."""
    ids = [r["id"] for r in out.get("searched", {}).get("resolved_entities", []) if r.get("id")]
    claimed = {c.get("node_id") for c in out.get("claims", [])}
    return [i for i in dict.fromkeys(ids) if i not in claimed]


def answer(question: str, viewer: dict | str | None = None, asker: str | None = None,
           retries: int = 0, wait: float = 30.0) -> dict:
    """The answer to `question` for `viewer` (a preset name, {"name", "acl_groups"},
    or None for the company's default viewer). `asker` is the person asking, by
    name; they are never proposed as their own contact. `retries` re-runs while
    the graph is empty (e.g. during a rebuild), sleeping `wait` seconds."""
    t0 = time.perf_counter()
    who = access.viewer_of(viewer)
    out: dict = {}
    cypher: list[str] = []
    for attempt in range(retries + 1):
        cypher = []
        if BUILD_REQUEST.search(question):
            out = _answer_build_request(question, cypher, who, asker)
        else:
            out = _search(question, cypher, who, asker)
        searched_any = any(s.get("documents") for s in out.get("searched", {}).get("sources", []))
        if out["claims"] or searched_any or attempt == retries:
            break
        time.sleep(wait)
    out["entities"] = _entity_ids(out)
    out["question"] = question
    out.setdefault("path", "search")
    out["cypher"] = cypher
    out["ms"] = int((time.perf_counter() - t0) * 1000)
    return out


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) < 2:
        sys.exit('usage: python -m tesseract.answer "your question"')
    print(json.dumps(answer(" ".join(sys.argv[1:])), indent=2, default=str))
