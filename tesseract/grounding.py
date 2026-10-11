"""
grounding.py - work out WHAT a question is about before anything is searched.

Names are resolved deterministically, against the nodes that exist in this
company's namespace, never guessed:

  full name      "David Kim"            -> that person, no doubt
  @handle        "@priya"               -> the account's owner
  one name part  "Priya", "Nakamura"    -> resolved IF exactly one person has it
                 "Marcus", "Chen"       -> AMBIGUOUS: ask, do not guess

An ambiguous name is reported as ambiguous, with every candidate, rather than
silently picking one. Services, teams, documents and ticket keys resolve the
same way (ground_org, ground_docs, ground_tickets).

Bots are excluded from name-part matching. "Datadog" is the first word of the
Datadog alert integration's display name, and "who handles datadog?" is about
the service, not the bot.
"""

from __future__ import annotations

import re

from . import graph

PEOPLE = """
MATCH (p:Person {ns: $ns})
OPTIONAL MATCH (p)-[:MEMBER_OF]->(t:Team)
OPTIONAL MATCH (p)-[:HAS_ACCOUNT]->(a:Account {source: 'slack'})
RETURN p.id AS id, p.name AS name, p.status AS status, p.title AS title,
       coalesce(p.isBot, false) AS isBot, p.departedAt AS departedAt,
       collect(DISTINCT t.name) AS teams, collect(DISTINCT a.handle) AS handles
"""

# Words a question uses that happen to be spelled like a name part. Kept short
# on purpose: most English words are not anybody's first name or surname, and
# the check below is exact-token, so "parking" never matches "Park".
NOT_NAMES = {"who", "what", "when", "where", "which", "how", "the", "and", "for",
             "team", "work", "working", "owns", "own", "is", "are", "on", "in"}

WORD = re.compile(r"@?[A-Za-z][A-Za-z'.-]*")


def ground(question: str) -> dict:
    """Resolve the people a question names.

    Returns {"people": [...], "ambiguous": {surface: [candidates]}, "notes": [...]}.
    """
    people = graph.run(PEOPLE)
    if not people:
        return {"people": [], "ambiguous": {}, "notes": []}

    text = question
    resolved: dict[str, dict] = {}
    notes: list[str] = []
    consumed: list[tuple[int, int]] = []

    # Full names first, longest first, so "Marcus Chen" claims both words and
    # neither is later read as an ambiguous first name or surname.
    for person in sorted(people, key=lambda p: -len(p["name"] or "")):
        name = person["name"]
        if not name or " " not in name:
            continue
        for match in re.finditer(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE):
            resolved[person["id"]] = {**person, "how": "full name"}
            consumed.append(match.span())

    def taken(span: tuple[int, int]) -> bool:
        return any(a <= span[0] and span[1] <= b for a, b in consumed)

    by_handle = {h.lower(): p for p in people for h in (p.get("handles") or []) if h}
    parts: dict[str, list[dict]] = {}
    for person in people:
        if person.get("isBot"):
            continue
        for part in (person["name"] or "").split():
            if len(part) >= 3:
                parts.setdefault(part.lower(), []).append(person)

    ambiguous: dict[str, list[dict]] = {}
    for match in WORD.finditer(text):
        if taken(match.span()):
            continue
        token = match.group(0).rstrip(".'")
        if token.startswith("@"):
            owner = by_handle.get(token[1:].lower())
            if owner:
                resolved[owner["id"]] = {**owner, "how": token}
                notes.append(f"Read {token} as {owner['name']}.")
            continue
        key = token.lower()
        if key in NOT_NAMES:
            continue
        candidates = parts.get(key)
        if not candidates:
            continue
        distinct = {c["id"]: c for c in candidates}
        if len(distinct) == 1:
            person = next(iter(distinct.values()))
            if person["id"] not in resolved:
                resolved[person["id"]] = {**person, "how": f"'{token}'"}
                notes.append(f"Read '{token}' as {person['name']}.")
        else:
            ambiguous[token] = sorted(distinct.values(), key=lambda p: p["name"])

    # A name part that belongs to someone the question already named in full
    # is not a second person: "David Kim and Kim's team" is one Kim.
    for token in list(ambiguous):
        if any(token.lower() in (p["name"] or "").lower().split() for p in resolved.values()):
            del ambiguous[token]

    return {"people": list(resolved.values()), "ambiguous": ambiguous, "notes": notes}


ORG = """
MATCH (n {ns: $ns}) WHERE n:Service OR n:Team
RETURN labels(n)[0] AS label, n.id AS id, n.name AS name, n.kind AS kind
"""


def ground_org(question: str) -> list[dict]:
    """Services and teams named in a question, however loosely.

    People say "the billing service", "billing", "BillingService" and
    "billing-service" for one thing, and "the platform team" for a team whose
    name is "Platform". Each resolves to the exact stored name, so the model
    never has to guess the spelling - which is most of why questions had to be
    phrased a particular way to work.

    A single-word team name ("Data", "Sales") resolves only next to the word
    "team" or when capitalised, so "data pipeline" is not read as the Data
    team. A bare service stem ("billing") is passed on as a POSSIBLE match
    rather than a certain one; the query can include it, and the rows decide.
    """
    rows = graph.run(ORG)
    found: dict[str, dict] = {}
    lowered = question.lower()

    for row in rows:
        name = row["name"] or ""
        if row["label"] == "Team":
            pattern = rf"(?<!\w){re.escape(name)}(?!\w)"
            for match in re.finditer(pattern, question, re.IGNORECASE):
                near_team = re.search(rf"{re.escape(name.lower())}\s+team|team\s+{re.escape(name.lower())}",
                                      lowered)
                if " " in name or near_team or match.group(0)[0].isupper():
                    found[row["id"]] = {**row, "certain": True}
            continue

        parts = name.split("-")
        strong = {name, " ".join(parts), "_".join(parts), "".join(p.capitalize() for p in parts)}
        weak = set()
        for suffix in ("service", "engine", "server", "platform"):
            if name.endswith("-" + suffix):
                stem = name[: -(len(suffix) + 1)]
                strong.add(f"{stem} {suffix}")
                weak.add(stem)
        if any(re.search(rf"(?<![\w-]){re.escape(a)}(?![\w-])", question, re.IGNORECASE)
               for a in strong):
            found[row["id"]] = {**row, "certain": True}
        elif any(re.search(rf"(?<![\w-]){re.escape(a)}(?![\w-])", question, re.IGNORECASE)
                 for a in weak if len(a) >= 3):
            found.setdefault(row["id"], {**row, "certain": False})
    return list(found.values())


DOCS = "MATCH (d:Document {ns: $ns}) RETURN d.id AS id, d.path AS path, d.title AS title"


def ground_docs(question: str) -> list[dict]:
    """Documents a question names by file name or by title.

    "who authored employee-handbook.md?" and "who wrote the employee
    handbook?" are the same question. The stored title is "employee handbook"
    and the path is "people-ops/policies/employee-handbook.md", and a model
    matching `title = 'employee-handbook.md'` gets zero rows from either.
    """
    rows = graph.run(DOCS)
    lowered = question.lower()
    found = []
    for row in rows:
        path = (row["path"] or "").lower()
        base = path.rsplit("/", 1)[-1]
        stem = base.rsplit(".", 1)[0]
        title = (row["title"] or "").lower()
        forms = [f for f in {base, stem if "-" in stem or "_" in stem else ""} if f]
        if len(title.split()) >= 2:
            forms.append(title)
        if any(re.search(rf"(?<![\w-]){re.escape(f)}(?![\w-])", lowered) for f in forms):
            found.append(row)
    if found:
        # A README appears once per service; naming "README.md" alone matches
        # all of them, which is not a resolution - leave that to the query.
        return found if len(found) <= 3 else []

    # Nobody types a title exactly. "the kafka consumer lag runbook" is the
    # file runbooks/kafka-consumer-lag-recovery.md: three of its four title
    # words, plus its folder. Without this the query fell back to "any path
    # containing kafka", pulled in an incident report and an ADR as well, and
    # the answer credited the runbook to the incident's commander.
    words = set(re.findall(r"[a-z0-9]+", lowered)) - STOPWORDS
    scored = []
    for row in rows:
        path = (row["path"] or "").lower()
        vocab = set(re.findall(r"[a-z0-9]+", (row["title"] or "").lower())) - STOPWORDS
        folders = {w.rstrip("s") for w in re.findall(r"[a-z]+", path.rsplit("/", 1)[0])}
        if len(vocab) < 2:
            continue
        hit = len(vocab & words)
        # Folder names carry meaning people use: "the kafka ADR" is the one
        # file under architecture/adr/ with kafka in its title. Counting the
        # folder only as a bonus missed it - one title word plus "adr" was not
        # enough - and the question fell through to the kafka SERVICE, where
        # the model concluded David Kim had not written it. He had.
        folder_hit = any(w.rstrip("s") in folders for w in words)
        score = hit / len(vocab) + (0.5 if folder_hit else 0)
        if (hit >= 2 and hit / len(vocab) >= 0.6) or (hit >= 1 and folder_hit):
            scored.append((score, row))
    scored.sort(key=lambda s: -s[0])
    # Only a CLEAR winner is a resolution; two near-ties are left to the query.
    if scored and (len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.2):
        return [{**scored[0][1], "partial": True}]
    return []


# "doc"/"docs" and "engineering" are folder names too, but nearly every file
# sits under one of them - as evidence for a particular file they are noise.
STOPWORDS = {"the", "a", "an", "of", "for", "and", "to", "in", "on", "who", "what", "is",
             "doc", "docs", "document", "documents", "wrote", "write", "written", "owns",
             "own", "about", "did", "does", "do", "engineering", "file", "md"}


TICKET_KEY = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d{1,6})\b")


def ground_tickets(question: str) -> list[dict]:
    """Ticket keys like NEXP-318. Stored as id 'ticket:NEXP-318', which a model
    matching on the bare key misses."""
    keys = {m.group(1).upper() for m in TICKET_KEY.finditer(question.upper())}
    # Pull requests are stored as pr:101; people write "PR-101" or "PR #101".
    prs = {m.group(1) for m in re.finditer(r"\bPR[\s#-]*(\d+)\b", question, re.IGNORECASE)}
    if not keys and not prs:
        return []
    ids = [f"ticket:{k}" for k in keys if not k.startswith("PR-")] + [f"pr:{n}" for n in prs]
    return graph.run("MATCH (i:Issue {ns: $ns}) WHERE i.id IN $ids RETURN i.id AS id, i.title AS title",
                     ids=ids)
