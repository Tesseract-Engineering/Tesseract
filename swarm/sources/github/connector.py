"""
connector.py - GitHub behind the Connector interface.

Scope is what the App was installed on (or GITHUB_REPOS in token mode),
narrowed by GITHUB_REPOS when both are set. Every call is a read.

  enumerate   one container per granted repo
  fetch       the repo itself, then issues and PRs updated since the
              checkpoint (minus a 7-day lookback for edits and late
              comments), then default-branch commits in the same window,
              then the docs on the default branch whose blob changed
  list_ids    every issue, PR, commit and doc id, for weekly reconciliation
  acl         public repos are public; private and internal repos are
              readable by the repo's readers group
  groups      that readers group: every collaborator, including access via
              teams and org defaults (affiliation=all), as person:github:{login}.
              Listing collaborators needs push access; with a read-only
              credential the group is written with no members, so the graph
              side grants nobody through it rather than trusting stale members

Docs are the files that hold decisions and ownership in prose: Markdown and
reStructuredText (ADRs, READMEs, handbooks) and CODEOWNERS. Source code and
config are never read: they are where real secrets live, they dwarf
everything else, and they say what a system does rather than who decided
what. One tree call per repo lists the docs with their blob shas; a doc's
content and last commit are fetched only when its sha differs from the one
the last complete run published in state/github/docs.json, so an unchanged
doc costs nothing. A repo whose visibility changed since has all its docs
read again, so their records carry the new ACL. A file over MAX_DOC_BYTES is
generated, not written, and is out of scope. A doc left unread (past
MAX_DOCS, binary, undated) is not published, so every run reconsiders it.

A checkpoint is the newest issue update or commit date seen, never later
than the run's own clock: commit dates are set by whoever made the commit,
and one dated in the future would push the window past real activity.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

from ... import ids
from ...http import Client, SourceError
from ...interfaces import Checkpoint, RawContainer, RawItem
from ...records import Acl, GroupRecord
from ...stages.write import state_key
from ...store import ObjectStore
from ...timeutil import iso, parse_iso
from .refs import RepoIndex

log = logging.getLogger("swarm.github")

LOOKBACK = timedelta(days=7)
COMMIT_DETAIL_CAP = 500        # commits per repo per run that get their file list
DOC_SUFFIXES = (".md", ".markdown", ".rst")
DOC_NAMES = ("CODEOWNERS",)
SKIP_DIRS = {"node_modules", "vendor", "third_party", ".venv", "venv", "site-packages", "dist", "build"}
MAX_DOC_BYTES = 200_000        # the normalizer's MAX_TEXT; bigger files are generated, not written
MAX_DOCS = 500                 # docs per repo per run that get their content


def repo_visibility(repo: dict[str, Any]) -> str:
    """public, private or internal; older payloads carry only `private`."""
    return repo.get("visibility") or ("private" if repo.get("private") else "public")


def readers_group(repo_id: int) -> str:
    return f"group:{ids.github_repo(repo_id)}:readers"


def read_known_docs(store: ObjectStore) -> dict[str, dict[str, Any]]:
    """What the last complete run published in state/github/docs.json."""
    raw = store.get(state_key("github", "docs"))
    return json.loads(raw) if raw else {}


def is_doc(path: str) -> bool:
    parts = path.split("/")
    if SKIP_DIRS.intersection(parts[:-1]):
        return False
    return parts[-1].lower().endswith(DOC_SUFFIXES) or parts[-1] in DOC_NAMES


class GitHubConnector:
    source = "github"

    def __init__(self, client: Client, *, mode: str, repos: list[str] | None = None,
                 external_tracker: list[str] | None = None,
                 now: datetime | None = None, backfill_days: int = 730,
                 known_docs: dict[str, dict[str, Any]] | None = None):
        if mode == "token" and not repos:
            raise ValueError("token mode needs GITHUB_REPOS: a token has no installation to list")
        self.client = client
        self.mode = mode
        self.allow = {r.lower() for r in repos} if repos else None
        # Repos whose bare "#n" means an outside tracker (Trac, Bugzilla), not GitHub.
        self.external_tracker = {r.lower() for r in external_tracker or []}
        self.now = now or datetime.now(timezone.utc)
        self.backfill = timedelta(days=backfill_days)
        self.index = RepoIndex(lookup=self._lookup_kind)
        self._repos: dict[int, dict[str, Any]] = {}
        self._cursors: dict[str, str] = {}
        # repo id -> {"visibility": ..., "docs": {path: blob sha}}: what the last
        # complete run read (state/github/docs.json), and what this one has.
        self._known_docs = known_docs or {}
        self._docs_now: dict[str, dict[str, Any]] = {}

    # -- scope ---------------------------------------------------------------- #

    def enumerate(self) -> Iterator[RawContainer]:
        if self.mode == "app":
            repos = list(self.client.paginate("/installation/repositories", {"per_page": 100}, key="repositories"))
        else:
            repos = [self.client.get_json(f"/repos/{name}") for name in sorted(self.allow or [])]
        if self.allow is not None:
            repos = [r for r in repos if r["full_name"].lower() in self.allow]
        # Index every granted repo before any fetch, so cross-repo refs resolve.
        for repo in repos:
            self._repos[repo["id"]] = repo
            self.index.full_names[repo["full_name"].lower()] = repo["id"]
            if repo["full_name"].lower() in self.external_tracker:
                self.index.external.add(repo["id"])
        for repo in sorted(repos, key=lambda r: r["full_name"].lower()):
            yield RawContainer(source="github", id=ids.github_repo(repo["id"]), name=repo["full_name"],
                               meta={"repo": repo})

    def _window_start(self, since: Checkpoint | None) -> datetime:
        if since is None:
            return self.now - self.backfill
        return min(parse_iso(since.cursor), self.now) - LOOKBACK      # a cursor from the future heals

    # -- fetch ---------------------------------------------------------------- #

    def fetch(self, container: RawContainer, since: Checkpoint | None) -> Iterator[RawItem]:
        repo = container.meta["repo"]
        name, repo_id = repo["full_name"], repo["id"]
        start = iso(self._window_start(since))
        fetched = iso(self.now)
        brief = {k: repo.get(k) for k in ("id", "full_name", "default_branch", "private", "visibility")}
        newest = min(since.cursor, fetched) if since else start

        def item(native: str, payload: dict[str, Any]) -> RawItem:
            return RawItem(source="github", container=container, native_id=native, payload=payload,
                           fetched_at=fetched)

        yield item(f"repo-{repo_id}", {"type": "repo", "repo": repo})

        issues = list(self.client.paginate(f"/repos/{name}/issues", {
            "state": "all", "since": start, "sort": "updated", "direction": "asc", "per_page": 100}))
        for issue in issues:
            kind = "pull_request" if "pull_request" in issue else "issue"
            self.index.kinds[(repo_id, issue["number"])] = kind
        for issue in issues:
            n = issue["number"]
            newest = max(newest, min(iso(parse_iso(issue["updated_at"])), fetched))
            comments = (list(self.client.paginate(f"/repos/{name}/issues/{n}/comments", {"per_page": 100}))
                        if issue.get("comments") else [])
            if "pull_request" in issue:
                yield item(f"pr-{repo_id}-{n}", {
                    "type": "pull_request", "repo": brief, "issue": issue,
                    "pull": self.client.get_json(f"/repos/{name}/pulls/{n}"),
                    "reviews": list(self.client.paginate(f"/repos/{name}/pulls/{n}/reviews", {"per_page": 100})),
                    "review_comments": list(self.client.paginate(f"/repos/{name}/pulls/{n}/comments",
                                                                 {"per_page": 100})),
                    "comments": comments})
            else:
                yield item(f"issue-{repo_id}-{n}", {"type": "issue", "repo": brief, "issue": issue,
                                                    "comments": comments})

        for i, commit in enumerate(self._commits(name, repo, {"since": start})):
            if i < COMMIT_DETAIL_CAP:
                commit = self.client.get_json(f"/repos/{name}/commits/{commit['sha']}")
            date = (commit["commit"].get("committer") or {}).get("date")
            if date:
                newest = max(newest, min(iso(parse_iso(date)), fetched))
            yield item(f"commit-{commit['sha']}", {"type": "commit", "repo": brief, "commit": commit})

        for doc in self._docs(repo):
            yield item(f"file-{repo_id}-{doc['path']}", {"type": "file", "repo": brief, **doc})

        self._cursors[container.id] = newest

    def _tree(self, repo: dict[str, Any]) -> list[dict[str, Any]]:
        """Every doc blob on the default branch, in path order."""
        branch = repo.get("default_branch") or "main"
        try:
            tree = self.client.get_json(f"/repos/{repo['full_name']}/git/trees/{branch}", {"recursive": "1"})
        except SourceError as e:
            if e.status in (404, 409):   # no default branch yet; the repository is empty
                return []
            raise
        if tree.get("truncated"):
            log.warning("tree of repo %s is truncated; some docs are not read this run", repo["id"])
        return sorted((e for e in tree.get("tree") or [] if e.get("type") == "blob" and is_doc(e["path"])
                       and (e.get("size") or 0) <= MAX_DOC_BYTES), key=lambda e: e["path"])

    def _docs(self, repo: dict[str, Any]) -> Iterator[dict[str, Any]]:
        name, key = repo["full_name"], str(repo["id"])
        last_run = self._known_docs.get(key) or {}
        known = last_run.get("docs", {}) if last_run.get("visibility") == repo_visibility(repo) else {}
        current: dict[str, str] = {}
        self._docs_now[key] = {"visibility": repo_visibility(repo), "docs": current}
        read = skipped = 0
        for entry in self._tree(repo):
            path, sha = entry["path"], entry["sha"]
            if known.get(path) == sha:
                current[path] = sha
                continue
            if read >= MAX_DOCS:
                skipped += 1
                continue
            blob = self.client.get_json(f"/repos/{name}/git/blobs/{sha}")
            data = base64.b64decode(blob.get("content") or "") if blob.get("encoding") == "base64" \
                else (blob.get("content") or "").encode("utf-8")
            if b"\0" in data:
                skipped += 1
                continue
            last = next(iter(self.client.paginate(f"/repos/{name}/commits", {
                "sha": repo.get("default_branch") or "main", "path": path, "per_page": 1})), None)
            if last is None:             # nothing to date it by; not published, so retried next run
                skipped += 1
                continue
            read += 1
            current[path] = sha
            # Decoded here, so redaction (run.py) sees the text, never base64.
            yield {"path": path, "sha": sha, "content": data.decode("utf-8", errors="replace"), "last_commit": last}
        if skipped:
            log.info("repo %s: %d docs not read (binary, undated or over the per-run cap)", repo["id"], skipped)

    def _commits(self, name: str, repo: dict[str, Any], params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        try:
            yield from self.client.paginate(f"/repos/{name}/commits", {
                "sha": repo.get("default_branch") or "main", "per_page": 100, **params})
        except SourceError as e:
            if e.status != 409:          # 409: the repository is empty
                raise

    def checkpoint(self, container: RawContainer) -> Checkpoint | None:
        cursor = self._cursors.get(container.id)
        return Checkpoint(container.id, cursor, iso(self.now)) if cursor else None

    def published_state(self) -> dict[str, Any]:
        """state/github/repos.json: lowercased owner/name -> repo id for every
        granted repo, so Slack and Drive can link "acme/api#412" and GitHub
        URLs to GitHub IDs (swarm/sources/links.py)."""
        return {"repos": {name: repo_id for name, repo_id in sorted(self.index.full_names.items())},
                "docs": self._published_docs()}

    def _published_docs(self) -> dict[str, dict[str, Any]]:
        """state/github/docs.json: per repo id, its visibility and the blob sha
        of every doc read or confirmed unchanged, so the next run skips them.
        A granted repo this run did not reach keeps what the last run published."""
        merged = {k: v for k, v in self._known_docs.items() if int(k) in self._repos}
        merged.update(self._docs_now)
        return {k: {"visibility": v["visibility"], "docs": dict(sorted(v["docs"].items()))}
                for k, v in sorted(merged.items())}

    def _lookup_kind(self, repo_id: int, number: int) -> str | None:
        repo = self._repos.get(repo_id)
        if repo is None:
            return None
        found = self.client.get_optional(f"/repos/{repo['full_name']}/issues/{number}")
        if found is None:
            return None
        return "pull_request" if "pull_request" in found else "issue"

    # -- reconciliation, permissions -------------------------------------------- #

    def list_ids(self, container: RawContainer) -> Iterator[str]:
        repo = container.meta["repo"]
        name, repo_id = repo["full_name"], repo["id"]
        yield container.id
        for issue in self.client.paginate(f"/repos/{name}/issues", {"state": "all", "per_page": 100}):
            build = ids.github_pr if "pull_request" in issue else ids.github_issue
            yield build(repo_id, issue["number"])
        for commit in self._commits(name, repo, {}):
            yield ids.github_commit(repo_id, commit["sha"])
        for entry in self._tree(repo):
            yield ids.github_file(repo_id, entry["path"])

    def acl(self, item: RawItem) -> Acl:
        repo = item.container.meta["repo"]
        if repo_visibility(repo) == "public":
            return Acl(visibility="public")
        return Acl(visibility="restricted", principals=[readers_group(repo["id"])])

    def groups(self, container: RawContainer, *, run_id: str) -> list[GroupRecord]:
        repo = container.meta["repo"]
        if repo_visibility(repo) == "public":
            return []
        try:
            logins = sorted({c["login"] for c in self.client.paginate(
                f"/repos/{repo['full_name']}/collaborators", {"affiliation": "all", "per_page": 100})})
        except SourceError as e:
            if e.status != 403:
                raise
            log.warning("cannot list collaborators of repo %s; its readers group is written empty", repo["id"])
            logins = []
        return [GroupRecord(run_id=run_id, id=readers_group(repo["id"]), source="github",
                            name=f"{repo['full_name']} readers",
                            members=[ids.person("github", login) for login in logins],
                            observed_at=self.now)]
