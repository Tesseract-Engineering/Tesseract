"""
auth.py - credentials for Jira Cloud.

  JIRA_BASE_URL    https://{site}.atlassian.net
  JIRA_EMAIL       the read-only service account's email
  JIRA_API_TOKEN   its API token (id.atlassian.com, Security, API tokens)

Jira Cloud takes the pair as HTTP basic auth on every request.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class BasicAuth:
    base_url: str
    email: str
    token: str

    def headers(self) -> dict[str, str]:
        pair = base64.b64encode(f"{self.email}:{self.token}".encode()).decode("ascii")
        return {"Authorization": f"Basic {pair}"}


def from_env(env: dict[str, str] | None = None) -> BasicAuth:
    env = os.environ if env is None else env
    missing = [k for k in ("JIRA_BASE_URL", "JIRA_EMAIL", "JIRA_API_TOKEN") if not env.get(k, "").strip()]
    if missing:
        raise SystemExit(f"Jira needs {', '.join(missing)} (see .env.example)")
    return BasicAuth(base_url=env["JIRA_BASE_URL"].strip().rstrip("/"), email=env["JIRA_EMAIL"].strip(),
                     token=env["JIRA_API_TOKEN"].strip())
