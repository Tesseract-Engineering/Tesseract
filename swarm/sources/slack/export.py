"""
export.py - a Slack workspace export behind the Connector interface.

The cold start: an admin's export (Settings > Import/Export Data), as the
.zip Slack produces or unpacked into a folder:

  users.json            the user directory
  channels.json         public channels, with their members
  groups.json           private channels, in exports that include them
  {channel name}/{YYYY-MM-DD}.json   that day's messages, replies inline

DMs (dms.json, mpims.json) are never read. An export has no team id or
workspace URL; the team id comes from users.json unless SLACK_TEAM_ID is
set, and SLACK_WORKSPACE_URL is required for permalinks.

An export is a snapshot, so fetch ignores checkpoints and reads everything;
unchanged conversations write nothing (swarm/stages/write.py). It still
writes checkpoints, in the API connector's shape, so the first API run
continues from where the export ends.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from ...interfaces import Checkpoint, RawContainer, RawItem
from .connector import ACTIVE_DAYS, SlackBase, channel_brief


class _Files:
    """Read-only access to the export, zipped or not."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.zip = zipfile.ZipFile(self.path) if self.path.is_file() else None
        names = self.zip.namelist() if self.zip else [
            p.relative_to(self.path).as_posix() for p in self.path.rglob("*.json")]
        # Some exports wrap everything in one top folder.
        users = sorted((n for n in names if PurePosixPath(n).name == "users.json"), key=len)
        if not users:
            raise ValueError(f"{self.path}: no users.json; is this a Slack export?")
        self.root = str(PurePosixPath(users[0]).parent).removeprefix(".")
        self.names = names

    def _key(self, name: str) -> str:
        return f"{self.root}/{name}" if self.root else name

    def read(self, name: str, default: Any = None) -> Any:
        key = self._key(name)
        if self.zip:
            if key not in self.names:
                return default
            return json.loads(self.zip.read(key))
        path = self.path / key
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else default

    def days(self, channel_name: str) -> list[str]:
        prefix = self._key(f"{channel_name}/")
        return sorted(n.removeprefix(self._key("")) for n in self.names
                      if n.startswith(prefix) and n.endswith(".json") and "/" not in n[len(prefix):])


class SlackExportConnector(SlackBase):
    def __init__(self, path: str | Path, *, workspace_url: str, team_id: str | None = None,
                 channels: list[str] | None = None, now: datetime | None = None, active_days: int = ACTIVE_DAYS):
        if not workspace_url:
            raise ValueError("an export needs SLACK_WORKSPACE_URL (https://<workspace>.slack.com) for permalinks")
        super().__init__(team_id=team_id or "", workspace_url=workspace_url, channels=channels, now=now,
                         active_days=active_days)
        self.files = _Files(path)
        self._channel_members: dict[str, list[str]] = {}

    def enumerate(self) -> Iterator[RawContainer]:
        users = self.files.read("users.json", [])
        self.directory.load(users)
        if not self.team_id:
            teams = {u.get("team_id") or u.get("team") for u in users} - {None}
            if len(teams) != 1:
                raise ValueError("cannot tell the team id from users.json; set SLACK_TEAM_ID")
            self.team_id = teams.pop()
        briefs = []
        for name, private in (("channels.json", False), ("groups.json", True)):
            for ch in self.files.read(name, []):
                self._channel_members[ch["id"]] = list(ch.get("members") or [])
                briefs.append(channel_brief(ch, is_private=private))
        for brief in sorted((b for b in briefs if self._in_scope(b)), key=lambda b: b["name"].lower()):
            yield self._container(brief)

    def _messages(self, container: RawContainer) -> list[dict[str, Any]]:
        name = container.meta["channel"]["name"]
        return [m for day in self.files.days(name) for m in self.files.read(day, [])]

    def fetch(self, container: RawContainer, since: Checkpoint | None) -> Iterator[RawItem]:
        previous = json.loads(since.cursor) if since else None
        yield from self._items(container, self._messages(container), previous)

    def list_ids(self, container: RawContainer) -> Iterator[str]:
        yield from self._ids(container, self._messages(container))

    def _members(self, channel_id: str) -> list[str]:
        return self._channel_members.get(channel_id, [])
