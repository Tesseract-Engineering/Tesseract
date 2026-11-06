"""
threads.py - working context that follows a person from a call to a doc to Slack to a ticket.

A thread is owned by one person and read only by its audience (the owner, or the people on the
call). Bots append entries; none rewrites another's. Threads are never written into the graph: a
build replaces its namespace, and half-made work is not company knowledge until it is published
somewhere the swarm reads.

    threads     id, title, owner, audience, open | closed
    entries     transcript | decision | question | answer | note | link | action, by surface.
                A decision or question starts as `proposed` and counts only once the owner accepts it.
    active      the thread each person is working in: Slack and Docs read it as context
    approvals   an action with its exact payload and a hash over it. The thread's owner approves that
                hash; the surface then performs the action and reports it done, which adds an
                `action` entry with the hash.

Threads close when the owner says so or after 14 days without an entry, and are deleted 30 days
after closing. SQLite, under data/ (git-ignored).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

KINDS = ("transcript", "decision", "question", "answer", "note", "link", "action")
PROPOSED_KINDS = ("decision", "question")
SURFACES = ("zoom", "docs", "slack", "tickets")
IDLE_DAYS = 14
KEEP_DAYS = 30
CONTEXT_CHARS = 4000

SCHEMA = """
CREATE TABLE IF NOT EXISTS threads (
  id TEXT PRIMARY KEY, title TEXT NOT NULL, owner TEXT NOT NULL, audience TEXT NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT
);
CREATE TABLE IF NOT EXISTS entries (
  id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, at TEXT NOT NULL, surface TEXT NOT NULL,
  kind TEXT NOT NULL, by TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS entries_thread ON entries (thread_id, at);
CREATE TABLE IF NOT EXISTS active (
  email TEXT PRIMARY KEY, thread_id TEXT NOT NULL, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY, thread_id TEXT, by TEXT NOT NULL, surface TEXT NOT NULL, action TEXT NOT NULL,
  payload TEXT NOT NULL, hash TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
  approved_by TEXT, approved_at TEXT, result TEXT
);
"""


class Denied(Exception):
    """The caller may not do this: not in the audience, not the owner, or a stale hash."""


def now() -> datetime:
    return datetime.now(timezone.utc)


def approval_hash(action: str, payload: dict, thread_id: str | None) -> str:
    """Short hash over exactly what would be done. Shown wherever someone approves it."""
    body = json.dumps({"action": action, "payload": payload, "thread": thread_id}, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:6]


def _email(e: str) -> str:
    return (e or "").strip().lower()


class Threads:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        self.lock = threading.Lock()

    # -- threads ------------------------------------------------------------------------------- #

    def create(self, owner: str, title: str, audience: list[str] | None = None) -> dict:
        owner = _email(owner)
        people = [owner] + [p for p in dict.fromkeys(_email(a) for a in audience or []) if p and p != owner]
        tid = "thr_" + secrets.token_hex(3)
        at = now().isoformat()
        self.db.execute("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?)",
                        (tid, title.strip()[:200] or "Untitled", owner, json.dumps(people), "open", at, at, None))
        return self.get(tid, owner)

    def _row(self, tid: str) -> dict | None:
        self.sweep()
        row = self.db.execute("SELECT * FROM threads WHERE id = ?", (tid,)).fetchone()
        if row is None:
            return None
        t = dict(row)
        t["audience"] = json.loads(t["audience"])
        return t

    def get(self, tid: str, caller: str, proposed: bool = True) -> dict:
        """The thread with its entries, if the caller is in its audience."""
        t = self._row(tid)
        if t is None or _email(caller) not in t["audience"]:
            raise Denied("No such thread, or you are not in its audience.")
        rows = self.db.execute("SELECT * FROM entries WHERE thread_id = ? ORDER BY at, rowid", (tid,)).fetchall()
        t["entries"] = [self._entry(r) for r in rows if proposed or r["status"] == "kept"]
        return t

    @staticmethod
    def _entry(r: sqlite3.Row) -> dict:
        return {"id": r["id"], "at": r["at"], "surface": r["surface"], "kind": r["kind"], "by": r["by"],
                "status": r["status"], **json.loads(r["data"])}

    def mine(self, person: str, owned_only: bool = False) -> list[dict]:
        """Open threads a person owns (or, by default, is in the audience of), newest first."""
        self.sweep()
        person = _email(person)
        rows = self.db.execute("SELECT * FROM threads WHERE status = 'open' ORDER BY updated_at DESC").fetchall()
        out = []
        for r in rows:
            people = json.loads(r["audience"])
            if (r["owner"] == person) if owned_only else (person in people):
                out.append({"id": r["id"], "title": r["title"], "owner": r["owner"], "audience": people,
                            "status": r["status"], "created_at": r["created_at"], "updated_at": r["updated_at"]})
        return out

    def close(self, tid: str, by: str) -> dict:
        t = self._row(tid)
        if t is None or t["owner"] != _email(by):
            raise Denied("Only the thread's owner can close it.")
        self.db.execute("UPDATE threads SET status = 'closed', closed_at = ? WHERE id = ? AND status = 'open'",
                        (now().isoformat(), tid))
        self.db.execute("DELETE FROM active WHERE thread_id = ?", (tid,))
        return self.get(tid, by)

    def sweep(self) -> None:
        """Close threads idle for 14 days; delete those closed for 30."""
        idle = (now() - timedelta(days=IDLE_DAYS)).isoformat()
        gone = (now() - timedelta(days=KEEP_DAYS)).isoformat()
        self.db.execute("UPDATE threads SET status = 'closed', closed_at = ? WHERE status = 'open' AND updated_at < ?",
                        (now().isoformat(), idle))
        old = [r["id"] for r in self.db.execute("SELECT id FROM threads WHERE status = 'closed' AND closed_at < ?",
                                                (gone,)).fetchall()]
        for tid in old:
            for table, col in (("entries", "thread_id"), ("active", "thread_id"), ("approvals", "thread_id"),
                               ("threads", "id")):
                self.db.execute(f"DELETE FROM {table} WHERE {col} = ?", (tid,))

    # -- entries ------------------------------------------------------------------------------- #

    def append(self, tid: str, by: str, surface: str, kind: str, **data) -> dict:
        """Add what happened on a surface. A decision or question a bot proposes waits for the
        owner; the owner's own is kept at once."""
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        if surface not in SURFACES:
            raise ValueError(f"surface must be one of {', '.join(SURFACES)}")
        t = self._row(tid)
        by = _email(by)
        if t is None or by not in t["audience"]:
            raise Denied("No such thread, or you are not in its audience.")
        if t["status"] != "open":
            raise Denied("This thread is closed.")
        proposed = kind in PROPOSED_KINDS and (data.pop("proposed", True) or by != t["owner"])
        eid = "ent_" + secrets.token_hex(4)
        at = now().isoformat()
        clean = {k: v for k, v in data.items() if v is not None}
        self.db.execute("INSERT INTO entries VALUES (?,?,?,?,?,?,?,?)",
                        (eid, tid, at, surface, kind, by, "proposed" if proposed else "kept", json.dumps(clean)))
        self.db.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (at, tid))
        return {"id": eid, "at": at, "surface": surface, "kind": kind, "by": by,
                "status": "proposed" if proposed else "kept", **clean}

    def decide(self, tid: str, eid: str, by: str, keep: bool) -> dict:
        """The owner accepts or dismisses a proposed decision or question."""
        t = self._row(tid)
        if t is None or t["owner"] != _email(by):
            raise Denied("Only the thread's owner can accept or dismiss what Tesseract proposes.")
        n = self.db.execute("UPDATE entries SET status = ? WHERE id = ? AND thread_id = ? AND status = 'proposed'",
                            ("kept" if keep else "dismissed", eid, tid)).rowcount
        if not n:
            raise Denied("That entry is not waiting for a decision.")
        self.db.execute("UPDATE threads SET updated_at = ? WHERE id = ?", (now().isoformat(), tid))
        row = self.db.execute("SELECT * FROM entries WHERE id = ?", (eid,)).fetchone()
        return self._entry(row)

    # -- the thread each person is working in ------------------------------------------------- #

    def activate(self, tid: str, person: str) -> dict:
        t = self.get(tid, person)
        if t["status"] != "open":
            raise Denied("This thread is closed.")
        self.db.execute("INSERT OR REPLACE INTO active VALUES (?,?,?)", (_email(person), tid, now().isoformat()))
        return t

    def active(self, person: str) -> dict | None:
        """The thread a person picked last, while it is open; else the one they own that moved in the
        last day (closing the thread you were in should not leave you in none)."""
        row = self.db.execute("SELECT thread_id FROM active WHERE email = ?", (_email(person),)).fetchone()
        if row is not None:
            try:
                t = self.get(row["thread_id"], person)
                if t["status"] == "open":
                    return t
            except Denied:
                pass
        recent = (now() - timedelta(days=1)).isoformat()
        owned = [t for t in self.mine(person, owned_only=True) if t["updated_at"] >= recent]
        return self.get(owned[0]["id"], person) if owned else None

    # -- approvals ----------------------------------------------------------------------------- #

    def propose(self, by: str, surface: str, action: str, payload: dict, thread_id: str | None = None) -> dict:
        by = _email(by)
        if thread_id:
            t = self._row(thread_id)
            if t is None or by not in t["audience"]:
                raise Denied("No such thread, or you are not in its audience.")
        aid = "apr_" + secrets.token_hex(4)
        h = approval_hash(action, payload, thread_id)
        self.db.execute("INSERT INTO approvals (id, thread_id, by, surface, action, payload, hash, status, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?)",
                        (aid, thread_id, by, surface, action, json.dumps(payload), h, "proposed", now().isoformat()))
        return self.approval(aid)

    def approval(self, aid: str) -> dict | None:
        row = self.db.execute("SELECT * FROM approvals WHERE id = ?", (aid,)).fetchone()
        if row is None:
            return None
        a = dict(row)
        a["payload"] = json.loads(a["payload"])
        a["result"] = json.loads(a["result"]) if a["result"] else None
        return a

    def approvals(self, surface: str | None = None, status: str | None = None) -> list[dict]:
        rows = self.db.execute("SELECT id FROM approvals WHERE (? IS NULL OR surface = ?) AND (? IS NULL OR status = ?) "
                               "ORDER BY created_at", (surface, surface, status, status)).fetchall()
        return [self.approval(r["id"]) for r in rows]

    def approve(self, aid: str, by: str, h: str) -> dict:
        """Approve exactly the payload this hash was shown for. In a thread only its owner may;
        outside one, only whoever proposed it."""
        a = self.approval(aid)
        by = _email(by)
        if a is None:
            raise Denied("No such approval.")
        t = self._row(a["thread_id"]) if a["thread_id"] else None
        allowed = t["owner"] if t else a["by"]
        if by != allowed:
            raise Denied("Only the thread's owner can approve this." if t else "Only whoever proposed this can approve it.")
        n = self.db.execute("UPDATE approvals SET status = 'approved', approved_by = ?, approved_at = ? "
                            "WHERE id = ? AND status = 'proposed' AND hash = ?",
                            (by, now().isoformat(), aid, h)).rowcount
        if not n:
            raise Denied("This approval is out of date or already decided. Review the latest version.")
        return self.approval(aid)

    def cancel(self, aid: str, by: str) -> dict:
        a = self.approval(aid)
        if a is None or _email(by) not in (a["by"], a["approved_by"]):
            raise Denied("No such approval, or not yours to cancel.")
        self.db.execute("UPDATE approvals SET status = 'cancelled' WHERE id = ? AND status IN ('proposed', 'approved')",
                        (aid,))
        return self.approval(aid)

    def done(self, aid: str, surface: str, result: dict, ok: bool = True) -> dict:
        """The surface performed an approved action (or failed to). Logged on the thread with its hash."""
        with self.lock:
            a = self.approval(aid)
            if a is None or a["surface"] != surface:
                raise Denied("No such approval for this surface.")
            if a["status"] != "approved":
                raise Denied("Only an approved action can be done, once.")
            self.db.execute("UPDATE approvals SET status = ?, result = ? WHERE id = ?",
                            ("done" if ok else "failed", json.dumps(result), aid))
        if ok and a["thread_id"]:
            t = self._row(a["thread_id"])
            if t and t["status"] == "open":
                self.append(a["thread_id"], a["approved_by"], surface, "action", approval=a["hash"],
                            action=a["action"], text=result.get("text"), url=result.get("url"))
        return self.approval(aid)


def context_text(t: dict) -> str | None:
    """A thread as context for a question: what it is about, what was decided and asked, the last
    of the call, and where the work went. Never a source: an answer may not quote it."""
    kept = [e for e in t.get("entries", []) if e["status"] == "kept"]
    if not kept:
        return f"Working on: {t['title']}"
    lines = [f"Working on: {t['title']}"]
    for kind, head in (("decision", "Decided"), ("question", "Open question")):
        lines += [f"{head}: {e.get('text', '')}" for e in kept if e["kind"] == kind]
    for e in kept:
        if e["kind"] == "link":
            lines.append(f"Doc: {e.get('title') or ''} {e.get('url') or ''}".rstrip())
        elif e["kind"] == "note" and e.get("text"):
            lines.append(f"Note: {e['text']}")
        elif e["kind"] == "answer":
            lines.append(f"Asked: {e.get('question', '')}" + (f" (answered from {', '.join(e['titles'][:3])})"
                                                              if e.get("titles") else ""))
    said = [e.get("text", "") for e in kept if e["kind"] == "transcript"]
    if said:
        lines.append("From the call:\n" + "\n".join(said[-3:]))
    return "\n".join(lines)[-CONTEXT_CHARS:]
