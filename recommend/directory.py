"""
directory.py - one key per person across sources, and the name to show for them.

A person is their identity cluster when the swarm's identity run made one (graph/swarm_records.py,
rule 6), else their email, else their source account. So Dhruv asking in Slack and filing in Jira
counts once, and "handled by" shows a name, not an id.
"""

from __future__ import annotations

import re

# Which source's display name to show, best first: Slack and Jira profiles are what people chose; a Drive
# name is often the email's local part, and a commit name can be anything (the sandbox plants wrong ones).
NAME_SOURCES = {"slack": 0, "jira": 1, "gdrive": 2, "github": 3}


def _name_rank(name: str, source: str | None) -> tuple:
    human = " " in name.strip() and not re.search(r"[@.]\w", name)
    return (not human, NAME_SOURCES.get(source or "", 9), -len(name))


def norm_name(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


class Directory:
    def __init__(self, items: dict[str, dict], person_of: dict[str, str] | None = None,
                 clusters: dict[str, dict] | None = None) -> None:
        self.person_of = dict(person_of or {})   # a copy: unclustered accounts are added below
        self.names: dict[str, str] = {}          # key -> display name
        self._rank: dict[str, tuple] = {}
        self.emails: dict[str, str] = {}         # key -> email
        self.by_email: dict[str, str] = {}
        self.by_name: dict[str, str] = {}        # normalised name -> key, when one person has it
        self.by_handle: dict[str, str] = {}      # "github:kar722" -> key
        self.ids: dict[str, set[str]] = {}       # key -> every source account id
        ambiguous: set[str] = set()
        for pid, rec in (clusters or {}).items():
            for m in rec.get("members") or []:
                self._see(m.get("id") or "", m.get("email"), m.get("name"), m.get("source"), m.get("handle"), pid)
        for it in items.values():
            for p in it.get("people") or []:
                self._see(p["id"], p.get("email"), p.get("name"), p.get("source"), p.get("handle"))
        for key, name in self.names.items():
            n = norm_name(name)
            if n in self.by_name and self.by_name[n] != key:
                ambiguous.add(n)
            self.by_name.setdefault(n, key)
        for n in ambiguous:                       # two people with one name: neither is matched by name
            del self.by_name[n]

    def _see(self, pid, email, name, source, handle, cluster=None) -> None:
        email = (email or "").lower() or None
        key = cluster or self.person_of.get(pid) or (email and self.by_email.get(email)) or \
            (f"email:{email}" if email else pid)
        if email:
            self.by_email.setdefault(email, key)
            self.emails.setdefault(key, email)
        if name and (key not in self.names or _name_rank(name, source) < self._rank[key]):
            self.names[key], self._rank[key] = name, _name_rank(name, source)
        if source and handle:
            self.by_handle[f"{source}:{handle.lower()}"] = key
        if pid:
            self.ids.setdefault(key, set()).add(pid)
            self.person_of.setdefault(pid, key)

    def key(self, pid: str) -> str:
        return self.person_of.get(pid, pid)

    def name(self, key: str) -> str:
        return self.names.get(key) or self.emails.get(key) or key

    def find(self, text: str) -> str | None:
        """A person named as text: an email, an @handle (GitHub), or a full name."""
        text = text.strip().strip("`*_")
        if "@" in text and not text.startswith("@"):
            return self.by_email.get(text.lower())
        if text.startswith("@"):
            return self.by_handle.get(f"github:{text[1:].lower()}")
        if hit := self.by_name.get(norm_name(text)):
            return hit
        first = [k for n, k in self.by_name.items() if n.split()[0] == norm_name(text)] if " " not in text.strip() else []
        return first[0] if len(set(first)) == 1 else None      # "Hamilton", when one person is called that
