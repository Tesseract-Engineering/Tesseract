"""
ledger.py - what the bot has been asked to do, approved, and done. SQLite, under data/ (git-ignored).

    requests   one access request: who asks whom, for which items, with what message. Its `hash` is
               over exactly that payload; the requester approves a hash, and any edit makes a new one
               that needs approving again.
    grants     items a Drive owner shared with a person through the bot, until they expire. The graph
               learns of the change on the next swarm run; until then this is what vouches for it.
    google     each owner's Google refresh token, so Grant runs the Drive change as them.
    events     every step, with the hash it acted on.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  id TEXT PRIMARY KEY, hash TEXT NOT NULL, status TEXT NOT NULL,
  requester_slack TEXT NOT NULL, requester_email TEXT NOT NULL, requester_name TEXT,
  owner_slack TEXT, owner_email TEXT, owner_name TEXT,
  item_ids TEXT NOT NULL, sources TEXT NOT NULL, message TEXT NOT NULL, days INTEGER NOT NULL,
  question TEXT, dm_channel TEXT, dm_ts TEXT, owner_channel TEXT, owner_ts TEXT,
  created_at TEXT NOT NULL, decided_at TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS grants (
  email TEXT NOT NULL, item_id TEXT NOT NULL, expires_at TEXT NOT NULL, request_id TEXT NOT NULL,
  PRIMARY KEY (email, item_id)
);
CREATE TABLE IF NOT EXISTS google (
  email TEXT PRIMARY KEY, refresh_token TEXT NOT NULL, connected_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS oauth_states (
  state TEXT PRIMARY KEY, email TEXT NOT NULL, request_id TEXT NOT NULL, verifier TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  at TEXT NOT NULL, request_id TEXT, actor TEXT, what TEXT NOT NULL, hash TEXT
);
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def payload_hash(*, requester: str, owner: str, item_ids: list[str], days: int, message: str) -> str:
    """Short hash over exactly what would be sent. Shown on every card that acts on it."""
    body = json.dumps({"from": requester, "to": owner, "items": sorted(item_ids), "scope": "read",
                       "days": days, "message": message}, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()[:6]


@dataclass
class Request:
    id: str
    hash: str
    status: str            # draft | sent | granted | declined | cancelled | failed
    requester_slack: str
    requester_email: str
    requester_name: str | None
    owner_slack: str | None
    owner_email: str | None
    owner_name: str | None
    item_ids: list[str]
    sources: list[str]
    message: str
    days: int
    question: str | None
    dm_channel: str | None
    dm_ts: str | None
    owner_channel: str | None
    owner_ts: str | None
    created_at: str
    decided_at: str | None
    note: str | None


class Ledger:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def log(self, what: str, request_id: str | None = None, actor: str | None = None, hash: str | None = None):
        self.db.execute("INSERT INTO events VALUES (?,?,?,?,?)", (now().isoformat(), request_id, actor, what, hash))

    # -- requests ------------------------------------------------------------------------------ #

    def draft(self, *, requester_slack: str, requester_email: str, requester_name: str | None,
              owner_slack: str | None, owner_email: str | None, owner_name: str | None,
              item_ids: list[str], sources: list[str], message: str, days: int, question: str | None) -> Request:
        rid = secrets.token_hex(4)
        h = payload_hash(requester=requester_email, owner=owner_email or owner_name or "", item_ids=item_ids,
                         days=days, message=message)
        self.db.execute(
            "INSERT INTO requests (id, hash, status, requester_slack, requester_email, requester_name, owner_slack, "
            "owner_email, owner_name, item_ids, sources, message, days, question, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, h, "draft", requester_slack, requester_email, requester_name, owner_slack, owner_email, owner_name,
             json.dumps(item_ids), json.dumps(sources), message, days, question, now().isoformat()))
        self.log("drafted", rid, requester_slack, h)
        return self.get(rid)

    def get(self, rid: str) -> Request | None:
        row = self.db.execute("SELECT * FROM requests WHERE id = ?", (rid,)).fetchone()
        if row is None:
            return None
        d = dict(row)
        d["item_ids"], d["sources"] = json.loads(d["item_ids"]), json.loads(d["sources"])
        return Request(**d)

    def edit(self, rid: str, message: str) -> Request:
        r = self.get(rid)
        h = payload_hash(requester=r.requester_email, owner=r.owner_email or r.owner_name or "",
                         item_ids=r.item_ids, days=r.days, message=message)
        self.db.execute("UPDATE requests SET message = ?, hash = ? WHERE id = ? AND status = 'draft'", (message, h, rid))
        self.log("edited", rid, r.requester_slack, h)
        return self.get(rid)

    def set(self, rid: str, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE requests SET {cols} WHERE id = ?", (*fields.values(), rid))

    def transition(self, rid: str, expected: str, status: str, hash: str | None = None, **fields) -> bool:
        """Move a request from one status to the next, once. With `hash`, only if the payload is still
        the one that hash was shown for: a stale card cannot approve an edited request."""
        cols = ", ".join(["status = ?", *(f"{k} = ?" for k in fields)])
        where = "id = ? AND status = ?" + (" AND hash = ?" if hash else "")
        args = (status, *fields.values(), rid, expected, *((hash,) if hash else ()))
        return self.db.execute(f"UPDATE requests SET {cols} WHERE {where}", args).rowcount == 1

    # -- grants -------------------------------------------------------------------------------- #

    def grant(self, email: str, item_ids: list[str], days: int, request_id: str) -> datetime:
        expires = now() + timedelta(days=days)
        for iid in item_ids:
            self.db.execute("INSERT OR REPLACE INTO grants VALUES (?,?,?,?)",
                            (email.lower(), iid, expires.isoformat(), request_id))
        return expires

    def granted(self, email: str) -> list[str]:
        rows = self.db.execute("SELECT item_id FROM grants WHERE email = ? AND expires_at > ?",
                               (email.lower(), now().isoformat())).fetchall()
        return [r["item_id"] for r in rows]

    # -- google -------------------------------------------------------------------------------- #

    def google_token(self, email: str) -> str | None:
        row = self.db.execute("SELECT refresh_token FROM google WHERE email = ?", (email.lower(),)).fetchone()
        return row["refresh_token"] if row else None

    def save_google(self, email: str, refresh_token: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO google VALUES (?,?,?)", (email.lower(), refresh_token, now().isoformat()))

    def drop_google(self, email: str) -> None:
        self.db.execute("DELETE FROM google WHERE email = ?", (email.lower(),))

    def oauth_state(self, email: str, request_id: str, verifier: str) -> str:
        state = secrets.token_urlsafe(24)
        self.db.execute("INSERT INTO oauth_states VALUES (?,?,?,?,?)",
                        (state, email.lower(), request_id, verifier, now().isoformat()))
        return state

    def take_oauth_state(self, state: str) -> dict | None:
        """Each state works once, within 15 minutes."""
        row = self.db.execute("SELECT * FROM oauth_states WHERE state = ?", (state,)).fetchone()
        self.db.execute("DELETE FROM oauth_states WHERE state = ?", (state,))
        if row is None or datetime.fromisoformat(row["created_at"]) < now() - timedelta(minutes=15):
            return None
        return dict(row)
