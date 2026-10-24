"""
normalize.py - Drive files to item records.

  folder     kind folder: its name, and its description as text
  document   Docs, Slides, PDFs, Word and PowerPoint files with their text;
             Sheets, Excel files, PDFs without text and any other file type
             keep metadata only (meta.text_status says which)

People are the file's owners (role owner; files in a shared drive have none)
and the last person to modify it (participant), as person:gdrive:{email}.
Refs are the cross-source links in the text (swarm/sources/links.py).

Payloads arrive already redacted (swarm/run.py), so the redactions list is
read back from the markers left in the text.
"""

from __future__ import annotations

from typing import Any

from ... import ids
from ...interfaces import RawItem
from ...records import Acl, ItemRecord, Person, content_hash
from ...stages.redact import redactions_in
from ..links import GitHubLinks, JiraLinks, scan

MAX_TEXT = 200_000


class DriveNormalizer:
    def __init__(self, github: GitHubLinks | None = None, jira: JiraLinks | None = None):
        self.github = github or GitHubLinks()
        self.jira = jira
        self.run_id = ""

    def to_item(self, raw: RawItem, acl: Acl) -> ItemRecord:
        p = raw.payload
        f = p["file"]
        is_root = f["id"] == p["root"]
        parents = f.get("parents") or []
        item_id = ids.gdrive_file(f["id"])
        text = p["text"] or f.get("description") or ""

        people: dict[str, Person] = {}
        for owner in f.get("owners") or []:
            self._person(people, owner, "owner")
        if f.get("lastModifyingUser"):
            self._person(people, f["lastModifyingUser"], "participant")

        meta: dict[str, Any] = {"file_type": p["type"], "mime_type": f["mimeType"], "text_status": p["text_status"]}
        if f.get("driveId"):
            meta["drive_id"] = f["driveId"]
        if p.get("acl_inherited_from"):
            meta["acl_inherited_from"] = ids.gdrive_file(p["acl_inherited_from"])
        links = scan(text, self.github, own_id=item_id, jira=self.jira)
        meta.update(links.meta())
        if len(text) > MAX_TEXT:
            text, meta["truncated"] = text[:MAX_TEXT], True

        title = f["name"]
        container = None if is_root or not parents else ids.gdrive_file(parents[0])
        person_list = list(people.values())
        return ItemRecord(
            id=item_id, aliases=[title], kind="folder" if p["type"] == "folder" else "document", source="gdrive",
            container=container, title=title, text=text,
            url=f.get("webViewLink") or f"https://drive.google.com/file/d/{f['id']}/view",
            created_at=f.get("createdTime") or f["modifiedTime"], updated_at=f.get("modifiedTime") or f["createdTime"],
            version=str(f.get("version") or f.get("modifiedTime") or f.get("createdTime")),
            content_hash=content_hash(title=title, text=text, container=container, people=person_list,
                                      refs=links.refs, meta=meta),
            people=person_list, refs=links.refs, meta=meta, acl=acl, fetched_at=raw.fetched_at, run_id=self.run_id,
            redactions=redactions_in({"title": title, "text": text}))

    @staticmethod
    def _person(people: dict[str, Person], user: dict[str, Any], role: str) -> None:
        email = (user.get("emailAddress") or "").lower()
        if not email:
            return
        if email in people:
            if role not in people[email].roles:
                people[email] = people[email].model_copy(update={"roles": [*people[email].roles, role]})
            return
        people[email] = Person(id=ids.person("gdrive", email), source="gdrive", handle=email,
                               name=user.get("displayName"), email=email, roles=[role])
