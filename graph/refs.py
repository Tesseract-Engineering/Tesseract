"""refs.py - where a record lives in its source system, in one comparable form.

A SourceRef names an item in a live provider: enough to fetch it again, link
to it, and tell whether two records are the same item. Graph nodes carry one
(`ref`, with `refProvider` and `refKind`) when their source has a live provider;
everything else is index-only. build_swarm.py writes refs onto nodes; forge
reads them to fetch live content. The format is the graph's: forge imports it,
and nothing here imports forge.

    forge://github/blob/acme/api@<sha>:src/auth/session.py
    forge://github/pull/acme/api/412
    forge://github/issue/acme/api/7
    forge://github/repo/acme/api
    forge://jira/issue/AUTH-42
    forge://gdrive/file/<fileId>
    forge://gdrive/folder/<folderId>

`version` pins a ref to one revision (a blob's commit SHA). In the URI it sits
after the id's first segment group, before the path: `<id head>@<version>:<path>`.

to_url and from_url convert to and from the links people paste. from_url is
deliberately forgiving (query strings, fragments, /files suffixes, the several
Google Docs hosts) and returns None for anything it does not recognise.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

SCHEME = "forge://"

# provider -> the kinds it has. The spec validator (forge) checks against this.
# A record's `source` names its provider; sources not listed here are
# index-only: the graph holds their text, but nothing can fetch it again.
KINDS: dict[str, tuple[str, ...]] = {
    "github": ("blob", "pull", "issue", "repo"),
    "jira": ("issue",),
    "gdrive": ("file", "folder"),
}

JIRA_KEY = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")
_GH_NAME = r"[A-Za-z0-9_.\-]+"
_GH_REPO = re.compile(rf"^{_GH_NAME}/{_GH_NAME}$")
_GH_NUMBERED = re.compile(rf"^{_GH_NAME}/{_GH_NAME}/\d+$")
_GH_BLOB = re.compile(rf"^{_GH_NAME}/{_GH_NAME}:.+$")
_DRIVE_ID = re.compile(r"^[A-Za-z0-9_\-]{10,}$")

_ID_SHAPE = {
    ("github", "blob"): _GH_BLOB,
    ("github", "pull"): _GH_NUMBERED,
    ("github", "issue"): _GH_NUMBERED,
    ("github", "repo"): _GH_REPO,
    ("jira", "issue"): JIRA_KEY,
    ("gdrive", "file"): _DRIVE_ID,
    ("gdrive", "folder"): _DRIVE_ID,
}


@dataclass(frozen=True)
class SourceRef:
    provider: str
    kind: str
    id: str
    version: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in KINDS.get(self.provider, ()):
            raise ValueError(f"unknown ref kind {self.provider}/{self.kind}")
        if not _ID_SHAPE[(self.provider, self.kind)].match(self.id):
            raise ValueError(f"malformed {self.provider}/{self.kind} id {self.id!r}")
        if self.version is not None and (not self.version or "@" in self.version or ":" in self.version):
            raise ValueError(f"malformed ref version {self.version!r}")

    def uri(self) -> str:
        ident = self.id
        if self.version:
            head, sep, tail = ident.partition(":")
            ident = f"{head}@{self.version}{sep}{tail}"
        return f"{SCHEME}{self.provider}/{self.kind}/{ident}"

    def unversioned(self) -> "SourceRef":
        return SourceRef(self.provider, self.kind, self.id) if self.version else self

    def __str__(self) -> str:
        return self.uri()


def parse(uri: str) -> SourceRef:
    """The inverse of SourceRef.uri(). Raises ValueError on anything else."""
    if not uri.startswith(SCHEME):
        raise ValueError(f"not a {SCHEME} uri: {uri!r}")
    provider, _, rest = uri[len(SCHEME):].partition("/")
    kind, _, ident = rest.partition("/")
    head, sep, tail = ident.partition(":")
    version = None
    if "@" in head:
        head, version = head.rsplit("@", 1)
    return SourceRef(provider, kind, f"{head}{sep}{tail}", version)


# --------------------------------------------------------------------------- #
# links
# --------------------------------------------------------------------------- #

def to_url(ref: SourceRef, jira_base_url: str | None = None) -> str | None:
    """The browser link for a ref. Jira needs the site's base URL
    (`https://acme.atlassian.net`); without it there is no link, rather than a guessed one."""
    if ref.provider == "github":
        if ref.kind == "repo":
            return f"https://github.com/{ref.id}"
        if ref.kind == "blob":
            repo, _, path = ref.id.partition(":")
            return f"https://github.com/{repo}/blob/{ref.version or 'HEAD'}/{path}"
        repo, _, number = ref.id.rpartition("/")
        return f"https://github.com/{repo}/{'pull' if ref.kind == 'pull' else 'issues'}/{number}"
    if ref.provider == "jira":
        base = (jira_base_url or "").rstrip("/")
        return f"{base}/browse/{ref.id}" if base else None
    if ref.provider == "gdrive":
        if ref.kind == "folder":
            return f"https://drive.google.com/drive/folders/{ref.id}"
        return f"https://drive.google.com/file/d/{ref.id}/view"
    return None


def from_url(url: str) -> SourceRef | None:
    """The ref a link points at, or None when no provider parser recognises it."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    host = parts.hostname.lower()
    segments = [s for s in parts.path.split("/") if s]
    for parser in (_github, _gdrive, _jira):
        try:
            ref = parser(host, segments, parts.query)
        except ValueError:
            ref = None
        if ref:
            return ref
    return None


def _github(host: str, seg: list[str], _query: str) -> SourceRef | None:
    if host not in ("github.com", "www.github.com") or len(seg) < 2:
        return None
    repo = f"{seg[0]}/{seg[1].removesuffix('.git')}"
    if len(seg) == 2:
        return SourceRef("github", "repo", repo)
    if seg[2] in ("pull", "issues") and len(seg) >= 4 and seg[3].isdigit():
        return SourceRef("github", "pull" if seg[2] == "pull" else "issue", f"{repo}/{seg[3]}")
    if seg[2] == "blob" and len(seg) >= 5:
        # A branch name with a slash in it is ambiguous offline; the first
        # segment is taken as the version. Refs Tesseract writes pin a SHA,
        # and HEAD (what to_url writes for an unpinned blob) means no version.
        version = None if seg[3] == "HEAD" else seg[3]
        return SourceRef("github", "blob", f"{repo}:{'/'.join(seg[4:])}", version)
    return None


_DOCS_APPS = {"document", "spreadsheets", "presentation", "forms", "drawings"}


def _gdrive(host: str, seg: list[str], query: str) -> SourceRef | None:
    if host == "docs.google.com":
        if len(seg) >= 3 and seg[0] in _DOCS_APPS and seg[1] == "d":
            return SourceRef("gdrive", "file", seg[2])
        return None
    if host != "drive.google.com":
        return None
    if len(seg) >= 3 and seg[0] == "file" and seg[1] == "d":
        return SourceRef("gdrive", "file", seg[2])
    if "folders" in seg[:-1] and seg[0] == "drive":
        return SourceRef("gdrive", "folder", seg[seg.index("folders") + 1])
    if seg and seg[0] in ("open", "uc"):
        ids = parse_qs(query).get("id")
        return SourceRef("gdrive", "file", ids[0]) if ids else None
    return None


def _jira(_host: str, seg: list[str], _query: str) -> SourceRef | None:
    # Any host: a Jira site's address is the company's own, and /browse/<KEY>
    # is distinctive enough. to_url needs the base; from_url does not.
    if len(seg) >= 2 and seg[-2] == "browse" and JIRA_KEY.match(seg[-1]):
        return SourceRef("jira", "issue", seg[-1])
    return None

