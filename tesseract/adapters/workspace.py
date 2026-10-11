"""
adapters/workspace.py - a company's exports in the standard Tesseract layout.

Point the builder at a folder laid out like this; every part is optional:

    company.json            name, email domain and viewer presets (see config.py)
    people/directory.json   the roster (format below)
    slack/                  a Slack workspace export: users.json + one folder per channel
    jira/*.csv              Jira CSV exports, one file per project
    sharepoint/<site>/...   SharePoint libraries, with optional <file>.meta.json sidecars
    outlook/mailboxes/...   Outlook mailbox exports
    teams/...               Microsoft Teams channel threads and chats
    docs/                   anything else worth reading: .md .rst .txt .docx .pdf

people/directory.json:

    {"employees": [{"id": "e1", "name": "Jane Doe", "email": "jane@example.com",
                    "slack": "jane", "title": "Staff Engineer", "team": "Payments",
                    "status": "employee" | "alumni", "departed_at": "2026-01-31"}],
     "private_slack_channels": ["security-incidents"]}

What the layout gives the graph: people and their accounts (so one human is one
Person across Slack, email and Jira), teams from the roster, tickets with their
assignees, threads and documents with their authors, and the ACL each source
carries (private Slack channels, Confidential SharePoint files, private chats).

Like every adapter it reads and renames; it decides nothing. See bundle.py.
"""

from __future__ import annotations

import dataclasses
import json
import re
import sys
from pathlib import Path

from .. import connectors
from ..bundle import Bundle

NAME = "workspace"
SPEAKER_SOURCES = {"slack", "jira", "outlook", "teams"}
DOC_SUFFIXES = (".md", ".rst", ".txt", ".docx", ".pdf")
PARTS = ("people/directory.json", "slack", "jira", "sharepoint", "outlook", "teams", "docs")


def looks_like(root: Path) -> bool:
    return sum((root / p).exists() for p in PARTS) >= 2


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def roster(root: Path) -> tuple[list[dict], list[dict], list[dict], list[str]]:
    """(people, accounts, memberships, private slack channels) from people/directory.json."""
    path = root / "people" / "directory.json"
    if not path.is_file():
        return [], [], [], []
    data = json.loads(path.read_text(encoding="utf-8"))
    people, accounts, memberships = [], [], []
    for row in data.get("employees") or []:
        name = (row.get("name") or "").strip()
        if not name:
            continue
        email = (row.get("email") or "").lower()
        slack = (row.get("slack") or "").lstrip("@").lower()
        if email:
            accounts.append({"source": "directory", "handle": email, "name": name, "email": email})
        if slack:
            accounts.append({"source": "slack", "handle": slack, "name": name, "email": email})
        if not email and not slack:
            # Someone the roster lists but who has no account left (often alumni)
            # still exists, so documents that credit them can say who they were.
            accounts.append({"source": "directory", "handle": name.lower(), "name": name, "email": ""})
        status = row.get("status") or "employee"
        people.append({"empId": row.get("id"), "name": name, "title": row.get("title"),
                       "department": row.get("department"), "location": row.get("location"),
                       "status": status, "departedAt": row.get("departed_at")})
        if row.get("team") and status != "alumni":
            memberships.append({"name": name, "team": row["team"]})
    return people, accounts, memberships, list(data.get("private_slack_channels") or [])


def jira_tickets(root: Path):
    """Each Jira CSV, with the ticket keyed as ticket:<KEY> and its assignee declared."""
    for path in sorted((root / "jira").glob("*.csv")):
        for record in connectors.jira_csv(path, project=path.stem):
            key = record.meta.get("key")
            assignee = record.meta.get("assignee") or ""
            meta = {**record.meta, "path": path.relative_to(root).as_posix()}
            if assignee:
                meta["assigneeName"] = assignee
            yield dataclasses.replace(record, id=f"ticket:{key}", meta=meta)


H1 = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)


def documents(root: Path):
    """Everything under docs/, titled by its first Markdown heading when it has one."""
    docs = root / "docs"
    if not docs.is_dir():
        return

    def report(path: Path, exc: Exception) -> None:
        print(f"  could not read {path.relative_to(root)}: {exc}", file=sys.stderr)

    for record in connectors.documents(docs, suffixes=DOC_SUFFIXES, on_error=report):
        heading = H1.search(record.text[:2000]) if record.meta.get("format") == "markdown" else None
        yield dataclasses.replace(record, title=heading.group(1) if heading else record.title,
                                  meta={**record.meta, "path": f"docs/{record.meta['path']}"})


def load(root: Path) -> Bundle:
    people, accounts, memberships, private_channels = roster(root)
    bundle = Bundle(name=NAME, root=root, speaker_sources=set(SPEAKER_SOURCES))
    bundle.people, bundle.accounts = people, accounts

    teams = sorted({m["team"] for m in memberships})
    for team in teams:
        tid = f"team:{_slug(team)}"
        bundle.teams.append({"id": tid, "name": team, "source": "directory", "source_id": tid,
                             "producer": "field:team", "props": {"name": team}})
    by_name = {t: f"team:{_slug(t)}" for t in teams}
    for m in memberships:
        bundle.memberships.append({"name": m["name"], "team": by_name[m["team"]], "teamLabel": m["team"],
                                   "via": "directory", "producer": "field:team", "rank": 0})

    if (root / "slack").is_dir():
        bundle.records.extend(connectors.slack_export(root / "slack", private_channels=private_channels))
        bundle.files_read.update(p.relative_to(root).as_posix() for p in (root / "slack").rglob("*.json"))
    bundle.records.extend(jira_tickets(root))
    for reader in (connectors.sharepoint_export, connectors.outlook_export, connectors.teams_export):
        bundle.records.extend(reader(root))
    bundle.records.extend(documents(root))

    for record in bundle.records:
        if record.source == "sharepoint" and record.meta.get("sidecar"):
            bundle.files_read.add(record.meta["sidecar"])
        elif record.source == "outlook":
            bundle.files_read.update(record.meta.get("paths") or [])
        elif record.source == "teams" and record.meta.get("path"):
            bundle.files_read.add(record.meta["path"])
    bundle.files_read.update(p for p in ("people/directory.json", "company.json") if (root / p).is_file())
    return bundle
