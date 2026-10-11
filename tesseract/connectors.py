"""
connectors.py - deterministic readers, one per source system.

Every connector yields the same shape: SourceRecord. That uniformity is what
lets the rest of the pipeline stay source-agnostic, and it is why adding Slack
or Jira later is a new file here rather than a change everywhere.

Nothing in this layer uses a model. These systems have exact APIs and exact
IDs; an agent "reading" a Jira ticket is a slower, costlier, less reliable
version of a REST call that returns the same fields. Models come in at the next
stage, and only for prose.
"""

from __future__ import annotations

import csv
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class SourceRecord:
    """One item from one system, normalised.

    kind      'commit' | 'issue' | 'document'
    id        stable, globally unique, and reproducible across runs
    text      the prose to extract from (may be empty)
    people    [{'source', 'handle', 'name', 'email'}] - raw identities, NOT yet resolved
    refs      structured references the source itself gives us (e.g. files touched)
    """
    kind: str
    id: str
    title: str
    text: str
    url: str
    created_at: str
    source: str
    people: list[dict] = field(default_factory=list)
    refs: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    # Access control, carried from the source system. Everything here is public;
    # a Slack connector would map channel privacy onto these two fields.
    visibility: str = "public"
    acl_groups: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# git
# --------------------------------------------------------------------------- #

def git_commits(repo: Path, limit: int = 400, *, package_prefixes: tuple[str, ...],
                package_name: str):
    """Commits, their authors, and which package files they touched.

    Commit messages are the most underrated prose in any company: they are
    short, dated, attributed, and they routinely explain WHY.

    package_prefixes and package_name are REQUIRED rather than defaulted. They
    used to be hardcoded to requests, and on Django every path silently failed
    to match: zero TOUCHED edges, and therefore zero measured ownership, with
    no error anywhere. A wrong default is worse than a missing argument.
    """
    sep, fieldsep = "\x1e", "\x1f"
    out = subprocess.run(
        ["git", "-C", str(repo), "log", "--no-merges", f"-{limit}",
         f"--format={sep}%H{fieldsep}%aN{fieldsep}%aE{fieldsep}%aI{fieldsep}%s{fieldsep}%b",
         "--name-only"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=True).stdout

    for chunk in out.split(sep):
        if not chunk.strip():
            continue
        head, _, files_blob = chunk.partition("\n")
        parts = head.split(fieldsep)
        if len(parts) < 5:
            continue
        sha, name, email, when, subject = parts[:5]
        body = parts[5] if len(parts) > 5 else ""

        files = [f.strip() for f in files_blob.split("\n") if f.strip()]
        modules = set()
        for path in files:
            for prefix in package_prefixes:
                if path.startswith(prefix) and path.endswith(".py"):
                    inner = path[len(prefix):-3].split("/")
                    if inner[-1] == "__init__":
                        inner = inner[:-1]
                    modules.add(".".join([package_name, *inner]))

        yield SourceRecord(
            kind="commit", source="git", id=f"commit:{sha}",
            title=subject.strip(), text=f"{subject}\n\n{body}".strip(),
            url="", created_at=when,
            people=[{"source": "git", "handle": email.lower(), "name": name.strip(),
                     "email": email.lower()}],
            refs={"files": files, "modules": sorted(modules)},
            meta={"sha": sha, "shortSha": sha[:10]},
        )


# --------------------------------------------------------------------------- #
# documents
# --------------------------------------------------------------------------- #

# Formats that are prose in a file. The pipeline used to read .md and .rst
# only, which on Django meant 13 documents out of 677: the entire
# documentation tree is .txt, and the connector walked straight past it
# without a word. A reader that silently finds nothing looks exactly like a
# corpus that contains nothing.
def read_plaintext(path: Path, max_bytes: int) -> str:
    return path.read_text(encoding="utf-8", errors="replace")[:max_bytes]


# DOCX is a zip of XML, so this needs no dependency. Paragraph structure is
# kept because it is the only structure there is once the styling is gone,
# and later stages count lines.
DOCX_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def read_docx(path: Path, max_bytes: int) -> str:
    with zipfile.ZipFile(path) as archive:
        try:
            xml = archive.read("word/document.xml")
        except KeyError:
            return ""
    root = ET.fromstring(xml)
    paragraphs = []
    for para in root.iter(f"{DOCX_NS}p"):
        parts = []
        for node in para.iter():
            if node.tag == f"{DOCX_NS}t":
                parts.append(node.text or "")
            elif node.tag in (f"{DOCX_NS}tab",):
                parts.append("\t")
            elif node.tag in (f"{DOCX_NS}br", f"{DOCX_NS}cr"):
                parts.append("\n")
        paragraphs.append("".join(parts))
    return "\n".join(paragraphs)[:max_bytes]


def read_pdf(path: Path, max_bytes: int) -> str:
    """Text layer only. A scanned PDF has none and yields "", which is the
    honest result - it is not an error, and it is not something to paper over
    with OCR nobody asked for. The caller sees an empty document and can say
    so, which is better than a plausible-looking hallucination of one."""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    out, size = [], 0
    for page in reader.pages:
        text = page.extract_text() or ""
        out.append(text)
        size += len(text)
        if size >= max_bytes:
            break
    return "\n\n".join(out)[:max_bytes]


# suffix -> (reader, format name). Extraction quality differs enormously
# between these, and the format is recorded on the record so a later stage can
# treat a quote from a PDF with the suspicion it deserves.
READERS = {
    ".md": (read_plaintext, "markdown"),
    ".rst": (read_plaintext, "restructuredtext"),
    ".txt": (read_plaintext, "plaintext"),
    ".docx": (read_docx, "docx"),
    ".pdf": (read_pdf, "pdf"),
}

# .txt files that are configuration rather than prose. Kept deliberately
# short: this is a convenience, not a correctness mechanism, and a file that
# slips through costs a junk document, not a wrong edge.
NON_PROSE_STEMS = {"requirements", "constraints", "robots"}


def documents(root: Path, suffixes=(".md", ".rst", ".txt"), max_bytes: int = 60_000,
              skip_dirs=(".git", "node_modules", ".venv", "__pycache__"),
              on_error=None):
    """Local prose files. A design doc, a changelog, a README - the places
    where decisions are actually written down.

    `suffixes` defaults to the text formats because they are free to read.
    Pass ('.md', '.rst', '.txt', '.docx', '.pdf') for a corpus of real office
    documents; .docx and .pdf are read only when asked for, so a repository
    scan does not wander into binary attachments.

    A file that cannot be parsed is reported through `on_error` and skipped.
    It is never yielded as an empty document, because an empty document is
    indistinguishable from a real one that happens to say nothing, and the
    difference matters when you are counting coverage.
    """
    unknown = [s for s in suffixes if s not in READERS]
    if unknown:
        raise ValueError(f"no reader for {unknown} - known: {sorted(READERS)}")

    for path in sorted(root.rglob("*")):
        if path.suffix.lower() not in suffixes or not path.is_file():
            continue
        if any(part in skip_dirs for part in path.parts):
            continue
        if path.suffix.lower() == ".txt" and path.stem.lower() in NON_PROSE_STEMS:
            continue
        reader, fmt = READERS[path.suffix.lower()]
        try:
            text = reader(path, max_bytes)
        except Exception as exc:                      # noqa: BLE001 - one bad file
            if on_error is not None:
                on_error(path, exc)
            continue
        rel = path.relative_to(root).as_posix()
        stamp = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()
        yield SourceRecord(
            kind="document", source="docs", id=f"doc:{rel}",
            title=path.name, text=text, url="", created_at=stamp,
            refs={}, meta={"path": rel, "bytes": len(text), "format": fmt},
        )


# --------------------------------------------------------------------------- #
# GitHub issues  (the stand-in for Jira)
# --------------------------------------------------------------------------- #

GITHUB_API = "https://api.github.com/repos/{repo}/issues"


def github_issues(repo: str, cache: Path, pages: int = 3, per_page: int = 100,
                  state: str = "all"):
    """Issues and PRs from the public API.

    Unauthenticated calls are limited to 60/hour, so responses are cached to
    disk and reused. Set GITHUB_TOKEN to raise the limit. A Jira connector
    would look almost identical - different auth, same SourceRecord out.
    """
    cache.parent.mkdir(parents=True, exist_ok=True)
    if cache.exists():
        raw = json.loads(cache.read_text(encoding="utf-8"))
    else:
        raw = []
        token = os.environ.get("GITHUB_TOKEN")
        for page in range(1, pages + 1):
            url = f"{GITHUB_API.format(repo=repo)}?state={state}&per_page={per_page}&page={page}"
            request = urllib.request.Request(url, headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "local-knowledge-graph",
                **({"Authorization": f"Bearer {token}"} if token else {}),
            })
            try:
                with urllib.request.urlopen(request, timeout=30) as response:
                    batch = json.loads(response.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                print(f"  github: HTTP {exc.code} on page {page}"
                      f"{' (rate limited - set GITHUB_TOKEN)' if exc.code in (403, 429) else ''}")
                break
            if not batch:
                break
            raw.extend(batch)
            time.sleep(0.5)
        cache.write_text(json.dumps(raw), encoding="utf-8")

    for item in raw:
        user = item.get("user") or {}
        yield SourceRecord(
            kind="issue", source="github", id=f"issue:{repo}#{item['number']}",
            title=item.get("title") or "", text=(item.get("body") or "")[:20_000],
            url=item.get("html_url") or "", created_at=item.get("created_at") or "",
            people=[{"source": "github", "handle": (user.get("login") or "").lower(),
                     "name": user.get("login") or "", "email": ""}],
            refs={},
            meta={"number": item["number"], "state": item.get("state"),
                  "isPullRequest": "pull_request" in item,
                  "labels": [label["name"] for label in item.get("labels", [])],
                  "closedAt": item.get("closed_at")},
        )


# --------------------------------------------------------------------------- #
# Jira, as an exported CSV
# --------------------------------------------------------------------------- #

# Jira's CSV export names columns by their human label, and an instance with
# custom fields will not match another instance. So the mapping is a default
# that can be overridden, not an assumption.
JIRA_COLUMNS = {
    "key": ("Issue key", "Key"),
    "title": ("Summary",),
    "text": ("Description",),
    "created": ("Created",),
    "reporter": ("Reporter", "Creator"),
    "assignee": ("Assignee",),
    "status": ("Status",),
    "labels": ("Labels",),
}


def _pick(row: dict, names) -> str:
    for name in names:
        if row.get(name):
            return row[name]
    return ""


def jira_csv(path: Path, columns: dict | None = None, project: str = "jira"):
    """A Jira CSV export, one record per row.

    Jira repeats multi-valued fields as repeated columns with the SAME header
    - three labels means three columns all called "Labels" - which csv.DictReader
    collapses to the last one. So the header is read positionally and repeats
    are collected, or two thirds of every label set disappears without trace.
    """
    columns = {**JIRA_COLUMNS, **(columns or {})}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            return
        for values in reader:
            row: dict[str, str] = {}
            repeated: dict[str, list[str]] = {}
            for name, value in zip(header, values, strict=False):
                if value == "":
                    continue
                repeated.setdefault(name, []).append(value)
                row.setdefault(name, value)

            key = _pick(row, columns["key"])
            if not key:
                continue
            labels = [v for name in columns["labels"] for v in repeated.get(name, [])]
            reporter = _pick(row, columns["reporter"])
            yield SourceRecord(
                kind="issue", source="jira", id=f"jira:{project}:{key}",
                title=_pick(row, columns["title"]),
                text=_pick(row, columns["text"]),
                url="", created_at=_pick(row, columns["created"]),
                people=([{"source": "jira", "handle": reporter.lower(),
                          "name": reporter, "email": ""}] if reporter else []),
                refs={},
                meta={"key": key, "state": _pick(row, columns["status"]),
                      "labels": labels, "isPullRequest": False,
                      "assignee": _pick(row, columns["assignee"])},
            )


# --------------------------------------------------------------------------- #
# Slack, as an exported archive
# --------------------------------------------------------------------------- #

def slack_export(root: Path, private_channels=(), default_visibility: str = "internal"):
    """A Slack workspace export: users.json plus one directory per channel.

    A RECORD IS A THREAD, not a message. "sounds good" carries no meaning on
    its own, and a graph built from single messages is a graph of fragments.
    Replies are folded into their parent by thread_ts and the whole exchange
    becomes one record.

    Visibility travels with the record. An export contains whatever the
    exporter could see, and nothing downstream can reconstruct which channel
    was private once the text is loose, so the ACL is attached here or never.
    Channels named in `private_channels` are marked private and carry the
    channel as their acl_group.

    This reads an EXPORT, which is a file someone was authorised to produce.
    It is not the API connector below, and it deliberately does not need a
    token or an OAuth scope.
    """
    users = {}
    users_file = root / "users.json"
    if users_file.exists():
        for user in json.loads(users_file.read_text(encoding="utf-8")):
            profile = user.get("profile") or {}
            users[user.get("id")] = {
                "handle": (user.get("name") or "").lower(),
                "name": profile.get("real_name") or user.get("name") or "",
                "email": (profile.get("email") or "").lower(),
                "isBot": bool(user.get("is_bot")),
            }

    private = set(private_channels)
    for channel_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        channel = channel_dir.name
        threads: dict[str, list[dict]] = {}
        for day in sorted(channel_dir.glob("*.json")):
            try:
                messages = json.loads(day.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for message in messages:
                if message.get("subtype") in ("channel_join", "channel_leave"):
                    continue
                if not (message.get("text") or "").strip():
                    continue
                root_ts = message.get("thread_ts") or message.get("ts")
                threads.setdefault(root_ts, []).append(message)

        for root_ts, messages in threads.items():
            messages.sort(key=lambda m: float(m.get("ts") or 0))
            lines, people, seen = [], [], set()
            for message in messages:
                who = users.get(message.get("user"), {})
                name = who.get("name") or message.get("user") or "unknown"
                lines.append(f"{name}: {message.get('text')}")
                handle = who.get("handle") or message.get("user")
                if handle and handle not in seen:
                    seen.add(handle)
                    people.append({"source": "slack", "handle": handle,
                                   "name": who.get("name") or handle,
                                   "email": who.get("email") or ""})
            stamp = datetime.fromtimestamp(float(root_ts), timezone.utc).isoformat()
            is_private = channel in private
            yield SourceRecord(
                kind="document", source="slack",
                id=f"slack:{channel}:{root_ts}",
                title=f"#{channel} thread {root_ts}",
                text="\n".join(lines), url="", created_at=stamp,
                people=people, refs={},
                meta={"channel": channel, "threadTs": root_ts,
                      "messages": len(messages), "format": "slack-thread"},
                visibility="private" if is_private else default_visibility,
                acl_groups=[f"slack:{channel}"] if is_private else [],
            )


# --------------------------------------------------------------------------- #
# Microsoft 365, as exported folders: SharePoint, Outlook, Teams
# --------------------------------------------------------------------------- #

# SharePoint sensitivity labels that close a document to its site. Anything
# else ("General", "Internal", no label) is readable company-wide.
RESTRICTED_SENSITIVITY = {"confidential", "highly confidential", "restricted", "secret"}
SHAREPOINT_SUFFIXES = (".md", ".docx", ".pdf")


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def sharepoint_export(root: Path, max_bytes: int = 60_000, on_error=None):
    """Documents under root/sharepoint/<site>/<library>/**.

    A file may carry a sidecar `<file>.meta.json` with author, modified, site,
    library, sensitivity and url - the fields SharePoint keeps outside the
    file itself. Without one, site and library come from the folder layout.

    Sensitivity is the ACL. A Confidential document is `restricted` to its
    site (acl `sharepoint:<site>`); that decision is made here, at ingest,
    because once the text is loose nothing downstream can tell.
    """
    base = root / "sharepoint"
    if not base.is_dir():
        return
    for path in sorted(base.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SHAREPOINT_SUFFIXES:
            continue
        rel_inner = path.relative_to(base).parts
        if len(rel_inner) < 3:                       # needs <site>/<library>/<file>
            continue
        reader, fmt = READERS[path.suffix.lower()]
        try:
            text = reader(path, max_bytes)
        except Exception as exc:                      # noqa: BLE001 - one bad file
            if on_error is not None:
                on_error(path, exc)
            continue
        sidecar_path = path.with_name(path.name + ".meta.json")
        sidecar = _read_json(sidecar_path) if sidecar_path.is_file() else None
        sidecar = sidecar if isinstance(sidecar, dict) else {}
        site = sidecar.get("site") or rel_inner[0]
        library = sidecar.get("library") or rel_inner[1]
        sensitivity = sidecar.get("sensitivity") or ""
        restricted = sensitivity.strip().lower() in RESTRICTED_SENSITIVITY
        author = (sidecar.get("author") or "").strip()
        people = []
        if author:
            email = author.lower() if "@" in author else ""
            people.append({"source": "sharepoint", "handle": author.lower(),
                           "name": "" if email else author, "email": email})
        stamp = sidecar.get("modified") or datetime.fromtimestamp(
            path.stat().st_mtime, timezone.utc).isoformat()
        rel = path.relative_to(root).as_posix()
        meta = {"path": rel, "site": site, "library": library, "format": fmt,
                "bytes": len(text), "sensitivity": sensitivity, "author": author}
        if sidecar:
            meta["sidecar"] = sidecar_path.relative_to(root).as_posix()
        yield SourceRecord(
            kind="document", source="sharepoint", id=f"sharepoint:{site}/{'/'.join(rel_inner[1:])}",
            title=path.stem.replace("-", " "), text=text, url=sidecar.get("url") or "",
            created_at=stamp, people=people, refs={}, meta=meta,
            visibility="restricted" if restricted else "internal",
            acl_groups=[f"sharepoint:{site}"] if restricted else [],
        )


def _mail_party(party) -> dict | None:
    """{"name","email"} or a bare address string -> one identity, keyed by email."""
    if isinstance(party, str):
        party = {"email": party}
    if not isinstance(party, dict):
        return None
    email = (party.get("email") or "").strip().lower()
    name = (party.get("name") or "").strip()
    if not email and not name:
        return None
    return {"source": "outlook", "handle": email or name.lower(),
            "name": name or email, "email": email}


def outlook_export(root: Path):
    """Mail threads under root/outlook/mailboxes/<user>/*.json, one file per
    thread: {"subject","conversation_id","messages":[{"from","to","cc","date","body"}]}.

    A RECORD IS A CONVERSATION. The same thread sits in every participant's
    mailbox; it is yielded once, keyed by conversation_id, and every mailbox
    copy is listed in meta['paths'] so coverage still counts them as read.

    Mail is private to the people on it: visibility=private, one acl group
    `outlook:user:<email>` per participant (from, to and cc).
    """
    base = root / "outlook" / "mailboxes"
    if not base.is_dir():
        return
    threads: dict[str, tuple[Path, dict, list[str]]] = {}
    for path in sorted(base.glob("*/*.json")):
        thread = _read_json(path)
        if not isinstance(thread, dict) or not isinstance(thread.get("messages"), list):
            continue
        key = thread.get("conversation_id") or f"{path.parent.name}/{path.stem}"
        rel = path.relative_to(root).as_posix()
        if key in threads:
            threads[key][2].append(rel)
        else:
            threads[key] = (path, thread, [rel])

    for key, (path, thread, paths) in threads.items():
        lines, participants, counts = [], {}, {}
        messages = [m for m in thread["messages"] if isinstance(m, dict)]
        for message in messages:
            sender = _mail_party(message.get("from"))
            body = (message.get("body") or "").strip()
            if sender:
                lines.append(f"{sender['name']}: {body}")
                counts[sender["handle"]] = counts.get(sender["handle"], 0) + 1
            elif body:
                lines.append(body)
            for party in [message.get("from"), *(message.get("to") or []),
                          *(message.get("cc") or [])]:
                who = _mail_party(party)
                if who:
                    entry = participants.setdefault(who["handle"], {**who, "count": 0})
                    if not entry["email"] and who["email"]:
                        entry["email"] = who["email"]
        for handle, entry in participants.items():
            entry["count"] = counts.get(handle, 0)
        emails = sorted({p["email"] for p in participants.values() if p["email"]})
        started = messages[0].get("date") if messages else ""
        yield SourceRecord(
            kind="document", source="outlook", id=f"outlook:{key}",
            title=thread.get("subject") or path.stem, text="\n".join(lines), url="",
            created_at=started or "", people=list(participants.values()), refs={},
            meta={"path": paths[0], "paths": paths, "mailbox": path.parent.name,
                  "conversationId": key, "format": "outlook-thread",
                  "messages": len(messages), "messageCounts": counts},
            visibility="private", acl_groups=[f"outlook:user:{e}" for e in emails],
        )


def _chat_thread(thread: dict, path: Path, root: Path, record_id: str, shape: str):
    """One Teams conversation (channel thread or chat) -> SourceRecord."""
    lines, participants, counts = [], {}, {}
    messages = [m for m in thread.get("messages") or [] if isinstance(m, dict)]
    for message in messages:
        text = message.get("text") or ""
        if not text.strip():
            continue
        handle = (message.get("user") or "").lstrip("@").lower()
        name = (message.get("name") or "").strip() or handle or "unknown"
        lines.append(f"{name}: {text}")
        if handle:
            counts[handle] = counts.get(handle, 0) + 1
            entry = participants.setdefault((handle, name), {
                "source": "teams", "handle": handle, "name": name, "email": "", "count": 0})
            entry["count"] += 1
    started = (thread.get("started") or thread.get("created_at")
               or (messages[0].get("timestamp") if messages else ""))
    if shape == "chat":
        members = sorted({str(h).lstrip("@").lower() for h in thread.get("participants") or []}
                         | set(counts))
        visibility, acl = "private", [f"teams:user:{h}" for h in members]
    elif thread.get("is_private"):
        visibility = "restricted"
        acl = [f"teams:{thread.get('team') or path.parent.parent.name}/"
               f"{thread.get('channel') or path.parent.name}"]
    else:
        visibility, acl = "internal", []
    meta = {"format": "teams-chat" if shape == "chat" else "teams-thread",
            "messages": len(messages), "messageCounts": counts,
            "path": path.relative_to(root).as_posix()}
    if shape == "chat":
        meta["participants"] = thread.get("participants") or []
    else:
        meta["team"] = thread.get("team") or path.parent.parent.name
        meta["channel"] = thread.get("channel") or path.parent.name
    return SourceRecord(
        kind="document", source="teams", id=record_id,
        title=thread.get("topic") or path.stem, text="\n".join(lines), url="",
        created_at=started or "", people=list(participants.values()), refs={},
        meta=meta, visibility=visibility, acl_groups=acl,
    )


def teams_export(root: Path):
    """Microsoft Teams under root/teams/: channel threads at
    <team>/<channel>/*.json and 1:1 or group chats at chats/*.json.

    Same message shape as the Slack workspace export ({"user","name",
    "timestamp","text"}), same rule: a record is a thread. Chats are private
    to their participants (acl `teams:user:<handle>`); a channel marked
    is_private is restricted to `teams:<team>/<channel>`.
    """
    base = root / "teams"
    if not base.is_dir():
        return
    for path in sorted((base / "chats").glob("*.json")):
        thread = _read_json(path)
        if isinstance(thread, dict) and isinstance(thread.get("messages"), list):
            key = thread.get("conversation_id") or thread.get("thread_id") or path.stem
            yield _chat_thread(thread, path, root, f"teams:chat:{key}", "chat")
    for path in sorted(base.glob("*/*/*.json")):
        if path.parent.parent.name == "chats" or path.parent.name == "chats":
            continue
        thread = _read_json(path)
        if not isinstance(thread, dict) or not isinstance(thread.get("messages"), list):
            continue
        team = path.parent.parent.name
        channel = path.parent.name
        key = thread.get("thread_id") or path.stem
        yield _chat_thread(thread, path, root, f"teams:{team}/{channel}/{key}", "thread")


# --------------------------------------------------------------------------- #
# The seam for the systems not wired up yet
# --------------------------------------------------------------------------- #

def slack_messages(*_args, **_kwargs):
    """NOT IMPLEMENTED - the LIVE API only, deliberately. slack_export() above
    reads an archive someone was authorised to produce, which is the supported
    path and needs none of the following.

    The work here is not the API call, it is everything around it:

      * OAuth scopes and a bot token, with someone accountable for approving them
      * channel privacy -> SourceRecord.visibility / acl_groups, at ingest.
        A private channel's contents must never reach a node without its ACL.
      * thread reconstruction: a "record" is a thread, not a message
      * edits and deletions: Slack messages change after the fact, so you need
        tombstones or the graph is confidently wrong
      * PII and retention policy: DMs are usually out of scope entirely

    Everything downstream of this file already handles the SourceRecord shape,
    so wiring Slack is this function plus an ACL mapping - not a redesign.
    """
    raise NotImplementedError("Slack connector requires workspace approval and a bot token")


def jira_issues(*_args, **_kwargs):
    """NOT IMPLEMENTED - the LIVE API only. jira_csv() above reads an export
    and is the supported path; this is what a polling connector would need.

    Jira differs in auth (API token), pagination (startAt/maxResults), and that
    project permissions must map onto acl_groups. The SourceRecord it yields
    would be identical in shape to github_issues.
    """
    raise NotImplementedError("Jira connector requires a site URL and API token")


SOURCE_TEXT_LIMIT = 20_000
ISSUE_REF = re.compile(r"#(\d{1,6})\b")
