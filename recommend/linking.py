"""
linking.py - which items point at which: the swarm's refs, plus document links it does not resolve.

Two uses. An ask and the ticket filed from it, or a Jira duplicate, are one ask, not two. And a
runbook linked from a pattern's instances is where its steps come from.

"Same ask" is deliberately narrow, because ordinary references chain: epic children cite each other,
a status thread names five tickets. Only these join two items into one ask:

  - a ticket that links exactly one Slack thread (the permalink in its description: filed from it)
  - a Slack thread that names exactly one ticket ("filed OPS-31")
    (either way, only when the ticket is no older than the thread: an ask comes before its ticket)
  - a Jira Duplicate link

and an ask never holds two Slack threads.

The swarm resolves Jira keys, GitHub PR/issue/commit links and Drive links into refs. Two kinds of link
it leaves unresolved are resolved here, against the items this run can see:

  - Slack permalinks outside Slack (a Jira description's "Asked in Slack: https://.../p1792..."): the
    swarm keeps them in meta.slack_links, because a link to a top-level message may land inside a
    burst whose id it cannot know. Here every Slack item is known: a link names its thread (by
    thread_ts, or by the message's own ts when it started a thread), else the burst in that channel
    whose messages span the link's ts.
  - GitHub file links (".../handbook/blob/main/access-policy.md"), matched to documents by repo and
    path; Drive links by file id.
"""

from __future__ import annotations

import re

from rec_weights import parse_time, when

_DRIVE_ID = re.compile(r"(?:docs\.google\.com/(?:document|presentation|spreadsheets)/d/|drive\.google\.com/file/d/)"
                       r"([A-Za-z0-9_-]{10,})")
_SLACK = re.compile(r"https://[A-Za-z0-9-]+\.slack\.com/archives/([A-Z0-9]+)/p(\d{10})(\d{6})(?:\?[^\s)>\]|]*)?")
_THREAD_TS = re.compile(r"[?&]thread_ts=(\d+\.\d+)")
_GITHUB_FILE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/(?:blob|tree)/[^/\s)>\]|]+/([^\s)>\]|#?]+)")


def _num(value: str) -> bool:
    try:
        float(value)
    except ValueError:
        return False
    return True


def _doc_key(url: str) -> str | None:
    if m := _DRIVE_ID.search(url or ""):
        return f"drive:{m.group(1)}"
    if m := _GITHUB_FILE.search(url or ""):
        return f"github:{m.group(1).lower()}/{m.group(2)}"
    return None


class Links:
    def __init__(self, items: dict[str, dict]) -> None:
        self.items = items
        self.docs = {k: i["id"] for i in items.values() if i.get("kind") == "document" and (k := _doc_key(i.get("url")))}
        self.out: dict[str, set[str]] = {}
        self.duplicates: dict[str, set[str]] = {}
        self.threads: dict[tuple[str, str], str] = {}                # (channel, anchor ts) -> item id
        self.bursts: dict[str, list[tuple[float, float, str]]] = {}  # channel -> (first ts, last ts, id)
        for i in items.values():
            if i.get("source") == "slack" and i.get("kind") == "thread" and ":thread:" in i["id"]:
                channel, ts = i["id"].rsplit(":", 2)[-2:]
                self.threads[(channel, ts)] = i["id"]
                if (i.get("meta") or {}).get("grouping") == "burst":
                    last = str(i.get("version") or ts)
                    self.bursts.setdefault(channel, []).append((float(ts), float(last) if _num(last) else float(ts), i["id"]))
        for i in items.values():
            targets = set()
            for r in i.get("refs") or []:
                if r.get("target") in items and r["target"] != i["id"]:
                    targets.add(r["target"])
                    if r.get("type") == "duplicates":
                        self.duplicates.setdefault(i["id"], set()).add(r["target"])
            for m in _DRIVE_ID.finditer(i.get("text") or ""):
                targets.add(self.docs.get(f"drive:{m.group(1)}", ""))
            for m in _GITHUB_FILE.finditer(i.get("text") or ""):
                targets.add(self.docs.get(f"github:{m.group(1).lower()}/{m.group(2)}", ""))
            if i.get("source") != "slack":       # Slack's own links the swarm already resolved
                permalinks = set((i.get("meta") or {}).get("slack_links") or [])
                permalinks.update(m.group(0) for m in _SLACK.finditer(i.get("text") or ""))
                targets.update(t for link in permalinks if (t := self.slack_target(link)))
            targets -= {"", i["id"]}
            if targets:
                self.out[i["id"]] = targets

    def slack_target(self, link: str) -> str | None:
        m = _SLACK.search(link)
        if not m:
            return None
        channel, ts = m.group(1), f"{m.group(2)}.{m.group(3)}"
        anchor = (t.group(1) if (t := _THREAD_TS.search(link)) else None) or ts
        if hit := self.threads.get((channel, anchor)):
            return hit
        at = float(ts)
        return next((bid for first, last, bid in self.bursts.get(channel, []) if first <= at <= last), None)

    def kind(self, item_id: str) -> str:
        return self.items[item_id].get("kind", "")

    def _after(self, ticket: str, thread: str) -> bool:
        """Real times, not strings: Jira writes local time with an offset, Slack writes UTC."""
        a, b = parse_time(self.items[ticket].get("created_at")), parse_time(self.items[thread].get("created_at"))
        return a is not None and b is not None and a >= b

    def same_ask(self, ids: set[str]) -> list[tuple[str, str]]:
        """The pairs among `ids` that are one ask (see the module docstring)."""
        pairs = []
        for a in ids:
            out = self.out.get(a, set()) & ids
            threads = {t for t in out if self.kind(t) == "thread"}
            tickets = {t for t in out if self.kind(t) == "issue"}
            if self.kind(a) == "issue" and len(threads) == 1 and self._after(a, t := next(iter(threads))):
                pairs.append((a, t))
            if self.kind(a) == "thread" and len(tickets) == 1 and self._after(t := next(iter(tickets)), a):
                pairs.append((a, t))
            pairs.extend((a, d) for d in self.duplicates.get(a, set()) & ids)
        return pairs

    def documents(self, item_id: str) -> set[str]:
        return {t for t in self.out.get(item_id, ()) if self.kind(t) == "document"}


def asks(items: list[dict], links: Links) -> list[list[dict]]:
    """Items that are one ask, grouped: union-find over same_ask pairs, refusing any join that would put
    two Slack threads in one ask. Each ask is oldest first, and asks are in order of their first item."""
    parent = {i["id"]: i["id"] for i in items}
    threads = {i["id"]: int(i.get("kind") == "thread") for i in items}

    def root(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in sorted(links.same_ask(set(parent))):
        ra, rb = root(a), root(b)
        if ra != rb and threads[ra] + threads[rb] <= 1:
            parent[ra] = rb
            threads[rb] += threads[ra]
    groups: dict[str, list[dict]] = {}
    for i in sorted(items, key=when):
        groups.setdefault(root(i["id"]), []).append(i)
    return sorted(groups.values(), key=lambda g: when(g[0]))
