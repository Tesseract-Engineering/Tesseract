"""
normalize.py - Jira issues to item records.

Two kinds:
  project   one per project: its name, description and lead (as owner).
            Jira gives a project no timestamps, so created_at and updated_at
            are when this content was first seen, and the version is its
            content hash
  issue     the description, then every comment headed with who wrote it, as
            one text. The key is an alias, never the id: keys change when an
            issue moves between projects, so old keys are aliases too

People are the reporter, the assignee, commenters and anyone @-mentioned
(named as the mention shows them), as person:jira:{account id}
(ids.jira_person). Atlassian hides most emails, so
the identity pass matches Jira people by name, not email. Apps (automation,
integrations) are not people; they go in meta.bots.

Refs from fields:
  parent (an epic or parent issue)      part_of
  an outward "Blocks" link              blocks
  an outward "Duplicate" link           duplicates
  an outward link whose name says
  "depend"                              depends_on
  any other outward link                references
Inward links are left to the issue at their other end, which carries the
same link outward. Refs from text: Jira keys, GitHub and Drive links
(swarm/sources/links.py).

Payloads arrive already redacted (swarm/run.py), so the redactions list is
read back from the markers left in the text.
"""

from __future__ import annotations

from typing import Any

from ... import ids
from ...interfaces import RawItem
from ...records import Acl, ItemRecord, Person, Ref, content_hash
from ...stages.redact import redactions_in
from ..links import GitHubLinks, JiraLinks, relink, scan

MAX_TEXT = 200_000


class _People:
    """Roles per account id, in first-seen order."""

    def __init__(self) -> None:
        self._roles: dict[str, list[str]] = {}
        self._names: dict[str, str | None] = {}
        self._emails: dict[str, str | None] = {}
        self.bots: set[str] = set()

    def add(self, user: dict[str, Any] | None, role: str) -> None:
        if not user or not user.get("accountId"):
            return
        if user.get("accountType") == "app":
            self.bots.add(user.get("displayName") or user["accountId"])
            return
        self.mention(user["accountId"], role)
        self._names[user["accountId"]] = self._names.get(user["accountId"]) or user.get("displayName")
        email = user.get("emailAddress")
        self._emails[user["accountId"]] = self._emails.get(user["accountId"]) or (email.lower() if email else None)

    def mention(self, account_id: str, role: str = "mentioned", name: str | None = None) -> None:
        roles = self._roles.setdefault(account_id, [])
        if role not in roles:
            roles.append(role)
        if name and not self._names.get(account_id):
            self._names[account_id] = name

    def build(self) -> list[Person]:
        return [Person(id=ids.jira_person(a), source="jira", handle=a, name=self._names.get(a),
                       email=self._emails.get(a), roles=roles) for a, roles in self._roles.items()]


def _name(field: dict[str, Any] | None) -> str | None:
    return (field or {}).get("name")


class JiraNormalizer:
    def __init__(self, index: JiraLinks, github: GitHubLinks | None = None):
        self.index = index
        self.github = github or GitHubLinks()
        self.run_id = ""

    def relink(self, item: ItemRecord, **when: str) -> ItemRecord | None:
        return relink(item, github=self.github, jira=self.index, **when)

    def to_item(self, raw: RawItem, acl: Acl) -> ItemRecord:
        if raw.payload["type"] == "project":
            return self._project(raw, acl)
        p = raw.payload
        site, issue, f = p["site"], p["issue"], p["fields"]
        item_id = ids.jira_issue(site, issue["id"])

        people = _People()
        people.add(f.get("reporter"), "reporter")
        people.add(f.get("assignee"), "assignee")
        blocks = [p["description"]]
        mentions = list(p.get("mentions") or [])
        names = dict(p.get("mention_names") or {})
        for c in p.get("comments") or []:
            author = c.get("author") or {}
            people.add(author, "commenter")
            mentions += c.get("mentions") or []
            names.update(c.get("mention_names") or {})
            blocks.append(f"--- comment: {author.get('displayName') or 'unknown'} ---\n{c.get('text') or ''}".strip())
        for account_id in mentions:
            people.mention(account_id, name=names.get(account_id))
        text = "\n\n".join(b for b in blocks if b)

        refs: list[Ref] = []

        def add(kind: str, target: str) -> None:
            if all((r.type, r.target) != (kind, target) for r in refs):
                refs.append(Ref(type=kind, target=target, via="field"))

        if f.get("parent"):
            add("part_of", ids.jira_issue(site, f["parent"]["id"]))
        links = []
        for link in f.get("issuelinks") or []:
            kind = link.get("type") or {}
            name = (kind.get("name") or "").lower()
            if "outwardIssue" in link:
                other = link["outwardIssue"]
                add("blocks" if name == "blocks" else "duplicates" if name == "duplicate"
                    else "depends_on" if "depend" in name else "references", ids.jira_issue(site, other["id"]))
                links.append({"type": kind.get("outward") or name, "key": other.get("key")})
            elif "inwardIssue" in link:
                links.append({"type": kind.get("inward") or name, "key": link["inwardIssue"].get("key")})

        # The summary too: "Follow-up to SBX-4" often names its ticket only there.
        found = scan(f"{f.get('summary') or ''}\n\n{text}", self.github, own_id=item_id, jira=self.index)
        refs += [r for r in found.refs if all(r.target != x.target for x in refs)]

        meta: dict[str, Any] = {
            "key": issue["key"], "project_key": p["project"]["key"],
            "issue_type": _name(f.get("issuetype")), "status": _name(f.get("status")),
            "status_category": ((f.get("status") or {}).get("statusCategory") or {}).get("key"),
            "priority": _name(f.get("priority")), "labels": sorted(f.get("labels") or []),
            "components": sorted(c["name"] for c in f.get("components") or []),
            "resolution": _name(f.get("resolution")), "resolved_at": f.get("resolutiondate"),
            "due_date": f.get("duedate"), "parent_key": (f.get("parent") or {}).get("key"),
            "links": links, "old_keys": p.get("old_keys") or [],
            "security_level": _name(f.get("security")), **found.meta(),
        }
        if people.bots:
            meta["bots"] = sorted(people.bots)
        if len(text) > MAX_TEXT:
            text, meta["truncated"] = text[:MAX_TEXT], True
        meta = {k: v for k, v in meta.items() if v not in (None, [], "")}
        title = f.get("summary") or issue["key"]
        built = people.build()
        return ItemRecord(
            id=item_id, aliases=[issue["key"], *(p.get("old_keys") or [])], kind="issue", source="jira",
            container=ids.jira_project(site, p["project"]["id"]), title=title, text=text, url=p["url"],
            created_at=f["created"], updated_at=f["updated"], version=f["updated"], people=built, refs=refs,
            meta=meta, acl=acl, fetched_at=raw.fetched_at, run_id=self.run_id,
            redactions=redactions_in({"title": title, "text": text}),
            content_hash=content_hash(title=title, text=text, container=ids.jira_project(site, p["project"]["id"]),
                                      people=built, refs=refs, meta=meta))

    def _project(self, raw: RawItem, acl: Acl) -> ItemRecord:
        p = raw.payload
        project = p["project"]
        item_id = ids.jira_project(p["site"], project["id"])
        people = _People()
        people.add(p.get("lead"), "owner")
        built = people.build()
        title, text = project.get("name") or project["key"], p.get("description") or ""
        meta = {k: v for k, v in {"key": project["key"], "project_type": p.get("project_type"),
                                  "category": p.get("category")}.items() if v}
        digest = content_hash(title=title, text=text, container=None, people=built, refs=[], meta=meta)
        return ItemRecord(
            id=item_id, aliases=[project["key"]], kind="project", source="jira", container=None, title=title,
            text=text, url=p["url"], created_at=raw.fetched_at, updated_at=raw.fetched_at, version=digest,
            people=built, meta=meta, acl=acl, fetched_at=raw.fetched_at, run_id=self.run_id,
            redactions=redactions_in({"title": title, "text": text}), content_hash=digest)
