"""A fake Jira Cloud built from tests/fixtures/jira/acme.json.

Serves tenant_info, project/search, search/jql (a project id and an updated
>= filter, ordered by updated, in pages of two with nextPageToken), issue
lookups by key (an old key answers with the moved issue), issue comments
(startAt pages) and user/permission/search through httpx.MockTransport.
Search returns an issue's first two comments, as the real API returns a
first page. Every request is recorded.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime

import httpx

from .github_fixture import FIXTURES, planted

NOW_ISO = "2026-10-21T12:00:00Z"
NOW = datetime.fromisoformat(NOW_ISO.replace("Z", "+00:00"))
BASE = "https://acme-sbx.atlassian.net"
SEARCH_PAGE = 2
COMMENTS_IN_SEARCH = 2


def load() -> dict:
    text = (FIXTURES / "jira" / "acme.json").read_text(encoding="utf-8")
    tokens = planted()
    text = re.sub(r"\{\{PLANTED:(\w+)\}\}",
                  lambda m: json.dumps(tokens[m.group(1)])[1:-1] if m.group(1) in tokens else m.group(0), text)
    data = json.loads(text)
    data.pop("_about")
    users = data.pop("_users")

    def resolve(value):
        if isinstance(value, dict):
            if set(value) == {"$user"}:
                return copy.deepcopy(users[value["$user"]])
            return {k: resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [resolve(v) for v in value]
        return value

    data = resolve(data)
    data["users"] = users
    return data


def _jira_time(stamp: str) -> str:
    """'2026-10-15T10:00:00.000+0000' -> '2026-10-15 10:00', for comparing with JQL dates."""
    return stamp[:16].replace("T", " ")


class FakeJira:
    def __init__(self, data: dict | None = None):
        self.data = data or load()
        self.requests: list[httpx.Request] = []
        self.forbidden: set[str] = set()          # paths that answer 403

    def issue(self, key_or_id: str) -> dict | None:
        for issue in self.data["issues"]:
            old = [c["fromString"] for h in issue["changelog"]["histories"] for c in h["items"]
                   if c["field"] == "Key"]
            if key_or_id in (issue["id"], issue["key"], *old):
                return issue
        return None

    def _search_view(self, issue: dict, q: httpx.QueryParams) -> dict:
        wanted = set(q.get("fields", "").split(",")) if q.get("fields") else set(issue["fields"])
        fields = {k: copy.deepcopy(v) for k, v in issue["fields"].items() if k in wanted}
        if "comment" in wanted:
            comments = issue["_comments"]
            fields["comment"] = {"comments": copy.deepcopy(comments[:COMMENTS_IN_SEARCH]), "total": len(comments),
                                 "maxResults": COMMENTS_IN_SEARCH, "startAt": 0}
        out = {"id": issue["id"], "key": issue["key"], "fields": fields}
        if "changelog" in q.get("expand", ""):
            out["changelog"] = copy.deepcopy(issue["changelog"])
        return out

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path, q = request.url.path, request.url.params
        if path in self.forbidden:
            return httpx.Response(403, json={"errorMessages": ["You do not have permission."]})
        if path == "/_edge/tenant_info":
            return httpx.Response(200, json={"cloudId": self.data["cloud_id"]})
        if path == "/rest/api/3/project/search":
            start, size = int(q.get("startAt", 0)), int(q.get("maxResults", 50))
            values = self.data["projects"][start:start + size]
            return httpx.Response(200, json={"values": values, "startAt": start, "maxResults": size,
                                             "total": len(self.data["projects"]),
                                             "isLast": start + size >= len(self.data["projects"])})
        if path == "/rest/api/3/search/jql":
            jql = q["jql"]
            project = re.search(r"project = (\d+)", jql).group(1)
            found = [i for i in self.data["issues"] if i["_project"] == project]
            if m := re.search(r'updated >= "([^"]+)"', jql):
                found = [i for i in found if _jira_time(i["fields"]["updated"]) >= m.group(1)]
            key = (lambda i: (i["fields"]["updated"], int(i["id"]))) if "ORDER BY updated" in jql \
                else (lambda i: int(i["id"]))
            found.sort(key=key)
            start = int(q.get("nextPageToken", 0))
            page = found[start:start + SEARCH_PAGE]
            body = {"issues": [self._search_view(i, q) for i in page], "isLast": start + SEARCH_PAGE >= len(found)}
            if not body["isLast"]:
                body["nextPageToken"] = str(start + SEARCH_PAGE)
            return httpx.Response(200, json=body)
        if m := re.fullmatch(r"/rest/api/3/issue/([^/]+)/comment", path):
            issue = self.issue(m.group(1))
            start, size = int(q.get("startAt", 0)), int(q.get("maxResults", 50))
            comments = issue["_comments"]
            return httpx.Response(200, json={"comments": copy.deepcopy(comments[start:start + size]),
                                             "startAt": start, "maxResults": size, "total": len(comments)})
        if m := re.fullmatch(r"/rest/api/3/issue/([^/]+)", path):
            issue = self.issue(m.group(1))
            if issue is None:
                return httpx.Response(404, json={"errorMessages": ["Issue does not exist"]})
            return httpx.Response(200, json={"id": issue["id"], "key": issue["key"],
                                             "fields": {"project": {"id": issue["_project"]}}})
        if path == "/rest/api/3/user/permission/search":
            assert q["permissions"] == "BROWSE_PROJECTS"
            names = self.data["browsers"][q["projectKey"]]
            start, size = int(q.get("startAt", 0)), int(q.get("maxResults", 50))
            return httpx.Response(200, json=[self.data["users"][n] for n in names[start:start + size]])
        return httpx.Response(404, json={"errorMessages": [f"no route {path}"]})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def paths(self) -> list[str]:
        return [r.url.path for r in self.requests]


def connector(fake: FakeJira | None = None, *, now=NOW, projects=None, known_keys=None):
    from swarm.http import Client
    from swarm.sources.jira import JiraConnector

    fake = fake or FakeJira()
    client = Client(BASE, transport=fake.transport(), sleep=lambda s: None)
    return JiraConnector(client, browse_url=BASE, projects=projects, now=now, known_keys=known_keys), fake
