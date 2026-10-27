"""
connector.py - Slack behind the Connector interface.

Two connectors share everything but where messages come from:

  SlackApiConnector      the Web API with a bot token; nightly runs
  SlackExportConnector   a workspace export (export.py); the cold start

Scope is the channels the bot was invited to (an export: every public and
private channel in it), narrowed by SLACK_CHANNELS. DMs and group DMs are
never read.

  enumerate   one container per channel; loads the user directory first
  fetch       the channel itself, then its threads and bursts
              (normalize.py); the API reads history from midnight UTC of the
              checkpoint minus 7 days, every thread started in that window,
              and every thread still active in the last ACTIVE_DAYS, so a late
              reply to an old thread is not missed
  list_ids    the channel and every thread and burst, for reconciliation
  acl         public channels are readable by the whole workspace (internal);
              private channels by their members group
  groups      that members group: user:{email}, or person:slack:{id} where
              the profile has no email; bots left out, unknown ids counted
              as unresolved

A checkpoint's cursor is JSON: {"ts": newest message ts seen, "active":
{thread_ts: latest reply ts}}. The export writes the same shape, so the first
API run after a cold start picks up where the export ended.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

from ... import ids
from ...http import Client, SourceError
from ...interfaces import Checkpoint, RawContainer, RawItem
from ...records import Acl, GroupRecord
from ...timeutil import iso
from .directory import Directory
from .normalize import Conversation, clean_message, group_conversations, time_ts, ts_key, ts_time

API = "https://slack.com/api"
LOOKBACK = timedelta(days=7)
ACTIVE_DAYS = 30
PAGE = 200


def members_group(team_id: str, channel_id: str) -> str:
    return f"group:{ids.slack_channel(team_id, channel_id)}:members"


def channel_brief(ch: dict[str, Any], *, is_private: bool) -> dict[str, Any]:
    """The channel fields items and ACLs need, the same from an export's
    channels.json and from conversations.list."""
    return {"id": ch["id"], "name": ch.get("name") or ch["id"], "is_private": is_private,
            "is_archived": bool(ch.get("is_archived")), "created": ch.get("created") or 0,
            "creator": ch.get("creator"),
            "topic": (ch.get("topic") or {}).get("value") or "", "topic_set": (ch.get("topic") or {}).get("last_set") or 0,
            "purpose": (ch.get("purpose") or {}).get("value") or "",
            "purpose_set": (ch.get("purpose") or {}).get("last_set") or 0}


class SlackError(SourceError):
    """Slack said ok: false. Its error code (not_in_channel, missing_scope,
    invalid_auth, ...) is the message; nothing else from the body is kept."""

    def __init__(self, error: str, method: str):
        RuntimeError.__init__(self, f"{method} -> {error}")
        self.status = 200
        self.error = error


class SlackBase:
    """What both connectors share: scope, items, ACLs, groups, checkpoints."""
    source = "slack"

    def __init__(self, *, team_id: str = "", workspace_url: str = "", channels: list[str] | None = None,
                 now: datetime | None = None, active_days: int = ACTIVE_DAYS):
        self.team_id = team_id
        self.workspace_url = workspace_url.rstrip("/")
        self.allow = {c.strip().lstrip("#").lower() for c in channels} if channels else None
        self.now = now or datetime.now(timezone.utc)
        self.active_window = timedelta(days=active_days)
        self.directory = Directory()
        self._cursors: dict[str, dict[str, Any]] = {}

    # -- scope ---------------------------------------------------------------- #

    def _in_scope(self, ch: dict[str, Any]) -> bool:
        return self.allow is None or ch["id"].lower() in self.allow or ch["name"].lower() in self.allow

    def _container(self, brief: dict[str, Any]) -> RawContainer:
        return RawContainer(source="slack", id=ids.slack_channel(self.team_id, brief["id"]),
                            name=f"#{brief['name']}", meta={"channel": brief})

    # -- items ---------------------------------------------------------------- #

    def _items(self, container: RawContainer, messages: list[dict[str, Any]],
               previous: dict[str, Any] | None) -> Iterator[RawItem]:
        brief = container.meta["channel"]
        fetched = iso(self.now)
        base = {"team_id": self.team_id, "workspace_url": self.workspace_url, "channel": brief}

        def item(native: str, payload: dict[str, Any]) -> RawItem:
            return RawItem(source="slack", container=container, native_id=native, payload=payload,
                           fetched_at=fetched)

        yield item(f"channel-{brief['id']}", {"type": "channel", **base})
        conversations = group_conversations([clean_message(m) for m in messages])
        for c in conversations:
            yield item(f"{c.grouping}-{brief['id']}-{c.anchor}", {
                "type": "thread", **base, "grouping": c.grouping, "anchor": c.anchor, "messages": c.messages})
        self._advance(container.id, conversations, previous)

    def _advance(self, container_id: str, conversations: list[Conversation], previous: dict[str, Any] | None) -> None:
        newest = (previous or {}).get("ts")
        active: dict[str, str] = {}
        horizon = self.now - self.active_window
        for c in conversations:
            latest = c.latest
            newest = latest if newest is None else max(newest, latest, key=ts_key)
            if c.grouping == "thread" and ts_time(latest) >= horizon:
                active[c.anchor] = latest
        if newest is not None:
            self._cursors[container_id] = {"ts": newest, "active": dict(sorted(active.items()))}

    def checkpoint(self, container: RawContainer) -> Checkpoint | None:
        cursor = self._cursors.get(container.id)
        if cursor is None:
            return None
        return Checkpoint(container.id, json.dumps(cursor, sort_keys=True), iso(self.now))

    def _ids(self, container: RawContainer, messages: list[dict[str, Any]]) -> Iterator[str]:
        channel_id = container.meta["channel"]["id"]
        yield container.id
        for c in group_conversations([clean_message(m) for m in messages]):
            yield ids.slack_thread(self.team_id, channel_id, c.anchor)

    # -- permissions ---------------------------------------------------------- #

    def acl(self, item: RawItem) -> Acl:
        brief = item.container.meta["channel"]
        if not brief["is_private"]:
            return Acl(visibility="internal")
        return Acl(visibility="restricted", principals=[members_group(self.team_id, brief["id"])])

    def _members(self, channel_id: str) -> list[str]:
        raise NotImplementedError

    def groups(self, container: RawContainer, *, run_id: str) -> list[GroupRecord]:
        brief = container.meta["channel"]
        if not brief["is_private"]:
            return []
        members: set[str] = set()
        unresolved = 0
        for uid in self._members(brief["id"]):
            user = self.directory.get(uid)
            if user is None:
                unresolved += 1
            elif not (user.is_bot or user.deleted):
                members.add(f"user:{user.email}" if user.email else ids.person("slack", uid))
        return [GroupRecord(run_id=run_id, id=members_group(self.team_id, brief["id"]), source="slack",
                            name=f"#{brief['name']} members", members=sorted(members), unresolved=unresolved,
                            observed_at=self.now)]


class SlackApiConnector(SlackBase):
    def __init__(self, client: Client, *, channels: list[str] | None = None, now: datetime | None = None,
                 backfill_days: int = 730, active_days: int = ACTIVE_DAYS):
        super().__init__(channels=channels, now=now, active_days=active_days)
        self.client = client
        self.backfill = timedelta(days=backfill_days)

    # -- the Web API -------------------------------------------------------- #

    @staticmethod
    def _ok(method: str):
        def check(body: dict[str, Any]) -> None:
            if not body.get("ok"):
                raise SlackError(str(body.get("error") or "unknown_error"), method)
        return check

    def _call(self, method: str, **params: Any) -> dict[str, Any]:
        body = self.client.get_json(f"/{method}", params)
        self._ok(method)(body)
        return body

    def _pages(self, method: str, key: str, **params: Any) -> Iterator[dict[str, Any]]:
        return self.client.paginate_cursor(
            f"/{method}", {"limit": PAGE, **params}, key=key, cursor_param="cursor",
            next_cursor=lambda body: (body.get("response_metadata") or {}).get("next_cursor"),
            check=self._ok(method))

    # -- scope ---------------------------------------------------------------- #

    def enumerate(self) -> Iterator[RawContainer]:
        who = self._call("auth.test")
        self.team_id = who["team_id"]
        self.workspace_url = who["url"].rstrip("/")
        self.directory.load(self._pages("users.list", "members"))
        channels = [ch for ch in self._pages("conversations.list", "channels",
                                             types="public_channel,private_channel", exclude_archived="false")
                    if ch.get("is_member")]
        briefs = [channel_brief(ch, is_private=bool(ch.get("is_private"))) for ch in channels]
        for brief in sorted((b for b in briefs if self._in_scope(b)), key=lambda b: b["name"].lower()):
            yield self._container(brief)

    # -- fetch ---------------------------------------------------------------- #

    def _window_start(self, cursor: dict[str, Any] | None) -> datetime:
        start = ts_time(cursor["ts"]) - LOOKBACK if cursor else self.now - self.backfill
        return start.replace(hour=0, minute=0, second=0, microsecond=0)       # whole days: stable bursts

    def fetch(self, container: RawContainer, since: Checkpoint | None) -> Iterator[RawItem]:
        channel_id = container.meta["channel"]["id"]
        cursor = json.loads(since.cursor) if since else None
        oldest = time_ts(self._window_start(cursor))
        history = list(self._pages("conversations.history", "messages", channel=channel_id,
                                   oldest=oldest, inclusive="true"))
        threads = {m["ts"] for m in history if (m.get("reply_count") or 0) > 0}
        # A broadcast reply whose thread started before the window.
        threads |= {m["thread_ts"] for m in history if m.get("thread_ts") and m["thread_ts"] != m["ts"]}
        horizon = self.now - self.active_window
        threads |= {ts for ts, latest in ((cursor or {}).get("active") or {}).items() if ts_time(latest) >= horizon}
        messages = list(history)
        for thread_ts in sorted(threads, key=ts_key):
            messages.extend(self._replies(channel_id, thread_ts))
        yield from self._items(container, messages, cursor)

    def _replies(self, channel_id: str, thread_ts: str) -> list[dict[str, Any]]:
        try:
            return list(self._pages("conversations.replies", "messages", channel=channel_id, ts=thread_ts))
        except SlackError as e:
            if e.error == "thread_not_found":          # the whole thread was deleted
                return []
            raise

    # -- reconciliation, permissions -------------------------------------------- #

    def list_ids(self, container: RawContainer) -> Iterator[str]:
        channel_id = container.meta["channel"]["id"]
        yield from self._ids(container, list(self._pages("conversations.history", "messages", channel=channel_id)))

    def _members(self, channel_id: str) -> list[str]:
        return list(self._pages("conversations.members", "members", channel=channel_id))
