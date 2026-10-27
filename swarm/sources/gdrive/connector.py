"""
connector.py - Google Drive behind the Connector interface.

Scope is what the service account was granted: every shared drive it is a
member of (narrowed by GDRIVE_DRIVES), plus the folders listed in
GDRIVE_FOLDERS, which is how an account without Google Workspace shares in.
Personal files outside those are never seen. Every call is a read.

  enumerate   one container per shared drive or listed folder; a shared
              drive's root folder has the drive's id, so both are
              gdrive:file:{id}
  fetch       the root, then every folder and file modified since the
              checkpoint minus 7 days: one query per shared drive, a walk
              down the tree for a listed folder. Docs and Slides are
              exported to plain text, Sheets exported as .xlsx, text PDFs
              read with pypdf, and Office files (Word, PowerPoint, Excel,
              and the Sheets exports) unzipped and read with the standard
              library (swarm/text.py); folders, scanned PDFs and every other
              file type keep metadata only, so the graph still knows the
              file, its owner and who may read it
  list_ids    every folder and file in scope, for reconciliation
  acl         from the file's permissions: anyone -> public, a domain ->
              domain:{d}, a user -> user:{email}, a Google group ->
              group:gdrive:group:{id}. The service account itself is not a
              reader. When the account may not list a file's permissions,
              the file takes its parent folder's, which never names someone
              the file hides from (a file is shared at least as widely as
              its folder), unless the file is a limited-access folder
              (inheritedPermissionsDisabled). With neither, the file has no
              ACL and is not written: it is counted as rejected
  groups      the Google groups those ACLs name. Their members need the
              Admin SDK (domain-wide delegation, an opt-in upgrade), so the
              records carry the group's email as name and no members: the
              graph side grants nobody through them until that upgrade

Permission changes do not move modifiedTime, so nightly runs see them only
for files that changed for another reason; weekly reconciliation re-reads
the rest.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from typing import Any

from ... import ids
from ...http import GOOGLE_RATE_LIMITS, Client, SourceError
from ...interfaces import Checkpoint, RawContainer, RawItem
from ...records import Acl, GroupRecord
from ...text import MAX_OFFICE_BYTES, MAX_PDF_BYTES, office_text, pdf_text
from ...timeutil import iso, parse_iso

log = logging.getLogger("swarm.gdrive")

API = "https://www.googleapis.com"
LOOKBACK = timedelta(days=7)

FOLDER = "application/vnd.google-apps.folder"
FILE_TYPES = {
    FOLDER: "folder",
    "application/vnd.google-apps.document": "doc",
    "application/vnd.google-apps.presentation": "slides",
    "application/vnd.google-apps.spreadsheet": "sheet",
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
}
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
OTHER = "file"                          # any other type: metadata only
FILE_FIELDS = ("id,name,mimeType,parents,driveId,webViewLink,createdTime,modifiedTime,version,size,description,"
               "inheritedPermissionsDisabled,owners(displayName,emailAddress),"
               "lastModifyingUser(displayName,emailAddress)")
PERMISSION_FIELDS = "nextPageToken,permissions(id,type,role,emailAddress,domain,deleted)"
ALL_DRIVES = {"supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}


def group_principal(permission_id: str) -> str:
    return f"group:gdrive:group:{permission_id}"


class DriveConnector:
    source = "gdrive"

    def __init__(self, client: Client, *, drives: list[str] | None = None, folders: list[str] | None = None,
                 service_account: str | None = None, now: datetime | None = None, backfill_days: int = 730):
        self.client = client
        self.allow_drives = {d.lower() for d in drives} if drives else None
        self.folders = list(folders or [])
        self.service_account = (service_account or "").lower()
        self.now = now or datetime.now(timezone.utc)
        self.backfill = timedelta(days=backfill_days)
        self.metadata_only: dict[str, int] = {}                     # mimeType -> other-type files seen
        self._cursors: dict[str, str] = {}
        self._permissions: dict[str, list[dict[str, Any]] | None] = {}
        self._groups: dict[str, dict[str, str]] = {}                # container id -> {principal: email}

    def _pages(self, path: str, params: dict[str, Any], key: str) -> Iterator[dict[str, Any]]:
        return self.client.paginate_cursor(path, params, key=key, cursor_param="pageToken",
                                           next_cursor=lambda body: body.get("nextPageToken"))

    # -- scope ---------------------------------------------------------------- #

    def enumerate(self) -> Iterator[RawContainer]:
        containers = []
        for drive in self._pages("/drive/v3/drives", {"pageSize": 100,
                                 "fields": "nextPageToken,drives(id,name,createdTime)"}, "drives"):
            if self.allow_drives is None or {drive["id"].lower(), drive["name"].lower()} & self.allow_drives:
                root = {"id": drive["id"], "name": drive["name"], "mimeType": FOLDER, "driveId": drive["id"],
                        "createdTime": drive.get("createdTime"), "modifiedTime": drive.get("createdTime"),
                        "webViewLink": f"https://drive.google.com/drive/folders/{drive['id']}"}
                containers.append(RawContainer(source="gdrive", id=ids.gdrive_file(drive["id"]), name=drive["name"],
                                               meta={"scope": "drive", "root": root}))
        for folder_id in self.folders:
            root = self.client.get_json(f"/drive/v3/files/{folder_id}",
                                        {"fields": FILE_FIELDS, "supportsAllDrives": "true"})
            if root.get("mimeType") != FOLDER:
                raise ValueError(f"GDRIVE_FOLDERS lists {folder_id}, which is not a folder")
            containers.append(RawContainer(source="gdrive", id=ids.gdrive_file(folder_id), name=root["name"],
                                           meta={"scope": "folder", "root": root}))
        yield from sorted(containers, key=lambda c: (c.name.lower(), c.id))

    # -- fetch ---------------------------------------------------------------- #

    def _window_start(self, since: Checkpoint | None) -> datetime:
        return parse_iso(since.cursor) - LOOKBACK if since else self.now - self.backfill

    def _files(self, container: RawContainer, start: datetime | None) -> Iterator[dict[str, Any]]:
        """Every folder and file under the container's root (not the root),
        modified at or after `start` when given."""
        root = container.meta["root"]
        fields = f"nextPageToken,files({FILE_FIELDS})"
        if container.meta["scope"] == "drive":
            q = "trashed = false" + (f" and modifiedTime >= '{iso(start)}'" if start else "")
            yield from self._pages("/drive/v3/files", {"corpora": "drive", "driveId": root["id"], "q": q,
                                                       "fields": fields, "pageSize": 1000, **ALL_DRIVES}, "files")
            return
        # A listed folder: walk down, since a query cannot ask for descendants.
        queue, seen = [root["id"]], {root["id"]}
        while queue:
            parent = queue.pop(0)
            for f in self._pages("/drive/v3/files", {"q": f"'{parent}' in parents and trashed = false",
                                                     "fields": fields, "pageSize": 1000, **ALL_DRIVES}, "files"):
                if f["mimeType"] == FOLDER and f["id"] not in seen:
                    seen.add(f["id"])
                    queue.append(f["id"])
                if start is None or parse_iso(f["modifiedTime"]) >= start:
                    yield f

    def fetch(self, container: RawContainer, since: Checkpoint | None) -> Iterator[RawItem]:
        start = self._window_start(since)
        fetched = iso(self.now)
        root = container.meta["root"]
        newest = since.cursor if since else iso(start)
        self._groups.setdefault(container.id, {})
        for f in [root, *self._files(container, start)]:
            kind = FILE_TYPES.get(f["mimeType"], OTHER)
            if kind == OTHER:
                self.metadata_only[f["mimeType"]] = self.metadata_only.get(f["mimeType"], 0) + 1
            if f.get("modifiedTime"):
                newest = max(newest, iso(parse_iso(f["modifiedTime"])))
            text, status = self._text(f, kind)
            permissions, inherited = self._acl_source(f, root["id"])
            yield RawItem(source="gdrive", container=container, native_id=f"file-{f['id']}", fetched_at=fetched,
                          payload={"type": kind, "file": f, "root": root["id"], "text": text,
                                   "text_status": status, "permissions": permissions,
                                   "acl_inherited_from": inherited})
        if self.metadata_only:
            log.info("other file types, metadata only: %s", dict(sorted(self.metadata_only.items())))
        self._cursors[container.id] = newest

    def _text(self, f: dict[str, Any], kind: str) -> tuple[str, str]:
        """The file's text and how it was got: exported, extracted,
        metadata_only, too_large, no_text or unreadable."""
        if kind in ("doc", "slides"):
            try:
                text = self.client.request("GET", f"/drive/v3/files/{f['id']}/export",
                                           params={"mimeType": "text/plain"}).text
                # Docs exports start with a BOM and end lines with CRLF.
                return text.lstrip("﻿").replace("\r\n", "\n"), "exported"
            except SourceError as e:
                if e.reason == "exportSizeLimitExceeded":     # over 10 MB
                    return "", "too_large"
                if e.status == 403 and e.reason not in GOOGLE_RATE_LIMITS:
                    return "", "unreadable"                 # e.g. the owner disabled downloads for viewers
                raise
        if kind == "pdf":
            if int(f.get("size") or 0) > MAX_PDF_BYTES:
                return "", "too_large"
            data = self.client.request("GET", f"/drive/v3/files/{f['id']}",
                                       params={"alt": "media", "supportsAllDrives": "true"}).content
            return pdf_text(data)
        if kind == "sheet":
            # Exported as .xlsx, not CSV: a CSV export holds the first sheet only.
            try:
                data = self.client.request("GET", f"/drive/v3/files/{f['id']}/export",
                                           params={"mimeType": XLSX}).content
            except SourceError as e:
                if e.reason == "exportSizeLimitExceeded":
                    return "", "too_large"
                if e.status == 403 and e.reason not in GOOGLE_RATE_LIMITS:
                    return "", "unreadable"
                raise
            return office_text(data, "xlsx")
        if kind in ("docx", "pptx", "xlsx"):
            if int(f.get("size") or 0) > MAX_OFFICE_BYTES:
                return "", "too_large"
            data = self.client.request("GET", f"/drive/v3/files/{f['id']}",
                                       params={"alt": "media", "supportsAllDrives": "true"}).content
            return office_text(data, kind)
        return "", "metadata_only"

    # -- permissions ---------------------------------------------------------- #

    def _list_permissions(self, file_id: str) -> list[dict[str, Any]] | None:
        """A file's permissions, or None when the account may not list them."""
        if file_id not in self._permissions:
            try:
                self._permissions[file_id] = list(self._pages(
                    f"/drive/v3/files/{file_id}/permissions",
                    {"fields": PERMISSION_FIELDS, "pageSize": 100, "supportsAllDrives": "true"}, "permissions"))
            except SourceError as e:
                # Throttling that outlasted the client's retries is not an answer.
                if e.status not in (403, 404) or e.reason in GOOGLE_RATE_LIMITS:
                    raise
                self._permissions[file_id] = None
        return self._permissions[file_id]

    def _acl_source(self, f: dict[str, Any], root_id: str) -> tuple[list[dict[str, Any]] | None, str | None]:
        own = self._list_permissions(f["id"])
        if own is not None:
            return own, None
        parents = f.get("parents") or []
        # A limited-access folder is shared more narrowly than its parent, so
        # the parent's readers would name people it hides from.
        if f["id"] == root_id or not parents or f.get("inheritedPermissionsDisabled"):
            return None, None
        return self._list_permissions(parents[0]), parents[0]

    def acl(self, item: RawItem) -> Acl | None:
        permissions = item.payload["permissions"]
        if permissions is None:
            return None
        principals: set[str] = set()
        groups = self._groups.setdefault(item.container.id, {})
        for p in permissions:
            if p.get("deleted"):
                continue
            kind, email = p.get("type"), (p.get("emailAddress") or "").lower()
            if kind == "anyone":
                return Acl(visibility="public")
            if kind == "domain" and p.get("domain"):
                principals.add(f"domain:{p['domain'].lower()}")
            elif kind == "user" and email and email != self.service_account:
                principals.add(f"user:{email}")
            elif kind == "group" and p.get("id"):
                principals.add(group_principal(p["id"]))
                groups[group_principal(p["id"])] = email
        if not principals:
            return None
        return Acl(visibility="restricted", principals=sorted(principals))

    def groups(self, container: RawContainer, *, run_id: str) -> list[GroupRecord]:
        return [GroupRecord(run_id=run_id, id=gid, source="gdrive", name=email, members=[], observed_at=self.now)
                for gid, email in sorted(self._groups.get(container.id, {}).items())]

    # -- checkpoints, reconciliation ------------------------------------------ #

    def checkpoint(self, container: RawContainer) -> Checkpoint | None:
        cursor = self._cursors.get(container.id)
        return Checkpoint(container.id, cursor, iso(self.now)) if cursor else None

    def list_ids(self, container: RawContainer) -> Iterator[str]:
        yield container.id
        for f in self._files(container, None):
            yield ids.gdrive_file(f["id"])
