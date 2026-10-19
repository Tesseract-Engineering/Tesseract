"""config.py - every setting Tesseract reads, in one place.

Settings come from the environment (see .env.example). What is specific to one
company - its name, email domain, viewer presets - comes from a company file
named by TESSERACT_COMPANY, so the engine itself never names a company:

    {
      "name": "Example Co",
      "email_domain": "example.com",
      "profile_prefix": "doc:company-profile/",
      "default_viewer": "employee",
      "viewers": {
        "employee": {"label": "Employee", "acl_groups": []},
        "legal":    {"label": "Legal",    "acl_groups": ["sharepoint:legal"]}
      },
      "users": [
        {"name": "Jane Doe", "team": "Payments", "viewer": "employee"},
        {"name": "Sam Lee",  "team": "Legal",    "viewer": "legal"}
      ],
      "suggestions": ["Who owns the billing service?"],
      "jira_base_url": "https://example.atlassian.net"
    }

`jira_base_url` is the Jira site tickets link to (<base>/browse/<KEY>); without
it tickets carry no link. `suggestions` are starter questions the UI shows. `users` is who the UI offers to sign in as, each mapped to a viewer preset.
It stands in for single sign-on: in a real deployment the identity provider
says who someone is, and their groups say which preset applies.

When TESSERACT_COMPANY is unset, <TESSERACT_DATA>/company.json is used if it
exists, so one mounted folder can carry both the exports and their company.

Every key is optional. Without a company file there is one viewer, an
employee with no restricted groups, and contacts with no known address are
left without one rather than guessed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    """Read .env at the repository root into os.environ. Real environment
    variables always win, so CI and one-off overrides still work."""
    path = ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        name, value = name.strip(), value.strip().strip('"').strip("'")
        if name and name not in os.environ:
            os.environ[name] = value


_load_dotenv()


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


@dataclass(frozen=True)
class Viewer:
    name: str
    label: str
    acl_groups: tuple[str, ...] = ()

    def as_dict(self) -> dict:
        return {"name": self.name, "label": self.label, "acl_groups": list(self.acl_groups)}


@dataclass(frozen=True)
class Company:
    name: str = "Company"
    email_domain: str = ""
    profile_prefix: str = "doc:company-profile/"
    default_viewer: str = "employee"
    viewers: dict[str, Viewer] = field(
        default_factory=lambda: {"employee": Viewer("employee", "Employee (no restricted groups)")})
    users: tuple[dict, ...] = ()
    suggestions: tuple[str, ...] = ()
    jira_base_url: str = ""

    @classmethod
    def from_file(cls, path: Path) -> "Company":
        data = json.loads(path.read_text(encoding="utf-8"))
        viewers = {k: Viewer(k, str(v.get("label") or k), tuple(v.get("acl_groups") or ()))
                   for k, v in (data.get("viewers") or {}).items()}
        base = cls()
        viewers = viewers or base.viewers
        default = data.get("default_viewer") or next(iter(viewers))
        if default not in viewers:
            raise ValueError(f"{path}: default_viewer {default!r} is not one of {sorted(viewers)}")
        users = []
        for u in data.get("users") or []:
            if not u.get("name"):
                continue
            viewer = u.get("viewer") or default
            if viewer not in viewers:
                raise ValueError(f"{path}: user {u['name']!r} has unknown viewer {viewer!r}")
            users.append({"name": str(u["name"]), "team": str(u.get("team") or ""), "viewer": viewer})
        return cls(name=str(data.get("name") or base.name),
                   email_domain=str(data.get("email_domain") or ""),
                   profile_prefix=str(data.get("profile_prefix") or base.profile_prefix),
                   default_viewer=default, viewers=viewers, users=tuple(users),
                   suggestions=tuple(str(q) for q in data.get("suggestions") or ()),
                   jira_base_url=str(data.get("jira_base_url") or "").rstrip("/"))

    def sign_in_users(self) -> list[dict]:
        """The configured users, or one per viewer preset when none are listed."""
        if self.users:
            return [dict(u) for u in self.users]
        return [{"name": v.label, "team": "", "viewer": k} for k, v in self.viewers.items()]


@dataclass(frozen=True)
class Settings:
    neo4j_uri: str
    neo4j_user: str
    neo4j_password: str
    neo4j_database: str
    namespace: str
    company: Company
    state_dir: Path
    outbox: Path


@lru_cache(maxsize=1)
def settings() -> Settings:
    company_file = env("TESSERACT_COMPANY")
    if not company_file and env("TESSERACT_DATA"):
        beside_data = Path(env("TESSERACT_DATA")) / "company.json"
        company_file = str(beside_data) if beside_data.is_file() else ""
    company = Company.from_file(Path(company_file)) if company_file else Company()
    state = Path(env("TESSERACT_STATE", str(ROOT / "var")))
    return Settings(
        neo4j_uri=env("NEO4J_URI", "neo4j://localhost:7687"),
        neo4j_user=env("NEO4J_USER", "neo4j"),
        neo4j_password=env("NEO4J_PASSWORD"),
        neo4j_database=env("NEO4J_DATABASE", "neo4j"),
        namespace=env("TESSERACT_NS", "tesseract:default"),
        company=company,
        state_dir=state,
        outbox=Path(env("TESSERACT_OUTBOX", str(state / "outbox"))),
    )
