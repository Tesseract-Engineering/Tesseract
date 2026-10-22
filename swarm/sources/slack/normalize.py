"""
normalize.py - Slack messages to conversations, and conversations to items.

One grouping serves both connectors (export and API), so a conversation gets
the same id and the same content whichever way it was fetched:

  thread   a parent message and its replies, id slack:{team}:thread:{C}:{thread_ts}
  burst    back-to-back top-level messages with no thread, at most BURST_GAP
           apart and never across midnight UTC, id ...:thread:{C}:{first ts}

Splitting bursts at midnight UTC makes them a function of whole days, so a
run that starts its window at a day boundary rebuilds exactly the bursts an
earlier run (or the export) built. A top-level message becomes a thread
parent once it has replies, and leaves its burst; if it was the burst's first
message, the burst's id moves to the next message and weekly reconciliation
retires the old id.

Join, leave, topic and similar system messages are dropped. Bot messages are
kept as text, and bots go in meta.bots, never on the people list.

Refs: channel mentions (<#C1>), permalinks to threads in this workspace, and
the cross-source links in swarm/sources/links.py. A permalink to a single
message stays in meta.slack_links, since that message may sit in a burst
whose id starts elsewhere.

Payloads arrive already redacted (swarm/run.py), so the redactions list is
read back from the markers left in the text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html import unescape
from typing import Any
from urllib.parse import parse_qs, urlparse

from ... import ids
from ...interfaces import RawItem
from ...records import Acl, ItemRecord, Person, content_hash
from ...stages.redact import redactions_in
from ..links import GitHubLinks, scan
from .directory import Directory

BURST_GAP = timedelta(minutes=20)
MAX_TEXT = 200_000
TITLE_LEN = 120
# Messages that carry what people said. Everything else (channel_join,
# channel_topic, pinned_item, tombstone, ...) is a system event.
KEEP_SUBTYPES = {None, "bot_message", "thread_broadcast", "file_share", "me_message"}
# The message fields both the export and the API carry, and all the
# normalizer reads. Raw payloads keep only these, so the two match.
MESSAGE_FIELDS = ("ts", "thread_ts", "reply_count", "user", "bot_id", "username", "subtype", "text",
                  "edited", "files", "bot_profile")

_TOKEN = re.compile(r"<([^<>]+)>")


def ts_key(ts: str) -> tuple[int, int]:
    """Sort key for a Slack ts ("1760450400.000100"), exact where float is not."""
    sec, _, frac = ts.partition(".")
    return int(sec), int((frac or "0").ljust(6, "0")[:6])


def ts_time(ts: str) -> datetime:
    sec, micro = ts_key(ts)
    return datetime.fromtimestamp(sec, tz=timezone.utc).replace(microsecond=micro)


def time_ts(when: datetime) -> str:
    return f"{int(when.timestamp())}.000000"


def permalink(workspace_url: str, channel_id: str, ts: str) -> str:
    sec, micro = ts_key(ts)
    return f"{workspace_url}/archives/{channel_id}/p{sec}{micro:06d}"


def clean_message(m: dict[str, Any]) -> dict[str, Any]:
    """The fields of one message the normalizer reads, and nothing else."""
    out = {k: m[k] for k in MESSAGE_FIELDS if m.get(k) is not None}
    if "files" in out:
        out["files"] = [{"name": f.get("name"), "title": f.get("title")} for f in out["files"]]
    if "edited" in out:
        out["edited"] = {"ts": out["edited"].get("ts")}
    if "bot_profile" in out:
        out["bot_profile"] = {"name": out["bot_profile"].get("name")}
    return out


# --------------------------------------------------------------------------- #
# grouping
# --------------------------------------------------------------------------- #

@dataclass
class Conversation:
    grouping: str                      # "thread" | "burst"
    anchor: str                        # thread_ts, or the burst's first ts
    messages: list[dict[str, Any]]

    @property
    def latest(self) -> str:
        return max((m["ts"] for m in self.messages), key=ts_key)


def _is_reply(m: dict[str, Any]) -> bool:
    return bool(m.get("thread_ts")) and m["thread_ts"] != m["ts"]


def group_conversations(messages: list[dict[str, Any]]) -> list[Conversation]:
    by_ts: dict[str, dict[str, Any]] = {}
    for m in messages:
        if m.get("subtype") in KEEP_SUBTYPES and m.get("ts"):
            by_ts.setdefault(m["ts"], m)       # history and replies both return broadcasts
    threads: dict[str, list[dict[str, Any]]] = {}
    for m in by_ts.values():
        if _is_reply(m):
            threads.setdefault(m["thread_ts"], []).append(m)
    top = []
    for ts, m in by_ts.items():
        if _is_reply(m):
            continue
        if ts in threads or (m.get("reply_count") or 0) > 0:
            threads.setdefault(ts, []).append(m)
        else:
            top.append(m)

    out = [Conversation("thread", ts, sorted(msgs, key=lambda m: ts_key(m["ts"])))
           for ts, msgs in threads.items()]
    burst: list[dict[str, Any]] = []
    for m in sorted(top, key=lambda m: ts_key(m["ts"])):
        if burst:
            last, now = ts_time(burst[-1]["ts"]), ts_time(m["ts"])
            if now - last > BURST_GAP or now.date() != last.date():
                out.append(Conversation("burst", burst[0]["ts"], burst))
                burst = []
        burst.append(m)
    if burst:
        out.append(Conversation("burst", burst[0]["ts"], burst))
    return sorted(out, key=lambda c: ts_key(c.anchor))


# --------------------------------------------------------------------------- #
# items
# --------------------------------------------------------------------------- #

@dataclass
class _Seen:
    """What one item's text names: people by role, bots, channels."""
    roles: dict[str, list[str]] = field(default_factory=dict)
    bots: set[str] = field(default_factory=set)
    channels: list[str] = field(default_factory=list)

    def add(self, user_id: str, role: str) -> None:
        roles = self.roles.setdefault(user_id, [])
        if role not in roles:
            roles.append(role)


class SlackNormalizer:
    def __init__(self, directory: Directory, github: GitHubLinks | None = None):
        self.directory = directory
        self.github = github or GitHubLinks()
        self.run_id = ""

    def to_item(self, raw: RawItem, acl: Acl) -> ItemRecord:
        p = raw.payload
        seen = _Seen()
        fields = self._channel(p, seen) if p["type"] == "channel" else self._conversation(p, seen)
        return self._finish(fields, seen, p, acl, raw.fetched_at)

    # -- channel ------------------------------------------------------------ #

    def _channel(self, p: dict[str, Any], seen: _Seen) -> dict[str, Any]:
        ws, ch = p["workspace_url"], p["channel"]
        if ch.get("creator"):
            self._person(seen, ch["creator"], "owner")
        text = "\n\n".join(x for x in [self._render(ch.get("purpose") or "", seen),
                                         self._render(ch.get("topic") or "", seen)] if x)
        changed = max(ch.get("created") or 0, ch.get("topic_set") or 0, ch.get("purpose_set") or 0)
        return dict(
            id=ids.slack_channel(p["team_id"], ch["id"]), kind="channel", container=None,
            aliases=[f"#{ch['name']}"], title=f"#{ch['name']}", text=text, url=f"{ws}/archives/{ch['id']}",
            created_at=datetime.fromtimestamp(ch.get("created") or 0, tz=timezone.utc),
            updated_at=datetime.fromtimestamp(changed, tz=timezone.utc), version=str(changed),
            meta={"is_private": bool(ch.get("is_private")), "is_archived": bool(ch.get("is_archived"))})

    # -- thread or burst ---------------------------------------------------- #

    def _conversation(self, p: dict[str, Any], seen: _Seen) -> dict[str, Any]:
        team, ws, ch, messages = p["team_id"], p["workspace_url"], p["channel"], p["messages"]
        lines, first_body = [], ""
        for i, m in enumerate(messages):
            speaker = self._speaker(m, seen, "author" if i == 0 else "participant")
            body = self._render(m.get("text") or "", seen)
            files = " ".join(f"[file: {f.get('title') or f.get('name')}]" for f in m.get("files") or [])
            body = " ".join(x for x in [body, files] if x)
            first_body = first_body if i else body
            lines.append(f"{speaker}: {body}")
        latest = max((x for m in messages for x in (m["ts"], (m.get("edited") or {}).get("ts")) if x), key=ts_key)
        url = permalink(ws, ch["id"], p["anchor"])
        meta: dict[str, Any] = {"grouping": p["grouping"], "message_count": len(messages)}
        if p["grouping"] == "thread":
            meta["reply_count"] = sum(1 for m in messages if m["ts"] != p["anchor"])
        return dict(
            id=ids.slack_thread(team, ch["id"], p["anchor"]), kind="thread",
            container=ids.slack_channel(team, ch["id"]), aliases=[url],
            title=self._title(first_body, ch["name"], messages[0]["ts"]), text="\n".join(lines), url=url,
            created_at=ts_time(messages[0]["ts"]), updated_at=ts_time(latest), version=latest, meta=meta)

    def _speaker(self, m: dict[str, Any], seen: _Seen, role: str) -> str:
        user = self.directory.get(m.get("user"))
        bot = (m.get("subtype") == "bot_message" or (m.get("bot_id") and not m.get("user"))
               or (user is not None and user.is_bot))
        if bot:
            name = (m.get("username") or (m.get("bot_profile") or {}).get("name")
                    or (user.name if user else None) or "bot")
            seen.bots.add(name)
            return name
        self._person(seen, m["user"], role)
        return (user.name if user else None) or m["user"]

    def _person(self, seen: _Seen, user_id: str, role: str) -> None:
        user = self.directory.get(user_id)
        if user and user.is_bot:
            seen.bots.add(user.name or user_id)
        else:
            seen.add(user_id, role)

    @staticmethod
    def _title(first: str, channel: str, ts: str) -> str:
        line = next((x.strip() for x in first.splitlines() if x.strip()), "")
        if not line:
            return f"#{channel} {ts_time(ts).date().isoformat()}"
        if len(line) <= TITLE_LEN:
            return line
        return line[:TITLE_LEN].rsplit(" ", 1)[0] + "…"

    def _render(self, text: str, seen: _Seen) -> str:
        """Slack mrkdwn to plain text: <@U1> -> @Name (and a mention),
        <#C1|name> -> #name, <url|label> -> label (url), &amp; -> &."""
        def token(m: re.Match[str]) -> str:
            target, _, label = m.group(1).partition("|")
            if target.startswith("@"):
                uid = target[1:]
                self._person(seen, uid, "mentioned")
                return "@" + (self.directory.name(uid) or label or uid)
            if target.startswith("#"):
                if target[1:] not in seen.channels:
                    seen.channels.append(target[1:])
                return "#" + (label or target[1:])
            if target.startswith(("!subteam^", "!date^")):
                return label
            if target.startswith("!"):
                return "@" + (label or target[1:]).lstrip("@")
            return f"{label} ({target})" if label and label != target else target
        return unescape(_TOKEN.sub(token, text))

    # -- shared ------------------------------------------------------------- #

    def _finish(self, f: dict[str, Any], seen: _Seen, p: dict[str, Any], acl: Acl, fetched_at: str) -> ItemRecord:
        team, ws, own_channel = p["team_id"], p["workspace_url"], p["channel"]["id"]
        links = scan(f["text"], self.github)
        for channel_id in seen.channels:
            if channel_id != own_channel:
                links.add(ids.slack_channel(team, channel_id), "mention")
        unresolved = []
        for link in links.slack_links:
            target = _thread_link(link, ws, team)
            if target and target != f["id"]:
                links.add(target, "url")
            elif not target:
                unresolved.append(link)
        links.slack_links = unresolved

        meta = f["meta"]
        meta.update(links.meta())
        if seen.bots:
            meta["bots"] = sorted(seen.bots)
        text = f["text"]
        if len(text) > MAX_TEXT:
            text, meta["truncated"] = text[:MAX_TEXT], True
        f["text"] = text
        f["people"] = [self._build_person(uid, roles) for uid, roles in seen.roles.items()]
        f["refs"] = links.refs
        f["content_hash"] = content_hash(title=f["title"], text=text, container=f["container"],
                                         people=f["people"], refs=f["refs"], meta=meta)
        return ItemRecord(source="slack", acl=acl, fetched_at=fetched_at, run_id=self.run_id,
                          redactions=redactions_in({"title": f["title"], "text": text}), **f)

    def _build_person(self, user_id: str, roles: list[str]) -> Person:
        user = self.directory.get(user_id)
        return Person(id=ids.person("slack", user_id), source="slack", handle=user_id,
                      name=user.name if user else None, email=user.email if user else None, roles=roles)


def _thread_link(link: str, workspace_url: str, team: str) -> str | None:
    """A permalink into this workspace names a thread when it carries
    thread_ts, as a reply's link does."""
    if not link.startswith(workspace_url + "/"):
        return None
    parsed = urlparse(link)
    thread_ts = (parse_qs(parsed.query).get("thread_ts") or [None])[0]
    if thread_ts and re.fullmatch(r"\d+\.\d+", thread_ts):
        return ids.slack_thread(team, parsed.path.split("/")[2], thread_ts)
    return None
