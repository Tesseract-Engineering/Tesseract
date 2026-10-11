"""
resolve.py - entity resolution. The actual hard problem.

A person is a Slack handle, an email address, a Jira display name and a git
author, and nothing in the data says they are the same human. Unresolved,
"Priya Sharma" and "@priya" are two nodes describing one person.

The strategy is a ladder, strongest evidence first:

  1. same email                        -> merge, confidence 1.00
  2. same normalised name              -> merge, confidence 0.95
  3. github login == normalised name   -> merge, confidence 0.90
     ("priyasharma" == "Priya Sharma" with spaces and case removed)
  4. email local-part == github login  -> merge, confidence 0.85
  5. close string similarity           -> DO NOT MERGE. Queue for a human.

Rule 5 is the important one. Fuzzy matching is exactly where automated
resolution quietly corrupts a graph: "Chris Lee" and "Chris Lees" are one
character apart and are two people. Anything below the threshold becomes a
ReviewItem, which is a row in the graph a person can accept or reject - not a
silent merge nobody can audit later.

A model could adjudicate rule 5 given context (same repos? overlapping dates?).
That is a good use of one. It is not implemented here because the rules-based
ladder should be measured first - you cannot tell whether a model helps if you
never established the baseline.
"""

from __future__ import annotations

import difflib
import re
import unicodedata

REVIEW_THRESHOLD = 0.88     # below the auto-merge rules, above "ignore"

# Sources that identify a person by a HANDLE rather than a name: rules 3 and 4
# compare a handle against a normalised name, and they only make sense for these.
HANDLE_SOURCES = {"github", "slack", "jira", "zendesk"}


def normalise(name: str) -> str:
    """'Kenneth Reitz' -> 'kennethreitz'. Accents folded, punctuation dropped."""
    folded = unicodedata.normalize("NFKD", name or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", folded.lower())


class Union:
    """Union-find. Accounts get merged into groups; each group becomes a Person."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}
        self.why: list[dict] = []

    def add(self, key: str) -> None:
        self.parent.setdefault(key, key)

    def find(self, key: str) -> str:
        self.add(key)
        while self.parent[key] != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def union(self, a: str, b: str, rule: str, confidence: float) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        self.parent[rb] = ra
        self.why.append({"a": a, "b": b, "rule": rule, "confidence": confidence})


def resolve_people(accounts: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """accounts: [{'source','handle','name','email'}] -> (people, links, review_items)"""
    unique: dict[str, dict] = {}
    for account in accounts:
        handle = (account.get("handle") or account.get("email") or account.get("name") or "").lower()
        if not handle:
            continue
        account_id = f"account:{account['source']}:{handle}"
        entry = unique.setdefault(account_id, {
            "id": account_id, "source": account["source"], "handle": handle,
            "name": account.get("name") or handle, "email": (account.get("email") or "").lower(),
            "norm": normalise(account.get("name") or handle),
        })
        if not entry["email"] and account.get("email"):
            entry["email"] = account["email"].lower()

    groups = Union()
    for account_id in unique:
        groups.add(account_id)

    by_email: dict[str, str] = {}
    by_norm: dict[str, str] = {}
    for account_id, account in unique.items():
        if account["email"]:
            if account["email"] in by_email:
                groups.union(by_email[account["email"]], account_id, "same-email", 1.00)
            else:
                by_email[account["email"]] = account_id
        if account["norm"]:
            if account["norm"] in by_norm:
                groups.union(by_norm[account["norm"]], account_id, "same-normalised-name", 0.95)
            else:
                by_norm[account["norm"]] = account_id

    # handle vs a full name with the spaces taken out
    for account_id, account in unique.items():
        if account["source"] not in HANDLE_SOURCES:
            continue
        twin = by_norm.get(normalise(account["handle"]))
        if twin and twin != account_id:
            groups.union(twin, account_id, "login-matches-name", 0.90)

    # email local-part vs handle: kennethreitz@gmail.com <-> kennethreitz
    logins = {normalise(a["handle"]): aid for aid, a in unique.items()
              if a["source"] in HANDLE_SOURCES}
    for account_id, account in unique.items():
        local = normalise(account["email"].split("@")[0]) if account["email"] else ""
        if local and local in logins and logins[local] != account_id:
            groups.union(logins[local], account_id, "email-matches-login", 0.85)

    # Everything left that merely LOOKS similar becomes a question, not a merge.
    review: list[dict] = []
    roots = sorted({groups.find(a) for a in unique})
    labels = {root: unique[root]["norm"] for root in roots}
    for i, a in enumerate(roots):
        for b in roots[i + 1:]:
            if not labels[a] or not labels[b]:
                continue
            score = difflib.SequenceMatcher(None, labels[a], labels[b]).ratio()
            if REVIEW_THRESHOLD <= score < 1.0:
                review.append({
                    "id": f"review:{a}|{b}", "kind": "identity",
                    "a": a, "b": b, "aName": unique[a]["name"], "bName": unique[b]["name"],
                    "score": round(score, 4), "status": "pending",
                    "reason": f"names {round(score * 100)}% similar - too close to merge automatically",
                })

    people, links = [], []
    for root in roots:
        members = [aid for aid in unique if groups.find(aid) == root]
        # Prefer the longest human-looking name as the display name.
        display = max((unique[m]["name"] for m in members), key=len)
        person_id = f"person:{unique[root]['norm'] or unique[root]['handle']}"
        # Bots dominate recent commit history (dependabot alone is 64 of the
        # last 400 here) and drown out humans in any "who worked on this"
        # query. Flagged rather than dropped - their activity is still real.
        # GitHub marks bots `name[bot]`; Slack integrations are conventionally
        # `something-bot`. Only the first was recognised, so a Datadog alert
        # integration was counted as an employee.
        is_bot = any("[bot]" in unique[m]["handle"] or unique[m]["name"].endswith("[bot]")
                     or unique[m]["handle"].endswith("-bot")
                     for m in members)
        people.append({
            "id": person_id, "name": display, "accountCount": len(members),
            "sources": sorted({unique[m]["source"] for m in members}),
            "isBot": is_bot,
        })
        for member in members:
            links.append({"personId": person_id, "account": unique[member]})

    return people, links, review
