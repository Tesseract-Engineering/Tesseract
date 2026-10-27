"""
links.py - links between sources, found in text.

Any source's text can point at the others: a Slack thread names acme/api#412,
a Google Doc links a PR, a PR links a Doc. The patterns live here so every
normalizer reads them the same way.

  Drive URLs          gdrive:file:{id}, always: the id is in the URL
  GitHub URLs and     github:repo:{id}:pr|issue|commit:..., when the repo was
  owner/repo#N        granted to the GitHub pipeline and, for a number, it is
                      known to be an issue or a PR
  Jira keys           jira:{site}:issue:{id}, when the Jira pipeline has seen
                      the key (an old key from before a move counts); else
                      kept in meta.jira_keys
  Slack permalinks    kept in meta: a permalink's message may sit inside a
                      burst whose id starts at another message

Outside GitHub itself, the GitHub side comes from what the GitHub pipeline
left in the bucket: repo names from state/github/repos.json (published each
run) and issue and PR numbers from the item ids in state/github/seen.json.
Other pipelines only read those two keys. A GitHub link that does not resolve
is kept in meta.github_links, so a later run can resolve it. Jira keys
resolve the same way through state/jira/keys.json, which the Jira pipeline
publishes each run.

Which pipeline ran first decides what each could resolve, so links found
later are never lost: an item written with unresolved GitHub links or Jira
keys is kept in state/{source}/unresolved.json, and every later run of that
source resolves it again against the current state (relink), writing a new
record only when a link appears.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from .. import ids
from ..records import ItemRecord, Ref, content_hash
from ..stages.write import state_key
from ..store import ObjectStore

REPO = r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+"
GITHUB_URL = re.compile(rf"https?://github\.com/({REPO})/(pull|issues|commit)/([0-9a-f]{{7,40}}|\d+)\b")
GITHUB_KEY = re.compile(rf"(?<![\w/&#-])({REPO})#(\d+)\b")
DRIVE = re.compile(r"https?://(?:docs\.google\.com/(?:document|presentation|spreadsheets)/d/|"
                   r"drive\.google\.com/(?:file/d/|open\?id=|drive/folders/))([A-Za-z0-9_-]{10,})")
JIRA = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")
JIRA_FALSE = {"SHA", "UTF", "ISO", "RFC", "CVE", "ADR", "SEV", "AES", "TLS", "HTTP", "COVID"}
SLACK_PERMALINK = re.compile(r"https://[A-Za-z0-9-]+\.slack\.com/archives/[A-Z0-9]+/p\d{16}(?:\?[^\s)>\]|]*)?")

_CODE = re.compile(r"```.*?(?:```|\Z)|`[^`\n]*`", re.S)
_QUOTE = re.compile(r"^\s*(?:>|&gt;).*$", re.M)
_GITHUB_ITEM = re.compile(r"github:repo:(\d+):(pr|issue):(\d+)")


def scrub(text: str) -> str:
    """Blank out code and quoted replies, keeping offsets: "fixes #99" inside
    a code sample links nothing."""
    def blank(m: re.Match[str]) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))
    return _QUOTE.sub(blank, _CODE.sub(blank, text))


def jira_keys(text: str) -> list[str]:
    return sorted({k for k in JIRA.findall(text) if k.split("-")[0] not in JIRA_FALSE})


@dataclass
class GitHubLinks:
    """What the GitHub pipeline has seen: repo names and issue/PR numbers."""
    repos: dict[str, int] = field(default_factory=dict)              # lowercased owner/name -> repo id
    kinds: dict[tuple[int, int], str] = field(default_factory=dict)  # (repo id, number) -> issue | pull_request

    @classmethod
    def from_store(cls, store: ObjectStore) -> GitHubLinks:
        repos = json.loads(store.get(state_key("github", "repos")) or b"{}")
        seen = json.loads(store.get(state_key("github", "seen")) or b"{}")
        kinds: dict[tuple[int, int], str] = {}
        for item_id in seen.get("items", {}):
            if m := _GITHUB_ITEM.fullmatch(item_id):
                kinds[(int(m.group(1)), int(m.group(3)))] = "pull_request" if m.group(2) == "pr" else "issue"
        return cls(repos={name.lower(): int(rid) for name, rid in repos.items()}, kinds=kinds)

    def target(self, full_name: str, what: str, value: str) -> str | None:
        """The GitHub ID for owner/name plus pull|issues|commit|number, or None."""
        rid = self.repos.get(full_name.lower())
        if rid is None:
            return None
        if what == "commit":
            return ids.github_commit(rid, value)
        kind = self.kinds.get((rid, int(value)))
        if kind is None:
            return None
        return ids.github_pr(rid, int(value)) if kind == "pull_request" else ids.github_issue(rid, int(value))


@dataclass
class JiraLinks:
    """Jira keys the Jira pipeline has seen, old keys included. In the Jira
    pipeline itself, `lookup` asks the API about a key not seen yet."""
    site: str | None = None                                          # the Jira site's cloud id
    keys: dict[str, str] = field(default_factory=dict)               # KEY-12 -> issue id
    lookup: Callable[[str], str | None] | None = None
    missing: set[str] = field(default_factory=set)

    @classmethod
    def from_store(cls, store: ObjectStore) -> JiraLinks:
        data = json.loads(store.get(state_key("jira", "keys")) or b"{}")
        return cls(site=data.get("site"), keys=dict(data.get("keys", {})))

    def target(self, key: str, *, ask: bool = True) -> str | None:
        """The issue's ID, from the keys seen; `ask` lets lookup try a key not seen."""
        if not self.site:
            return None
        issue_id = self.keys.get(key)
        if issue_id is None and ask and self.lookup is not None and key not in self.missing:
            issue_id = self.lookup(key)
            if issue_id is None:
                self.missing.add(key)
            else:
                self.keys[key] = issue_id
        return ids.jira_issue(self.site, issue_id) if issue_id else None

    def resolve(self, keys: list[str], found: list[Ref], *, own_id: str | None = None,
                ask: bool = True) -> list[str]:
        """Adds a references ref for each key that resolves; returns the rest."""
        rest = []
        for key in keys:
            target = self.target(key, ask=ask)
            if target is None:
                rest.append(key)
            elif target != own_id and all(r.target != target for r in found):
                found.append(Ref(type="references", target=target, via="key"))
        return rest


@dataclass
class Links:
    refs: list[Ref] = field(default_factory=list)
    github_links: list[str] = field(default_factory=list)   # GitHub links not resolved (yet)
    jira_keys: list[str] = field(default_factory=list)
    slack_links: list[str] = field(default_factory=list)

    def add(self, target: str, via: str) -> None:
        if all(r.target != target for r in self.refs):
            self.refs.append(Ref(type="references", target=target, via=via))

    def meta(self) -> dict[str, list[str]]:
        """The unresolved parts, for an item's meta; empty lists left out."""
        out = {"github_links": self.github_links, "jira_keys": self.jira_keys, "slack_links": self.slack_links}
        return {k: v for k, v in out.items() if v}


def scan(text: str, github: GitHubLinks, *, own_id: str | None = None, jira: JiraLinks | None = None) -> Links:
    """Cross-source links in text that is not GitHub's own (GitHub items use
    swarm/sources/github/refs.py, which also knows bare #N and closing
    keywords)."""
    text = scrub(text or "")
    found = Links()
    unresolved: set[str] = set()
    for m in GITHUB_URL.finditer(text):
        target = github.target(m.group(1), "commit" if m.group(2) == "commit" else "number", m.group(3))
        if target:
            found.add(target, "url")
        else:
            unresolved.add(m.group(0))
    for m in GITHUB_KEY.finditer(text):
        target = github.target(m.group(1), "number", m.group(2))
        if target:
            found.add(target, "key")
        else:
            unresolved.add(m.group(0))
    for m in DRIVE.finditer(text):
        target = ids.gdrive_file(m.group(1))
        if target != own_id:
            found.add(target, "url")
    found.github_links = sorted(unresolved)
    found.jira_keys = jira_keys(text)
    if jira is not None:
        found.jira_keys = jira.resolve(found.jira_keys, found.refs, own_id=own_id)
    found.slack_links = sorted(set(SLACK_PERMALINK.findall(text)))
    return found


def relink(item: ItemRecord, *, github: GitHubLinks | None = None, jira: JiraLinks | None = None,
           run_id: str, fetched_at: str) -> ItemRecord | None:
    """`item` with the links in meta.github_links and meta.jira_keys that now
    resolve made into refs, or None when nothing new resolves. Text, version
    and everything else stay as they were."""
    keys = list(item.meta.get("jira_keys") or [])
    links = list(item.meta.get("github_links") or [])
    refs = list(item.refs)
    # Only what the Jira pipeline has seen: a key that never resolves is not asked about on every run.
    keys_left = jira.resolve(keys, refs, own_id=item.id, ask=False) if jira is not None else keys
    links_left = []
    for link in links:
        target, via = None, "url"
        if github is not None:
            if m := GITHUB_URL.fullmatch(link):
                target = github.target(m.group(1), "commit" if m.group(2) == "commit" else "number", m.group(3))
            elif m := GITHUB_KEY.fullmatch(link):
                target, via = github.target(m.group(1), "number", m.group(2)), "key"
        if target is None:
            links_left.append(link)
        elif target != item.id and all(r.target != target for r in refs):
            refs.append(Ref(type="references", target=target, via=via))
    if keys_left == keys and links_left == links:
        return None
    meta = {k: v for k, v in item.meta.items() if k not in ("jira_keys", "github_links")}
    if keys_left:
        meta["jira_keys"] = keys_left
    if links_left:
        meta["github_links"] = links_left
    data = item.model_dump()
    data.update(refs=refs, meta=meta, run_id=run_id, fetched_at=fetched_at,
                content_hash=content_hash(title=item.title, text=item.text, container=item.container,
                                          people=item.people, refs=refs, meta=meta))
    return ItemRecord.model_validate(data)

