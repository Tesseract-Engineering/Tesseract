"""
connector.py - Jira Cloud behind the Connector interface.

Scope is every project the service account can browse, narrowed by
JIRA_PROJECTS (keys). Every call is a read, on the v3 REST API (v2 search is
retired).

  enumerate   the site's cloud id first (/_edge/tenant_info): IDs use it, not
              the site's name, which can be renamed. Then one container per
              project, jira:{site}:project:{id}
  fetch       the project itself (its description and lead), then issues
              updated since the checkpoint minus a 7-day lookback,
              oldest first, through /search/jql. JQL dates are read in the
              account's own time zone, and the lookback absorbs the
              difference. Descriptions and comments are turned from ADF into
              text here (adf.py), so redaction sees the text a person reads;
              comments past what search returns are paged in
  list_ids    the project and every issue id in it, for weekly reconciliation
  acl         the project's browsers group, for the project and its issues;
              an issue with a security level is readable by that level's
              group instead
  groups      the browsers group: every active person with Browse Projects on
              the project, as user:{email} when Atlassian shows the email and
              person:jira:{account id} when it hides it, as it does by default.
              Listing them needs the Browse users and groups permission;
              without it the group is written with no members, so the graph
              side grants nobody through it. Security level groups are written
              with no members: the API names their holders only to admins

Keys change when an issue moves between projects; the id does not. Each run
publishes every key it has seen, old keys from the changelog included, in
state/jira/keys.json, so a Slack thread that still says SBX-8 links to the
issue that is now OPS-2.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

from ... import ids
from ...http import Client, SourceError
from ...interfaces import Checkpoint, RawContainer, RawItem
from ...records import Acl, GroupRecord
from ...timeutil import iso, parse_iso
from ..links import JiraLinks
from . import adf

log = logging.getLogger("swarm.jira")

LOOKBACK = timedelta(days=7)
PAGE = 100
FIELDS = ("summary", "description", "issuetype", "status", "priority", "labels", "components", "assignee",
          "reporter", "created", "updated", "resolution", "resolutiondate", "duedate", "parent", "issuelinks",
          "comment", "security")


def browsers_group(site: str, project_id: str) -> str:
    return f"group:jira:{site}:project:{project_id}:browsers"


def security_group(site: str, level_id: str) -> str:
    return f"group:jira:{site}:security:{level_id}"


def old_keys(issue: dict[str, Any]) -> list[str]:
    """Keys the issue had before a move, from its changelog."""
    found = []
    for history in (issue.get("changelog") or {}).get("histories") or []:
        for change in history.get("items") or []:
            if (change.get("field") or "").lower() == "key" and change.get("fromString"):
                found.append(change["fromString"])
    return sorted(set(found) - {issue["key"]})


class JiraConnector:
    source = "jira"

    def __init__(self, client: Client, *, browse_url: str, projects: list[str] | None = None,
                 now: datetime | None = None, backfill_days: int = 730,
                 known_keys: dict[str, Any] | None = None):
        self.client = client
        self.browse_url = browse_url.rstrip("/")
        self.allow = {p.upper() for p in projects} if projects else None
        self.now = now or datetime.now(timezone.utc)
        self.backfill = timedelta(days=backfill_days)
        known = known_keys or {}
        self.index = JiraLinks(site=known.get("site"), keys=dict(known.get("keys", {})), lookup=self._lookup)
        self._projects: dict[str, dict[str, Any]] = {}
        self._levels: dict[str, dict[str, str]] = {}           # container id -> {level id: name}
        self._cursors: dict[str, str] = {}

    # -- scope ---------------------------------------------------------------- #

    def enumerate(self) -> Iterator[RawContainer]:
        site = self.client.get_json("/_edge/tenant_info")["cloudId"]
        if self.index.site and self.index.site != site:
            log.warning("the Jira site changed since the last run; keys from that site are dropped")
            self.index.keys.clear()
        self.index.site = site
        projects = list(self._start_at("/rest/api/3/project/search", {"orderBy": "key", "expand": "description,lead"},
                                       key="values"))
        if self.allow is not None:
            projects = [p for p in projects if p["key"].upper() in self.allow]
        for project in projects:
            self._projects[project["id"]] = project
        for project in sorted(projects, key=lambda p: p["key"]):
            yield RawContainer(source="jira", id=ids.jira_project(site, project["id"]), name=project["key"],
                               meta={"project": {k: project.get(k) for k in ("id", "key", "name")}})

    @property
    def site(self) -> str:
        assert self.index.site, "enumerate() reads the site first"
        return self.index.site

    def _window_start(self, since: Checkpoint | None) -> datetime:
        if since is None:
            return self.now - self.backfill
        return min(parse_iso(since.cursor), self.now) - LOOKBACK

    # -- fetch ---------------------------------------------------------------- #

    def fetch(self, container: RawContainer, since: Checkpoint | None) -> Iterator[RawItem]:
        project = container.meta["project"]
        start = self._window_start(since)
        fetched = iso(self.now)
        newest = min(since.cursor, fetched) if since else iso(start)
        jql = (f'project = {project["id"]} AND updated >= "{start:%Y-%m-%d %H:%M}" '
               f"ORDER BY updated ASC, id ASC")
        yield RawItem(source="jira", container=container, native_id=f"project-{project['id']}",
                      payload=self._project_payload(project), fetched_at=fetched)
        issues = list(self._search(jql, fields=",".join(FIELDS), expand="changelog"))
        # Index every key first, so text in one issue links to a later one.
        for issue in issues:
            for key in [issue["key"], *old_keys(issue)]:
                self.index.keys[key] = issue["id"]
        levels = self._levels.setdefault(container.id, {})
        for issue in issues:
            fields = issue["fields"]
            newest = max(newest, min(iso(parse_iso(fields["updated"])), fetched))
            if fields.get("security"):
                levels[fields["security"]["id"]] = fields["security"].get("name") or ""
            yield RawItem(source="jira", container=container, native_id=f"issue-{issue['id']}",
                          payload=self._payload(issue, project), fetched_at=fetched)
        self._cursors[container.id] = newest

    def _project_payload(self, project: dict[str, Any]) -> dict[str, Any]:
        full = self._projects.get(project["id"], {})
        return {"type": "project", "site": self.site, "project": project,
                "url": f"{self.browse_url}/browse/{project['key']}", "description": full.get("description"),
                "lead": full.get("lead"), "project_type": full.get("projectTypeKey"),
                "category": (full.get("projectCategory") or {}).get("name")}

    def _payload(self, issue: dict[str, Any], project: dict[str, Any]) -> dict[str, Any]:
        fields = dict(issue["fields"])
        description = adf.to_text(fields.pop("description", None))
        comments = []
        for c in self._comments(issue, fields.pop("comment", None) or {}):
            body = adf.to_text(c.get("body"))
            comments.append({"id": c.get("id"), "author": c.get("author"), "created": c.get("created"),
                             "text": body.text, "mentions": body.mentions, "mention_names": body.names})
        return {"type": "issue", "site": self.site, "project": project,
                "issue": {"id": issue["id"], "key": issue["key"]},
                "url": f"{self.browse_url}/browse/{issue['key']}", "old_keys": old_keys(issue),
                "fields": fields, "description": description.text, "mentions": description.mentions,
                "mention_names": description.names, "comments": comments}

    def _comments(self, issue: dict[str, Any], page: dict[str, Any]) -> list[dict[str, Any]]:
        got = page.get("comments") or []
        if page.get("total", len(got)) <= len(got):
            return got
        return list(self._start_at(f"/rest/api/3/issue/{issue['id']}/comment", {"orderBy": "created"},
                                   key="comments"))

    def _search(self, jql: str, **params: Any) -> Iterator[dict[str, Any]]:
        yield from self.client.paginate_cursor(
            "/rest/api/3/search/jql", {"jql": jql, "maxResults": PAGE, **params}, key="issues",
            cursor_param="nextPageToken", next_cursor=lambda b: None if b.get("isLast") else b.get("nextPageToken"))

    def _start_at(self, path: str, params: dict[str, Any], *, key: str | None = None) -> Iterator[Any]:
        """startAt/maxResults paging: an object with `key` (and isLast or
        total), or a bare list that ends on a short page."""
        start = 0
        while True:
            body = self.client.get_json(path, {**params, "startAt": start, "maxResults": PAGE})
            page = body if key is None else body.get(key) or []
            yield from page
            start += len(page)
            if not page:
                return
            if key is None:
                if len(page) < PAGE:
                    return
            elif body.get("isLast") or start >= body.get("total", float("inf")):
                return

    def _lookup(self, key: str) -> str | None:
        """A key not seen yet, asked of the API: its id when it is in a
        project in scope. Jira answers an old key with the moved issue."""
        try:
            found = self.client.get_optional(f"/rest/api/3/issue/{key}", {"fields": "project"})
        except SourceError as e:
            if e.status not in (400, 403):              # a malformed key; a project the account cannot see
                raise
            return None
        if not found or found.get("fields", {}).get("project", {}).get("id") not in self._projects:
            return None
        return found["id"]

    def checkpoint(self, container: RawContainer) -> Checkpoint | None:
        cursor = self._cursors.get(container.id)
        return Checkpoint(container.id, cursor, iso(self.now)) if cursor else None

    def published_state(self) -> dict[str, Any]:
        """state/jira/keys.json: every key seen, old keys included, -> issue
        id, for this pipeline's next run and for every other source."""
        return {"keys": {"site": self.index.site, "keys": dict(sorted(self.index.keys.items()))}}

    # -- reconciliation, permissions -------------------------------------------- #

    def list_ids(self, container: RawContainer) -> Iterator[str]:
        yield container.id
        for issue in self._search(f'project = {container.meta["project"]["id"]} ORDER BY id ASC', fields="summary"):
            yield ids.jira_issue(self.site, issue["id"])

    def acl(self, item: RawItem) -> Acl:
        level = (item.payload.get("fields") or {}).get("security")
        if level:
            return Acl(visibility="restricted", principals=[security_group(self.site, level["id"])])
        return Acl(visibility="restricted",
                   principals=[browsers_group(self.site, item.payload["project"]["id"])])

    def groups(self, container: RawContainer, *, run_id: str) -> list[GroupRecord]:
        project = container.meta["project"]
        try:
            users = list(self._start_at("/rest/api/3/user/permission/search",
                                        {"permissions": "BROWSE_PROJECTS", "projectKey": project["key"]}))
        except SourceError as e:
            if e.status not in (401, 403):
                raise
            log.warning("cannot list who may browse project %s; its browsers group is written empty", project["id"])
            users = []
        members = sorted({f"user:{u['emailAddress'].lower()}" if u.get("emailAddress")
                          else ids.jira_person(u["accountId"])
                          for u in users if u.get("accountType", "atlassian") == "atlassian" and u.get("active", True)})
        out = [GroupRecord(run_id=run_id, id=browsers_group(self.site, project["id"]), source="jira",
                           name=f"{project['key']} browsers", members=members, observed_at=self.now)]
        for level_id, name in sorted(self._levels.get(container.id, {}).items()):
            out.append(GroupRecord(run_id=run_id, id=security_group(self.site, level_id), source="jira",
                                   name=f"{project['key']} security level {name}".strip(), members=[],
                                   observed_at=self.now))
        return out
