"""A fake api.github.com built from tests/fixtures/github/acme-sbx.json.

Serves each 'METHOD /path' entry through httpx.MockTransport, paginates
{"pages": [...]} entries with real Link headers, resolves {"$ref": ...}, and
fills {{PLANTED:name}} from the redaction corpus. Every request is recorded so
tests can check what the connector asked for.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import httpx
import yaml

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CORPUS = FIXTURES / "redaction" / "corpus.yaml"
NOW_ISO = "2026-10-21T12:00:00Z"


def planted() -> dict[str, str]:
    data = yaml.safe_load(CORPUS.read_text(encoding="utf-8"))
    return {name: "".join(spec["parts"]) for name, spec in data["positives"].items()}


class FakeGitHub:
    def __init__(self, overrides: dict | None = None):
        text = (FIXTURES / "github" / "acme-sbx.json").read_text(encoding="utf-8")
        tokens = planted()
        # json.dumps(...)[1:-1] escapes the token for its place inside a JSON string.
        text = re.sub(r"\{\{PLANTED:(\w+)\}\}", lambda m: json.dumps(tokens[m.group(1)])[1:-1] if m.group(1) in tokens else m.group(0), text)
        data = json.loads(text)
        self.users = data.pop("_users")
        self.repos = data.pop("_repos")
        data.pop("_about")
        self.routes = {k: self._resolve(v) for k, v in data.items()}
        self.routes.update(overrides or {})
        self.requests: list[httpx.Request] = []

    def _resolve(self, value):
        if isinstance(value, dict):
            if "$ref" in value:
                kind, name = value["$ref"].split(":", 1)
                base = copy.deepcopy((self.users if kind == "user" else self.repos)[name])
                base.update({k: self._resolve(v) for k, v in value.items() if k != "$ref"})
                return base
            return {k: self._resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._resolve(v) for v in value]
        return value

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = f"{request.method} {request.url.path}"
        if key not in self.routes:
            return httpx.Response(404, json={"message": "Not Found"})
        entry = self.routes[key]
        if callable(entry):
            return entry(request)
        if isinstance(entry, dict) and "status" in entry and len(entry) <= 2:
            return httpx.Response(entry["status"], json=entry.get("body", {}), headers=entry.get("headers"))
        if isinstance(entry, dict) and "pages" in entry:
            pages = entry["pages"]
            page = int(request.url.params.get("page", "1"))
            headers = {}
            if page < len(pages):
                nxt = request.url.copy_merge_params({"page": str(page + 1)})
                headers["link"] = f'<{nxt}>; rel="next", <{request.url}>; rel="first"'
            return httpx.Response(200, json=pages[page - 1], headers=headers)
        return httpx.Response(200, json=entry)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def paths(self, method: str = "GET") -> list[str]:
        return [r.url.path for r in self.requests if r.method == method]


def connector(fake: FakeGitHub | None = None, *, mode: str = "app", repos: list[str] | None = None, now=None):
    """A GitHubConnector wired to the fake API, with sleeps disabled."""
    from datetime import datetime

    from swarm.http import Client
    from swarm.sources.github import GitHubConnector

    fake = fake or FakeGitHub()
    client = Client("https://api.github.com", transport=fake.transport(), sleep=lambda s: None)
    now = now or datetime.fromisoformat(NOW_ISO.replace("Z", "+00:00"))
    return GitHubConnector(client, mode=mode, repos=repos, now=now), fake
