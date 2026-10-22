"""
connector.py - GitHub behind the Connector interface.

Scope is what the App was installed on (or GITHUB_REPOS in token mode),
narrowed by GITHUB_REPOS when both are set. Every call is a read.

  enumerate   one container per granted repo
  fetch       the repo itself, then issues and PRs updated since the
              checkpoint (minus a 7-day lookback for edits and late
              comments), then default-branch commits in the same window
  list_ids    every issue, PR and commit id, for weekly reconciliation
  acl         public repos are public; private and internal repos are
              readable by the repo's readers group
  groups      that readers group: every collaborator, including access via
              teams and org defaults (affiliation=all), as person:github:{login}
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

from ... import ids
from ...http import Client, SourceError
from ...interfaces import Checkpoint, RawContainer, RawItem
from ...records import Acl, GroupRecord
from .refs import RepoIndex

LOOKBACK = timedelta(days=7)
COMMIT_DETAIL_CAP = 500        # commits per repo per run that get their file list


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def readers_group(repo_id: int) -> str:
    return f"group:{ids.github_repo(repo_id)}:readers"


class GitHubConnector:
    source = "github"

    def __init__(self, client: Client, *, mode: str, repos: list[str] | None = None,
                 external_tracker: list[str] | None = None,
                 now: datetime | None = None, backfill_days: int = 730):
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
        return _parse(since.cursor) - LOOKBACK

    # -- fetch ---------------------------------------------------------------- #

    def fetch(self, container: RawContainer, since: Checkpoint | None) -> Iterator[RawItem]:
        repo = container.meta["repo"]
        name, repo_id = repo["full_name"], repo["id"]
        start = _iso(self._window_start(since))
        fetched = _iso(self.now)
        brief = {k: repo.get(k) for k in ("id", "full_name", "default_branch", "private", "visibility")}
        newest = since.cursor if since else start

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
            newest = max(newest, issue["updated_at"])
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
            date = (commit["commit"].get("committer") or {}).get("date") or start
            newest = max(newest, date)
            yield item(f"commit-{commit['sha']}", {"type": "commit", "repo": brief, "commit": commit})

        self._cursors[container.id] = newest

    def _commits(self, name: str, repo: dict[str, Any], params: dict[str, Any]) -> Iterator[dict[str, Any]]:
        try:
            yield from self.client.paginate(f"/repos/{name}/commits", {
                "sha": repo.get("default_branch") or "main", "per_page": 100, **params})
        except SourceError as e:
            if e.status != 409:          # 409: the repository is empty
                raise

    def checkpoint(self, container: RawContainer) -> Checkpoint | None:
        cursor = self._cursors.get(container.id)
        return Checkpoint(container.id, cursor, _iso(self.now)) if cursor else None

    def published_state(self) -> dict[str, Any]:
        """state/github/repos.json: lowercased owner/name -> repo id for every
        granted repo, so Slack and Drive can link "acme/api#412" and GitHub
        URLs to GitHub IDs (swarm/sources/links.py)."""
        return {"repos": {name: repo_id for name, repo_id in sorted(self.index.full_names.items())}}

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

    def acl(self, item: RawItem) -> Acl:
        repo = item.container.meta["repo"]
        visibility = repo.get("visibility") or ("private" if repo.get("private") else "public")
        if visibility == "public":
            return Acl(visibility="public")
        return Acl(visibility="restricted", principals=[readers_group(repo["id"])])

    def groups(self, container: RawContainer, *, run_id: str) -> list[GroupRecord]:
        repo = container.meta["repo"]
        visibility = repo.get("visibility") or ("private" if repo.get("private") else "public")
        if visibility == "public":
            return []
        logins = sorted({c["login"] for c in self.client.paginate(
            f"/repos/{repo['full_name']}/collaborators", {"affiliation": "all", "per_page": 100})})
        return [GroupRecord(run_id=run_id, id=readers_group(repo["id"]), source="github",
                            name=f"{repo['full_name']} readers",
                            members=[ids.person("github", login) for login in logins],
                            observed_at=self.now)]
