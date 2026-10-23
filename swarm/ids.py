"""
ids.py - the ID formats every record and every ref target use.

IDs are built from immutable source IDs only (a Jira issue id, never its key;
a GitHub repo id, never its name), so they survive renames and project moves.
Mutable names go in `aliases`. Because every producer builds IDs here, the
linker can match references by plain string comparison.
"""

from __future__ import annotations

import re
from urllib.parse import quote

_SEG = r"[^:\s]+"

# Object kind -> full-match pattern. Order matters only for readability.
PATTERNS: dict[str, re.Pattern[str]] = {
    "github_repo":    re.compile(rf"github:repo:{_SEG}"),
    "github_pr":      re.compile(rf"github:repo:{_SEG}:pr:\d+"),
    "github_issue":   re.compile(rf"github:repo:{_SEG}:issue:\d+"),
    "github_commit":  re.compile(rf"github:repo:{_SEG}:commit:[0-9a-f]{{7,40}}"),
    "github_file":    re.compile(rf"github:repo:{_SEG}:file:{_SEG}"),
    "jira_issue":     re.compile(rf"jira:{_SEG}:issue:\d+"),
    "jira_project":   re.compile(rf"jira:{_SEG}:project:\d+"),
    "slack_channel":  re.compile(rf"slack:{_SEG}:channel:{_SEG}"),
    "slack_thread":   re.compile(rf"slack:{_SEG}:thread:{_SEG}:\d+\.\d+"),
    "gdrive_file":    re.compile(rf"gdrive:file:{_SEG}"),
    "person_source":  re.compile(rf"person:(?!c:){_SEG}:{_SEG}"),
    "person_cluster": re.compile(r"person:c:[0-9a-f]{6,64}"),
}


# ID kind -> the ontology entity types an object with that ID can be. A Drive
# id names a document or a folder; the ID alone does not say which. A Jira
# project is a container only, never a relation endpoint.
ENTITY_TYPES: dict[str, frozenset[str]] = {
    "github_repo":    frozenset({"repo"}),
    "github_pr":      frozenset({"pull_request"}),
    "github_issue":   frozenset({"issue"}),
    "github_commit":  frozenset({"commit"}),
    "github_file":    frozenset({"document"}),
    "jira_issue":     frozenset({"issue"}),
    "jira_project":   frozenset(),
    "slack_channel":  frozenset({"channel"}),
    "slack_thread":   frozenset({"thread"}),
    "gdrive_file":    frozenset({"document", "folder"}),
    "person_source":  frozenset({"person"}),
    "person_cluster": frozenset({"person"}),
}


def entity_types(value: str) -> frozenset[str]:
    """What an ID can point at, for checking relation endpoints."""
    kind = kind_of(value)
    return ENTITY_TYPES[kind] if kind else frozenset()


def kind_of(value: str) -> str | None:
    """The ID kind `value` matches, or None if it matches no pattern."""
    for kind, pattern in PATTERNS.items():
        if pattern.fullmatch(value):
            return kind
    return None


def is_valid(value: str) -> bool:
    return kind_of(value) is not None


def github_repo(repo_id: int | str) -> str:
    return f"github:repo:{repo_id}"


def github_pr(repo_id: int | str, number: int) -> str:
    return f"{github_repo(repo_id)}:pr:{number}"


def github_issue(repo_id: int | str, number: int) -> str:
    return f"{github_repo(repo_id)}:issue:{number}"


def github_commit(repo_id: int | str, sha: str) -> str:
    return f"{github_repo(repo_id)}:commit:{sha.lower()}"


def github_file(repo_id: int | str, path: str) -> str:
    """A file on the default branch, by its path: git gives files no id that
    survives an edit, so a renamed file is a new item and the old id is
    tombstoned. Spaces and colons are percent-encoded to keep the id one
    segment; slashes stay."""
    return f"{github_repo(repo_id)}:file:{quote(path, safe='/')}"


def jira_issue(site: str, issue_id: int | str) -> str:
    return f"jira:{site}:issue:{issue_id}"


def jira_project(site: str, project_id: int | str) -> str:
    return f"jira:{site}:project:{project_id}"


def slack_channel(team_id: str, channel_id: str) -> str:
    return f"slack:{team_id}:channel:{channel_id}"


def slack_thread(team_id: str, channel_id: str, thread_ts: str) -> str:
    return f"slack:{team_id}:thread:{channel_id}:{thread_ts}"


def gdrive_file(file_id: str) -> str:
    return f"gdrive:file:{file_id}"


def person(source: str, handle: str) -> str:
    return f"person:{source}:{handle}"


def person_cluster(cluster_hash: str) -> str:
    return f"person:c:{cluster_hash}"
