"""actions.py - the approval door: nothing Tesseract proposes runs until a person
approves the exact payload that will run.

    pid = propose([{"id": "a1", "type": "email.draft", "payload": {...}}])
    approve(pid, "a1", "jane.doe")      # pins the sha256 of the stored action
    execute(pid, "a1")                  # re-checks the hash, then writes the outbox
    reset()                             # removes everything execute() wrote

State per action: pending -> approved -> executed, or pending -> rejected.
A payload whose hash no longer matches what was proposed is rejected, never run.

Execution writes drafts to the outbox (TESSERACT_OUTBOX) and nothing else. It
never sends a message and never edits a source system; a person sends the
draft. One file per action:
  email.draft   email/<proposal>-<action>.json
  slack.draft   slack/<proposal>-<action>.json
  agent.launch  agent/<proposal>-<action>.json   the approved coding-agent brief
Stdlib only; no Neo4j.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .config import settings

TYPES = {"email.draft", "slack.draft", "agent.launch"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS actions (
    proposal_id TEXT NOT NULL, action_id TEXT NOT NULL, type TEXT NOT NULL,
    action_json TEXT NOT NULL, hash TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
    approver TEXT, approved_at TEXT, reason TEXT,
    executed_at TEXT, result_path TEXT,
    PRIMARY KEY (proposal_id, action_id));
CREATE TABLE IF NOT EXISTS journal (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, proposal_id TEXT, action_id TEXT,
    path TEXT NOT NULL, existed INTEGER NOT NULL, original_b64 TEXT);
"""


class DoorError(Exception):
    """Refused by the door. `code` is stable for the UI: not_found, bad_action,
    hash_mismatch, rejected, not_approved."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def canonical(action: dict) -> str:
    return json.dumps(action, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(action: dict) -> str:
    return hashlib.sha256(canonical(action).encode("utf-8")).hexdigest()


class Door:
    def __init__(self, db_path: Path | str | None = None, outbox: Path | str | None = None):
        s = settings()
        db = Path(db_path) if db_path else s.state_dir / "actions.sqlite"
        db.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(db)
        self.outbox = Path(outbox) if outbox else s.outbox
        with self._conn() as c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _row(self, c, proposal_id: str, action_id: str) -> sqlite3.Row:
        row = c.execute("SELECT * FROM actions WHERE proposal_id=? AND action_id=?",
                        (proposal_id, action_id)).fetchone()
        if row is None:
            raise DoorError("not_found", f"no action {action_id!r} in proposal {proposal_id!r}")
        return row

    # -- propose ----------------------------------------------------------- #

    def propose(self, actions: list[dict]) -> str:
        if not isinstance(actions, list) or not actions:
            raise DoorError("bad_action", "actions must be a non-empty list")
        seen = set()
        for a in actions:
            if not isinstance(a, dict) or a.get("type") not in TYPES:
                raise DoorError("bad_action", f"unknown action type in {a!r}; allowed {sorted(TYPES)}")
            if not a.get("id") or a["id"] in seen:
                raise DoorError("bad_action", f"every action needs a unique id: {a.get('id')!r}")
            if not isinstance(a.get("payload"), dict):
                raise DoorError("bad_action", f"action {a['id']} has no payload object")
            seen.add(a["id"])
        pid = "p_" + uuid.uuid4().hex[:12]
        with self._conn() as c:
            c.execute("INSERT INTO proposals (id, created_at) VALUES (?,?)", (pid, _now()))
            for a in actions:
                c.execute("INSERT INTO actions (proposal_id, action_id, type, action_json, hash) "
                          "VALUES (?,?,?,?,?)", (pid, a["id"], a["type"], canonical(a), digest(a)))
        return pid

    def proposal(self, proposal_id: str) -> dict:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM actions WHERE proposal_id=? ORDER BY action_id",
                             (proposal_id,)).fetchall()
        if not rows:
            raise DoorError("not_found", f"no proposal {proposal_id!r}")
        return {"proposal_id": proposal_id,
                "actions": [{"action_id": r["action_id"], "type": r["type"], "state": r["state"],
                             "hash": r["hash"], "action": json.loads(r["action_json"])} for r in rows]}

    # -- approve / reject -------------------------------------------------- #

    def _verify(self, c, row: sqlite3.Row, payload: dict | None) -> None:
        """The stored action must still hash to what was proposed, and a caller
        that shows what it is approving must show that same action."""
        stored = json.loads(row["action_json"])
        reason = None
        if digest(stored) != row["hash"]:
            reason = "stored action changed after it was proposed"
        elif payload is not None:
            candidate = payload if "payload" in payload else {**stored, "payload": payload}
            if digest(candidate) != row["hash"]:
                reason = "payload differs from the proposed payload"
        if reason:
            c.execute("UPDATE actions SET state='rejected', reason=? WHERE proposal_id=? AND action_id=?",
                      (f"hash mismatch: {reason}", row["proposal_id"], row["action_id"]))
            c.commit()
            raise DoorError("hash_mismatch", reason)

    def approve(self, proposal_id: str, action_id: str, approver: str,
                payload: dict | None = None) -> dict:
        if not approver:
            raise DoorError("bad_action", "approver is required")
        with self._conn() as c:
            row = self._row(c, proposal_id, action_id)
            if row["state"] == "rejected":
                raise DoorError("rejected", row["reason"] or "action was rejected")
            self._verify(c, row, payload)
            if row["state"] == "pending":
                c.execute("UPDATE actions SET state='approved', approver=?, approved_at=? "
                          "WHERE proposal_id=? AND action_id=?",
                          (approver, _now(), proposal_id, action_id))
            row = self._row(c, proposal_id, action_id)
        return self._approval(row)

    def reject(self, proposal_id: str, action_id: str, approver: str, reason: str = "") -> dict:
        with self._conn() as c:
            row = self._row(c, proposal_id, action_id)
            if row["state"] in ("pending", "approved"):
                c.execute("UPDATE actions SET state='rejected', approver=?, reason=? "
                          "WHERE proposal_id=? AND action_id=?",
                          (approver, reason or "rejected by approver", proposal_id, action_id))
            row = self._row(c, proposal_id, action_id)
        return self._approval(row)

    @staticmethod
    def _approval(row: sqlite3.Row) -> dict:
        return {"proposal_id": row["proposal_id"], "action_id": row["action_id"],
                "state": row["state"], "approver": row["approver"],
                "approved_at": row["approved_at"], "hash": row["hash"],
                **({"reason": row["reason"]} if row["reason"] else {})}

    # -- execute ----------------------------------------------------------- #

    def execute(self, proposal_id: str, action_id: str) -> dict:
        with self._conn() as c:
            row = self._row(c, proposal_id, action_id)
            if row["state"] == "executed":
                return self._receipt(row)
            if row["state"] != "approved":
                raise DoorError("not_approved", f"action {action_id} is {row['state']}, not approved")
            self._verify(c, row, None)
            action = json.loads(row["action_json"])
            path = getattr(self, "_do_" + action["type"].replace(".", "_"))(c, row, action)
            c.execute("UPDATE actions SET state='executed', executed_at=?, result_path=? "
                      "WHERE proposal_id=? AND action_id=?",
                      (_now(), self._rel(path), proposal_id, action_id))
            row = self._row(c, proposal_id, action_id)
        return self._receipt(row)

    @staticmethod
    def _receipt(row: sqlite3.Row) -> dict:
        return {"proposal_id": row["proposal_id"], "action_id": row["action_id"],
                "type": row["type"], "state": row["state"], "approver": row["approver"],
                "executed_at": row["executed_at"], "result_path": row["result_path"],
                "hash": row["hash"]}

    def receipts(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM actions WHERE state != 'pending' "
                             "ORDER BY coalesce(executed_at, approved_at, '') DESC").fetchall()
        return [self._receipt(r) | {"reason": r["reason"]} if r["reason"] else self._receipt(r)
                for r in rows]

    def _rel(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.outbox.resolve()).as_posix()
        except ValueError:
            return str(path)

    def _journal(self, c, row, path: Path) -> None:
        """Remember a file's original bytes once, before the first change."""
        if c.execute("SELECT 1 FROM journal WHERE path=?", (str(path),)).fetchone():
            return
        existed = path.exists()
        original = base64.b64encode(path.read_bytes()).decode() if existed else None
        c.execute("INSERT INTO journal (proposal_id, action_id, path, existed, original_b64) "
                  "VALUES (?,?,?,?,?)",
                  (row["proposal_id"], row["action_id"], str(path), int(existed), original))

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def _do_email_draft(self, c, row, action) -> Path:
        p = action["payload"]
        path = self.outbox / "email" / f"{row['proposal_id']}-{row['action_id']}.json"
        self._journal(c, row, path)
        to = p.get("to") or action.get("to")
        self._write_json(path, {
            "status": "draft", "created_at": _now(),
            "from": {"name": "tesseract", "approved_by": row["approver"]},
            "to": to if isinstance(to, list) else [to], "cc": p.get("cc", []),
            "subject": p.get("subject", ""), "body": p.get("body", ""),
            "citations": p.get("citations", []),
            "proposal": f"{row['proposal_id']}/{row['action_id']}",
        })
        return path

    def _do_slack_draft(self, c, row, action) -> Path:
        p = action["payload"]
        path = self.outbox / "slack" / f"{row['proposal_id']}-{row['action_id']}.json"
        self._journal(c, row, path)
        to = p.get("to") or action.get("to")
        self._write_json(path, {
            "status": "draft", "created_at": _now(),
            "from": {"name": "tesseract", "approved_by": row["approver"]},
            "to": to if isinstance(to, list) else [to],
            "text": p.get("text", ""), "citations": p.get("citations", []),
            "proposal": f"{row['proposal_id']}/{row['action_id']}",
        })
        return path

    def _do_agent_launch(self, c, row, action) -> Path:
        path = self.outbox / "agent" / f"{row['proposal_id']}-{row['action_id']}.json"
        self._journal(c, row, path)
        self._write_json(path, {"status": "launched", "created_at": _now(),
                                "approved_by": row["approver"], "brief": action["payload"],
                                "proposal": f"{row['proposal_id']}/{row['action_id']}"})
        return path

    # -- reset ------------------------------------------------------------- #

    def reset(self, clear: bool = True) -> dict:
        """Undo every outbox write, newest first: restore original bytes, or
        delete files that did not exist. `clear` also drops all proposals."""
        restored, deleted = [], []
        with self._conn() as c:
            for j in c.execute("SELECT * FROM journal ORDER BY seq DESC").fetchall():
                path = Path(j["path"])
                if j["existed"]:
                    path.write_bytes(base64.b64decode(j["original_b64"]))
                    restored.append(self._rel(path))
                elif path.exists():
                    path.unlink()
                    deleted.append(self._rel(path))
                    try:
                        path.parent.rmdir()       # only if now empty
                    except OSError:
                        pass
            c.execute("DELETE FROM journal")
            if clear:
                c.execute("DELETE FROM actions")
                c.execute("DELETE FROM proposals")
        return {"restored": restored, "deleted": deleted, "cleared": clear}


_default: Door | None = None


def door() -> Door:
    global _default
    if _default is None:
        _default = Door()
    return _default


def propose(actions):                       return door().propose(actions)
def approve(pid, aid, approver, payload=None):  return door().approve(pid, aid, approver, payload)
def reject(pid, aid, approver, reason=""):  return door().reject(pid, aid, approver, reason)
def execute(pid, aid):                      return door().execute(pid, aid)
def receipts():                             return door().receipts()
def proposal(pid):                          return door().proposal(pid)
def reset(clear=True):                      return door().reset(clear)
