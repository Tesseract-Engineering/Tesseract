"""
bundle.py - what an adapter hands the builder. The seam between
"how THIS company stores things" and "how a graph gets built".

An adapter (tesseract/adapters/<name>.py) reads one company's files and returns a
Bundle. Everything after that - identity resolution, extraction, provenance,
the door - lives in build.py and never learns which company it is.
A new company layout means a new adapter and nothing else; if it needs a change
anywhere else, that is a sign the change is really a bug in the generic part.

WHAT AN ADAPTER MUST NOT DO is decide anything. It reads, renames fields into
the shape below, and records where each thing came from. Merging people,
resolving conflicts and inferring mentions happen downstream, where they are
logged and reviewable. An adapter that "cleans" data hides the evidence.

THE RECORD FIELDS THE BUILDER UNDERSTANDS (SourceRecord.meta), all optional.
An adapter fills what its source has and leaves the rest absent - never
guessed:

  issue records (Jira tickets, support tickets, pull requests)
    key                 human key, 'NEXP-318' / 'PR-101'
    assigneeName        -> ASSIGNED_TO Person
    assigneeSlack       chat handle of the assignee, an identity hint
    declaredService     the ticket's own statement of its subject -> ABOUT,
                        and the ground truth --score measures extraction by
    declaredServiceField  the source field it came from, for provenance
                        (default 'service')
    commenters          [{source, handle, name, email, count, as}] -> COMMENTED
                        'as' is the role: comment / internal-note / review
    customer            customer name -> Customer + FOR_CUSTOMER
    customerTier, customerArr, merchantId   customer attributes
  any record
    messageCounts       {handle: n} for multi-author records (threads)
    path                file the record came from, relative to the root -
                        used for the coverage report
  refs
    linkedIssues        ['NEXP-12', ...] -> REFERENCES to ticket:<key>

Person dicts (record.people, commenters, Bundle.accounts) are raw identities:
{source, handle, name, email}. Unresolved, possibly contradictory. That is
fine; resolution is where contradiction is handled.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .connectors import SourceRecord

KNOWN_KINDS = {"issue", "document", "commit"}
KNOWN_META = {"key", "assigneeName", "assigneeSlack", "declaredService",
              "declaredServiceField", "commenters", "customer", "customerTier",
              "customerArr", "merchantId", "messageCounts", "path"}


@dataclass
class Bundle:
    """Everything one company's exports say, in one shape.

    services     name -> {id, name, props, source, producer}. The vocabulary the
                 mention extractor matches against. Empty is allowed: a company
                 without a service catalog simply gets no service MENTIONS.
    teams        [{id, name, props, source, producer}]
    ownership    [{team (id), service (name), basis, source, source_id,
                   producer, quote}]. Several claims about one service are
                 kept side by side, each labelled with its basis - never
                 reconciled here.
    people       roster rows: {name, empId?, email?, slack?, status?, ...any of
                 load.PERSON_EXTRAS}. Enrich Person nodes; never create
                 identity on their own (accounts do that).
    accounts     raw identities the roster knows about.
    memberships  [{empId | name, team (id or None), teamLabel, via, producer,
                   rank}]. Lower rank wins: a person placed by rank 0 is not
                 placed again by rank 1. team=None means "listed, but the team
                 matches nothing" and is reported, not dropped silently.
    records      every ticket, thread and document.
    speaker_sources  sources whose text is "Name: text" discussion lines.
    files_read   relative paths of every file the adapter consumed, for the
                 coverage report. Records with meta['path'] are added
                 automatically; list structural files (rosters, org charts,
                 catalogs) here yourself.
    """
    name: str
    root: Path
    services: dict[str, dict] = field(default_factory=dict)
    teams: list[dict] = field(default_factory=list)
    ownership: list[dict] = field(default_factory=list)
    people: list[dict] = field(default_factory=list)
    accounts: list[dict] = field(default_factory=list)
    memberships: list[dict] = field(default_factory=list)
    records: list[SourceRecord] = field(default_factory=list)
    speaker_sources: set[str] = field(default_factory=set)
    files_read: set[str] = field(default_factory=set)

    def problems(self) -> list[str]:
        """Shape errors an adapter author should hear about before a build."""
        out = []
        ids = Counter(r.id for r in self.records)
        dupes = [i for i, n in ids.items() if n > 1]
        if dupes:
            out.append(f"{len(dupes)} record ids used more than once "
                       f"(e.g. {', '.join(dupes[:3])}) - later ones would overwrite earlier")
        kinds = Counter(r.kind for r in self.records if r.kind not in KNOWN_KINDS)
        if kinds:
            out.append(f"unknown record kinds: {dict(kinds)}")
        team_ids = {t["id"] for t in self.teams}
        for claim in self.ownership:
            if claim["team"] not in team_ids:
                out.append(f"ownership claim by unknown team {claim['team']!r}")
            if claim["service"] not in self.services:
                out.append(f"ownership claim on unknown service {claim['service']!r}")
        # Meta keys outside KNOWN_META are allowed - load.py keeps the ones it
        # has a property for - so they are not reported as problems.
        return out

    def coverage(self) -> tuple[int, list[tuple[str, int]]]:
        """(files read, [(folder, files not read)]) over everything under root."""
        read = set(self.files_read) | {r.meta["path"] for r in self.records
                                       if r.meta.get("path")}
        unread: Counter = Counter()
        total = 0
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            total += 1
            rel = path.relative_to(self.root).as_posix()
            if rel not in read:
                parts = rel.split("/")
                unread["/".join(parts[:2]) if len(parts) > 2 else parts[0]] += 1
        return len(read), sorted(unread.items(), key=lambda kv: -kv[1])
