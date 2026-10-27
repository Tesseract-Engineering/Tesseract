"""
refs.py - deterministic links found in GitHub text.

Plain code, no model: these are exact syntaxes with exact meanings.

  "Fixes #12", "closes acme/api#3", "resolved <issue url>"
        from a PR: closes      from a commit: implements      from an issue: references
  "#12", "acme/api#3", github.com/.../pull/12 | issues/12 | commit/<sha>
        references
  docs.google.com/document/d/<id>, drive.google.com/file/d/<id>
        references gdrive:file:<id>
  @login                                      a person with role `mentioned`
  Jira keys (AUTH-12), Slack permalinks       kept in meta; their IDs need the
                                              Jira and Slack pipelines

GitHub numbers issues and PRs in one sequence, so "#12" needs a lookup to
know which it is. Some repos use "#12" for an outside tracker instead (Django
means Trac tickets), where it collides with real PR numbers; for repos marked
external, a bare "#12" is never resolved. It is kept in meta.external_refs,
and URLs and owner/repo#12 still link. Targets in repos outside the grant are dropped, because
scope equals what the customer granted. Fenced and inline code and quoted
replies are ignored: "fixes #99" inside a code sample closes nothing.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from ... import ids
from ...records import Ref
from ..links import DRIVE as _DRIVE
from ..links import GITHUB_URL as _URL
from ..links import REPO as _REPO
from ..links import SLACK_PERMALINK as _SLACK
from ..links import jira_keys, scrub

_ISSUE_URL = rf"https?://github\.com/({_REPO})/(?:issues|pull)/(\d+)"
_CLOSING = re.compile(
    rf"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?\s+(?:(?:({_REPO}))?#(\d+)\b|{_ISSUE_URL})", re.I)
_SHORT = re.compile(rf"(?<![\w/&#-])(?:({_REPO}))?#(\d+)\b")
_MENTION = re.compile(r"(?<![\w@/.`])@([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))(?![\w/-])")


@dataclass
class RepoIndex:
    """What is in scope, so refs can be turned into IDs.

    full_names  lowercased "owner/name" -> repo id, for every granted repo
    kinds       (repo id, number) -> "issue" | "pull_request", as seen so far
    lookup      asks the API for a number not seen yet; None if absent
    external    repo ids whose bare "#n" means an outside tracker
    """
    full_names: dict[str, int] = field(default_factory=dict)
    kinds: dict[tuple[int, int], str] = field(default_factory=dict)
    lookup: Callable[[int, int], str | None] | None = None
    missing: set[tuple[int, int]] = field(default_factory=set)
    external: set[int] = field(default_factory=set)

    def kind(self, repo_id: int, number: int) -> str | None:
        key = (repo_id, number)
        if key not in self.kinds and key not in self.missing and self.lookup is not None:
            found = self.lookup(repo_id, number)
            if found:
                self.kinds[key] = found
            else:
                self.missing.add(key)       # asked once per run, not once per mention
        return self.kinds.get(key)

    def repo_id(self, full_name: str | None, default: int) -> int | None:
        return default if full_name is None else self.full_names.get(full_name.lower())


@dataclass
class Found:
    refs: list[Ref] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    jira_keys: list[str] = field(default_factory=list)
    slack_links: list[str] = field(default_factory=list)
    external: list[str] = field(default_factory=list)    # bare "#n" in an outside-tracker repo
    dropped: int = 0                     # out of scope or unresolvable


def _target(repo_id: int, number: int, kind: str) -> str:
    return ids.github_pr(repo_id, number) if kind == "pull_request" else ids.github_issue(repo_id, number)


def extract(text: str, *, item_kind: str, repo_id: int, own_number: int | None,
            index: RepoIndex) -> Found:
    found = Found()
    text = scrub(text or "")
    seen: dict[str, str] = {}            # target -> relation; closes/implements outrank references

    def add(relation: str, target: str, via: str) -> None:
        if target in seen and seen[target] != "references":
            return
        if target in seen:
            found.refs = [r for r in found.refs if r.target != target]
        seen[target] = relation
        found.refs.append(Ref(type=relation, target=target, via=via))

    def resolve(full_name: str | None, number: int) -> tuple[int, str] | None:
        rid = index.repo_id(full_name, repo_id)
        if rid is None:
            return None
        if rid == repo_id and number == own_number:
            return None
        kind = index.kind(rid, number)
        return (rid, kind) if kind else None

    external = repo_id in index.external
    external_refs: set[int] = set()
    closing_spans = []
    for m in _CLOSING.finditer(text):
        closing_spans.append(m.span())
        full_name, number = (m.group(1), m.group(2)) if m.group(2) else (m.group(3), m.group(4))
        if external and m.group(2) and full_name is None:
            external_refs.add(int(number))
            continue
        hit = resolve(full_name, int(number))
        if hit is None:
            found.dropped += 1
            continue
        rid, kind = hit
        target = _target(rid, int(number), kind)
        if kind == "issue" and item_kind == "pull_request":
            add("closes", target, "closing_keyword")
        elif kind == "issue" and item_kind == "commit":
            add("implements", target, "closing_keyword")
        else:
            add("references", target, "closing_keyword")

    def inside_closing(pos: int) -> bool:
        return any(a <= pos < b for a, b in closing_spans)

    for m in _URL.finditer(text):
        if inside_closing(m.start()):
            continue
        rid = index.repo_id(m.group(1), repo_id)
        if rid is None:
            found.dropped += 1
            continue
        if m.group(2) == "commit":
            add("references", ids.github_commit(rid, m.group(3)), "url")
            continue
        number = int(m.group(3))
        hit = resolve(m.group(1), number)
        if hit:
            add("references", _target(hit[0], number, hit[1]), "url")
        elif not (rid == repo_id and number == own_number):
            found.dropped += 1

    for m in _SHORT.finditer(text):
        if inside_closing(m.start()):
            continue
        if external and m.group(1) is None:
            external_refs.add(int(m.group(2)))
            continue
        hit = resolve(m.group(1), int(m.group(2)))
        if hit:
            add("references", _target(hit[0], int(m.group(2)), hit[1]), "key")
        elif not (m.group(1) is None and int(m.group(2)) == own_number):
            found.dropped += 1

    for m in _DRIVE.finditer(text):
        add("references", ids.gdrive_file(m.group(1)), "url")

    found.jira_keys = jira_keys(text)
    found.slack_links = sorted(set(_SLACK.findall(text)))
    found.external = [f"#{n}" for n in sorted(external_refs)]
    found.mentions = list(dict.fromkeys(_MENTION.findall(text)))
    return found
