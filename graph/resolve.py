"""
resolve.py - the graph's fallback people matching.

The swarm owns identity: its identity run clusters source identities into people,
by email and, with the model on, by judgement. This file only handles identities no
cluster lists (people named in extracted relations, mostly), and it is deliberately
timid, because a person's access is the union of their accounts' groups
(swarm_queries.viewer). A wrong merge does not just misattribute work; it hands one
person the other's private items.

  1. same email                        -> merge, confidence 1.00
  2. same normalised name              -> review, 0.95   "John Smith" in Slack and in Jira
  3. handle == normalised name         -> review, 0.90   "kennethreitz" / "Kenneth Reitz"
  4. email local-part == handle        -> review, 0.85
  5. close string similarity           -> review, by score

Only rule 1 merges. Everything else becomes a ReviewItem: a row in the graph a person
can accept or reject, never a silent merge nobody can audit later.
"""

from __future__ import annotations

import difflib
import re
import unicodedata

REVIEW_THRESHOLD = 0.88     # string similarity worth a person's look

# Sources that identify a person by a HANDLE rather than a name: rules 3 and 4
# compare a handle against a normalised name, and only make sense for these.
HANDLE_SOURCES = {"github", "slack", "jira"}


def normalise(name: str) -> str:
    """'Kenneth Reitz' -> 'kennethreitz'. Accents folded, punctuation dropped."""
    folded = unicodedata.normalize("NFKD", name or "")
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", folded.lower())


def looks_like_bot(handle: str, name: str) -> bool:
    """GitHub marks bots `name[bot]`; Slack integrations are conventionally `something-bot`.
    Bots are flagged rather than dropped - their activity is real - so that "who worked on
    this" can leave them out."""
    handle, name = (handle or "").lower(), (name or "").lower()
    return "[bot]" in handle or name.endswith("[bot]") or handle.endswith("-bot")


class Union:
    """Union-find. Accounts get merged into groups; each group becomes a Person."""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def add(self, key: str) -> None:
        self.parent.setdefault(key, key)

    def find(self, key: str) -> str:
        self.add(key)
        while self.parent[key] != key:
            self.parent[key] = self.parent[self.parent[key]]
            key = self.parent[key]
        return key

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


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
    by_email: dict[str, str] = {}
    for account_id, account in unique.items():
        groups.add(account_id)
        if account["email"]:
            if account["email"] in by_email:
                groups.union(by_email[account["email"]], account_id)      # rule 1
            else:
                by_email[account["email"]] = account_id

    # Rules 2-5 only ever suggest: one suggestion per pair of people, strongest rule first.
    roots = sorted({groups.find(a) for a in unique})
    members = {root: [aid for aid in unique if groups.find(aid) == root] for root in roots}
    suggestions: dict[tuple[str, str], tuple[str, float]] = {}

    def suggest(a: str, b: str, rule: str, confidence: float) -> None:
        ra, rb = sorted((groups.find(a), groups.find(b)))
        if ra != rb and (ra, rb) not in suggestions:
            suggestions[(ra, rb)] = (rule, confidence)

    by_norm: dict[str, list[str]] = {}
    for account_id, account in unique.items():
        if account["norm"]:
            by_norm.setdefault(account["norm"], []).append(account_id)
    for same in by_norm.values():
        for other in same[1:]:
            suggest(same[0], other, "same-normalised-name", 0.95)
    for account_id, account in unique.items():
        if account["source"] in HANDLE_SOURCES:
            for twin in by_norm.get(normalise(account["handle"]), []):
                suggest(twin, account_id, "handle-matches-name", 0.90)
    logins = {normalise(a["handle"]): aid for aid, a in unique.items() if a["source"] in HANDLE_SOURCES}
    for account_id, account in unique.items():
        local = normalise(account["email"].split("@")[0]) if account["email"] else ""
        if local in logins:
            suggest(logins[local], account_id, "email-matches-handle", 0.85)
    for i, a in enumerate(roots):
        for b in roots[i + 1:]:
            na, nb = unique[a]["norm"], unique[b]["norm"]
            if na and nb:
                score = difflib.SequenceMatcher(None, na, nb).ratio()
                if REVIEW_THRESHOLD <= score < 1.0:
                    suggest(a, b, f"names {round(score * 100)}% similar", round(score, 4))

    review = [{"id": f"review:{a}|{b}", "kind": "identity", "a": a, "b": b,
               "aName": unique[a]["name"], "bName": unique[b]["name"], "rule": rule,
               "score": score, "status": "pending",
               "reason": f"{rule}: too weak to merge on its own"}
              for (a, b), (rule, score) in sorted(suggestions.items())]

    people, links = [], []
    for root in roots:
        group = members[root]
        # Prefer the longest human-looking name as the display name.
        display = max((unique[m]["name"] for m in group), key=len)
        person_id = f"person:{unique[root]['email'] or unique[root]['source'] + ':' + unique[root]['handle']}"
        people.append({
            "id": person_id, "name": display, "accountCount": len(group),
            "sources": sorted({unique[m]["source"] for m in group}),
            "isBot": any(looks_like_bot(unique[m]["handle"], unique[m]["name"]) for m in group),
        })
        for member in group:
            links.append({"personId": person_id, "account": unique[member]})

    return people, links, review
