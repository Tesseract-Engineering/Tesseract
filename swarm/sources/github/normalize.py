"""
normalize.py - GitHub payloads to item records.

Five kinds, one record each:

  repo           description and topics
  issue          body, with every comment folded in
  pull_request   body, then reviews, review comments (with their file), then
                 comments: the whole discussion in one text, as the design asks
  commit         the message
  document       a doc file on the default branch (connector.py says which),
                 versioned by its blob sha and dated by its last commit. Git
                 keeps no creation date short of walking the file's history,
                 so created_at is that last commit's date too. No people:
                 whoever last touched a file did not necessarily write it.

Payloads arrive already redacted (swarm/run.py redacts before anything is
kept), so the redactions list is read back from the markers left in the text.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from ... import ids
from ...interfaces import RawItem
from ...records import Acl, ItemRecord, Person, content_hash
from ...stages.redact import redactions_in
from ..links import JiraLinks, relink
from .connector import repo_visibility
from .refs import RepoIndex, extract

MAX_TEXT = 200_000
MAX_FILES = 100


def _login(user: dict | None) -> str | None:
    return (user or {}).get("login")


def _is_bot(user: dict | None, login: str | None) -> bool:
    return (user or {}).get("type") == "Bot" or bool(login and login.endswith("[bot]"))


class _People:
    """Roles per login, in first-seen order. One Person per login."""

    def __init__(self) -> None:
        self._roles: dict[str, list[str]] = {}
        self._extra: dict[str, dict[str, str | None]] = {}
        self.bots: set[str] = set()

    def add(self, handle: str | None, role: str, *, name: str | None = None, email: str | None = None,
            user: dict | None = None) -> None:
        """Bots (github-actions, dependabot) are not people: nobody should be
        sent to them as an owner or expert. They are kept in meta.bots."""
        if not handle:
            return
        if _is_bot(user, handle):
            self.bots.add(handle)
            return
        roles = self._roles.setdefault(handle, [])
        if role not in roles:
            roles.append(role)
        extra = self._extra.setdefault(handle, {"name": None, "email": None})
        extra["name"] = extra["name"] or name
        extra["email"] = extra["email"] or (email.lower() if email else None)

    def build(self) -> list[Person]:
        return [Person(id=ids.person("github", h), source="github", handle=h, roles=roles, **self._extra[h])
                for h, roles in self._roles.items()]


def _fold(blocks: list[tuple[str, str | None]]) -> str:
    parts = []
    for header, body in blocks:
        body = (body or "").strip()
        if header and body:
            parts.append(f"--- {header} ---\n{body}")
        elif header:
            parts.append(f"--- {header} ---")
        elif body:
            parts.append(body)
    return "\n\n".join(parts)


class GitHubNormalizer:
    def __init__(self, index: RepoIndex, jira: JiraLinks | None = None):
        self.index = index
        self.jira = jira or JiraLinks()
        self.run_id = ""

    def relink(self, item: ItemRecord, **when: str) -> ItemRecord | None:
        """GitHub links its own repos as it fetches; only Jira keys can be left."""
        return relink(item, jira=self.jira, **when)

    def to_item(self, raw: RawItem, acl: Acl) -> ItemRecord:
        payload = raw.payload
        builder = {"repo": self._repo, "issue": self._issue, "pull_request": self._pull,
                   "commit": self._commit, "file": self._file}[payload["type"]]
        fields = builder(payload)
        return self._finish(fields, acl, raw.fetched_at)

    # -- one builder per kind ------------------------------------------------ #

    def _repo(self, p: dict[str, Any]) -> dict[str, Any]:
        repo = p["repo"]
        topics = repo.get("topics") or []
        text = "\n\n".join(x for x in [repo.get("description") or "", " ".join(f"#{t}" for t in topics)] if x)
        return dict(
            id=ids.github_repo(repo["id"]), kind="repo", container=None, aliases=[repo["full_name"]],
            title=repo["full_name"], text=text, url=repo["html_url"],
            created_at=repo["created_at"], updated_at=repo["updated_at"],
            version=repo.get("pushed_at") or repo["updated_at"], people=_People(),
            meta={"default_branch": repo.get("default_branch"), "language": repo.get("language"),
                  "visibility": repo_visibility(repo),
                  "archived": bool(repo.get("archived")), "topics": topics},
            repo_id=repo["id"], number=None, scan="")

    def _issue(self, p: dict[str, Any]) -> dict[str, Any]:
        repo, issue = p["repo"], p["issue"]
        people = _People()
        people.add(_login(issue.get("user")), "author", user=issue.get("user"))
        for a in issue.get("assignees") or []:
            people.add(_login(a), "assignee", user=a)
        blocks = [("", issue.get("body"))]
        for c in p.get("comments", []):
            people.add(_login(c.get("user")), "commenter", user=c.get("user"))
            blocks.append((f"comment: {_login(c.get('user')) or 'ghost'}", c.get("body")))
        text = _fold(blocks)
        return dict(
            id=ids.github_issue(repo["id"], issue["number"]), kind="issue", container=ids.github_repo(repo["id"]),
            aliases=[f"{repo['full_name']}#{issue['number']}"], title=issue["title"], text=text,
            url=issue["html_url"], created_at=issue["created_at"], updated_at=issue["updated_at"],
            version=issue["updated_at"], people=people,
            meta={"number": issue["number"], "state": issue["state"],
                  "state_reason": issue.get("state_reason"),
                  "labels": sorted(lb["name"] for lb in issue.get("labels") or []),
                  "milestone": (issue.get("milestone") or {}).get("title"), "closed_at": issue.get("closed_at")},
            repo_id=repo["id"], number=issue["number"], scan=f"{issue['title']}\n\n{text}")

    def _pull(self, p: dict[str, Any]) -> dict[str, Any]:
        repo, issue, pull = p["repo"], p["issue"], p["pull"]
        people = _People()
        people.add(_login(pull.get("user")), "author", user=pull.get("user"))
        for a in pull.get("assignees") or []:
            people.add(_login(a), "assignee", user=a)
        for r in pull.get("requested_reviewers") or []:
            people.add(_login(r), "reviewer", user=r)
        blocks = [("", pull.get("body"))]
        for r in p.get("reviews", []):
            who = _login(r.get("user")) or "ghost"
            people.add(who, "reviewer", user=r.get("user"))
            blocks.append((f"review: {who} ({r.get('state', 'COMMENTED')})", r.get("body")))
        for c in p.get("review_comments", []):
            who = _login(c.get("user")) or "ghost"
            people.add(who, "commenter", user=c.get("user"))
            blocks.append((f"review comment: {who} on {c.get('path', '?')}", c.get("body")))
        for c in p.get("comments", []):
            who = _login(c.get("user")) or "ghost"
            people.add(who, "commenter", user=c.get("user"))
            blocks.append((f"comment: {who}", c.get("body")))
        text = _fold(blocks)
        return dict(
            id=ids.github_pr(repo["id"], pull["number"]), kind="pull_request", container=ids.github_repo(repo["id"]),
            aliases=[f"{repo['full_name']}#{pull['number']}"], title=pull["title"], text=text,
            url=pull["html_url"], created_at=pull["created_at"], updated_at=pull["updated_at"],
            version=pull["updated_at"], people=people,
            meta={"number": pull["number"], "state": "merged" if pull.get("merged_at") else pull["state"],
                  "draft": bool(pull.get("draft")), "merged_at": pull.get("merged_at"),
                  "base": (pull.get("base") or {}).get("ref"), "head": (pull.get("head") or {}).get("ref"),
                  "additions": pull.get("additions"), "deletions": pull.get("deletions"),
                  "changed_files": pull.get("changed_files"),
                  "labels": sorted(lb["name"] for lb in issue.get("labels") or [])},
            repo_id=repo["id"], number=pull["number"], scan=f"{pull['title']}\n\n{text}")

    def _commit(self, p: dict[str, Any]) -> dict[str, Any]:
        repo, c = p["repo"], p["commit"]
        git = c["commit"]
        author = git.get("author") or {}
        login = _login(c.get("author"))
        handle = login or (author.get("email") or "unknown").lower()
        people = _People()
        people.add(handle, "author", name=author.get("name"), email=author.get("email"), user=c.get("author"))
        message = git.get("message") or ""
        title = message.splitlines()[0] if message else c["sha"][:7]
        files = [f["filename"] for f in c.get("files") or []]
        return dict(
            id=ids.github_commit(repo["id"], c["sha"]), kind="commit", container=ids.github_repo(repo["id"]),
            aliases=[c["sha"][:7]], title=title, text=message, url=c["html_url"],
            created_at=author.get("date") or git["committer"]["date"],
            updated_at=(git.get("committer") or {}).get("date") or author.get("date"),
            version=c["sha"], people=people,
            meta={"sha": c["sha"], "files": files[:MAX_FILES], "files_truncated": len(files) > MAX_FILES,
                  "parents": len(c.get("parents") or []), "branch": repo.get("default_branch")},
            repo_id=repo["id"], number=None, scan=message)

    def _file(self, p: dict[str, Any]) -> dict[str, Any]:
        repo, path = p["repo"], p["path"]
        last = p.get("last_commit") or {}
        git = last.get("commit") or {}
        date = (git.get("committer") or {}).get("date") or (git.get("author") or {}).get("date")
        if not date:
            raise ValueError("a doc with no commit to date it")
        branch = repo.get("default_branch") or "main"
        text = p["content"]
        return dict(
            id=ids.github_file(repo["id"], path), kind="document", container=ids.github_repo(repo["id"]),
            aliases=[f"{repo['full_name']}/{path}"], title=path, text=text,
            url=f"https://github.com/{repo['full_name']}/blob/{quote(branch)}/{quote(path)}",
            created_at=date, updated_at=date, version=p["sha"], people=_People(),
            meta={"path": path, "branch": branch, "last_commit": last.get("sha")},
            repo_id=repo["id"], number=None, scan=text)

    # -- shared ------------------------------------------------------------- #

    def _finish(self, f: dict[str, Any], acl: Acl, fetched_at: str) -> ItemRecord:
        found = extract(f.pop("scan"), item_kind=f["kind"], repo_id=f.pop("repo_id"),
                        own_number=f.pop("number"), index=self.index)
        people: _People = f.pop("people")
        for login in found.mentions:
            people.add(login, "mentioned")
        meta = f["meta"]
        # Jira keys resolve through the Jira pipeline's last published keys (state/jira/keys.json).
        found.jira_keys = self.jira.resolve(found.jira_keys, found.refs, own_id=f["id"])
        if found.jira_keys:
            meta["jira_keys"] = found.jira_keys
        if found.slack_links:
            meta["slack_links"] = found.slack_links
        if found.external:
            meta["external_refs"] = found.external
        if found.dropped:
            meta["refs_dropped"] = found.dropped
        if people.bots:
            meta["bots"] = sorted(people.bots)
        text = f["text"]
        if len(text) > MAX_TEXT:
            text, meta["truncated"] = text[:MAX_TEXT], True
        f["text"] = text
        f["people"] = people.build()
        f["refs"] = found.refs
        f["meta"] = {k: v for k, v in meta.items() if v is not None}
        f["content_hash"] = content_hash(title=f["title"], text=text, container=f["container"],
                                         people=f["people"], refs=f["refs"], meta=f["meta"])
        return ItemRecord(source="github", acl=acl, fetched_at=fetched_at, run_id=self.run_id,
                          redactions=redactions_in({"title": f["title"], "text": text}), **f)
