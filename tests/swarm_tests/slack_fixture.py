"""A fake Slack workspace built from tests/fixtures/slack/acme.json.

FakeSlack serves it as the Web API through httpx.MockTransport: history
returns top-level messages and broadcasts newest first, replies return a
thread oldest first, and every list is cursor-paginated in small pages so
pagination is exercised. write_export lays the same data out as an export
folder. Every request is recorded so tests can check what was asked for.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .github_fixture import FIXTURES, planted

NOW_ISO = "2026-10-21T12:00:00Z"
NOW = datetime.fromisoformat(NOW_ISO.replace("Z", "+00:00"))
WORKSPACE = "https://acme.slack.com"
PAGE = 3


def load() -> dict:
    text = (FIXTURES / "slack" / "acme.json").read_text(encoding="utf-8")
    tokens = planted()
    text = re.sub(r"\{\{PLANTED:(\w+)\}\}",
                  lambda m: json.dumps(tokens[m.group(1)])[1:-1] if m.group(1) in tokens else m.group(0), text)
    data = json.loads(text)
    data.pop("_about")
    return data


def _key(ts: str) -> tuple[int, int]:
    sec, _, frac = ts.partition(".")
    return int(sec), int(frac or 0)


def _is_reply(m: dict) -> bool:
    return bool(m.get("thread_ts")) and m["thread_ts"] != m["ts"]


class FakeSlack:
    def __init__(self, data: dict | None = None, errors: dict[str, str] | None = None):
        self.data = data or load()
        self.errors = errors or {}            # method -> Slack error code
        self.requests: list[httpx.Request] = []

    def _page(self, items: list, request: httpx.Request, key: str, **extra) -> httpx.Response:
        start = int(request.url.params.get("cursor") or 0)
        body = {"ok": True, key: items[start:start + PAGE], **extra}
        nxt = start + PAGE
        body["response_metadata"] = {"next_cursor": str(nxt) if nxt < len(items) else ""}
        return httpx.Response(200, json=body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method = request.url.path.rsplit("/", 1)[-1]
        q = request.url.params
        if method in self.errors:
            return httpx.Response(200, json={"ok": False, "error": self.errors[method]})
        if method == "auth.test":
            team = self.data["team"]
            return httpx.Response(200, json={"ok": True, "team_id": team["id"], "url": team["url"],
                                             "user_id": "UB0TSWARM", "bot_id": "B0SWARM"})
        if method == "users.list":
            return self._page(self.data["users"], request, "members")
        if method == "conversations.list":
            channels = [{k: v for k, v in ch.items() if k != "members"} for ch in self.data["channels"]]
            return self._page(channels, request, "channels")
        messages = self.data["messages"].get(q.get("channel"), [])
        if method == "conversations.history":
            oldest = _key(q.get("oldest") or "0")
            top = [m for m in messages if (not _is_reply(m) or m.get("subtype") == "thread_broadcast")
                   and _key(m["ts"]) >= oldest]
            return self._page(sorted(top, key=lambda m: _key(m["ts"]), reverse=True), request, "messages")
        if method == "conversations.replies":
            thread = [m for m in messages if m["ts"] == q["ts"] or m.get("thread_ts") == q["ts"]]
            if not thread:
                return httpx.Response(200, json={"ok": False, "error": "thread_not_found"})
            return self._page(sorted(thread, key=lambda m: _key(m["ts"])), request, "messages")
        if method == "conversations.members":
            ch = next(c for c in self.data["channels"] if c["id"] == q["channel"])
            return self._page(ch["members"], request, "members")
        return httpx.Response(200, json={"ok": False, "error": "unknown_method"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def calls(self, method: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path.endswith("/" + method)]


def write_export(root: Path, data: dict | None = None) -> Path:
    """The workspace as an export folder: one file per channel per UTC day."""
    data = copy.deepcopy(data or load())
    root.mkdir(parents=True, exist_ok=True)
    (root / "users.json").write_text(json.dumps(data["users"]), encoding="utf-8")
    public = [c for c in data["channels"] if not c["is_private"]]
    private = [c for c in data["channels"] if c["is_private"]]
    for name, channels in (("channels.json", public), ("groups.json", private)):
        (root / name).write_text(json.dumps([{k: v for k, v in c.items() if k not in ("is_private", "is_member")}
                                             for c in channels]), encoding="utf-8")
    (root / "dms.json").write_text(json.dumps([{"id": "D01", "members": ["U123SAM", "U456PRI"]}]), encoding="utf-8")
    for ch in data["channels"]:
        days: dict[str, list] = {}
        for m in data["messages"].get(ch["id"], []):
            day = datetime.fromtimestamp(_key(m["ts"])[0], tz=timezone.utc).date().isoformat()
            days.setdefault(day, []).append(m)
        for day, msgs in days.items():
            path = root / ch["name"] / f"{day}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(sorted(msgs, key=lambda m: _key(m["ts"]))), encoding="utf-8")
    # A DM folder the connector must never read.
    (root / "D01").mkdir(exist_ok=True)
    (root / "D01" / "2026-10-14.json").write_text(json.dumps([{"type": "message", "user": "U123SAM",
                                                                "ts": "1791986400.000900", "text": "private DM"}]))
    return root


def api_connector(fake: FakeSlack | None = None, *, now=NOW, **kwargs):
    from swarm.http import Client
    from swarm.sources.slack import SlackApiConnector

    fake = fake or FakeSlack()
    client = Client("https://slack.com/api", transport=fake.transport(), sleep=lambda s: None)
    return SlackApiConnector(client, now=now, **kwargs), fake


def export_connector(root: Path, *, now=NOW, **kwargs):
    from swarm.sources.slack import SlackExportConnector

    return SlackExportConnector(root, workspace_url=WORKSPACE, now=now, **kwargs)
