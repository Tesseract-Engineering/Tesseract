"""
auth.py - credentials for the GitHub API.

Two modes, chosen by what the environment holds:

  App mode     GITHUB_APP_ID + GITHUB_APP_PRIVATE_KEY_PATH + GITHUB_INSTALLATION_ID
               The customer-created, read-only GitHub App from app-manifest.json.
               A short-lived JWT buys an installation token, cached until a
               minute before it expires.
  Token mode   GITHUB_TOKEN
               A read-only token, for a sandbox before an App is installed.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import jwt

from ...timeutil import parse_iso

API = "https://api.github.com"
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


class AppAuth:
    def __init__(self, app_id: str, private_key: str, installation_id: str, *,
                 transport: httpx.BaseTransport | None = None, now: Callable[[], float] = time.time):
        self.app_id = app_id
        self.private_key = private_key
        self.installation_id = installation_id
        self.mode = "app"
        self._now = now
        self._http = httpx.Client(base_url=API, headers=HEADERS, transport=transport, trust_env=True, timeout=30)
        self._token: str | None = None
        self._expires = 0.0

    def app_jwt(self) -> str:
        """The App's own JWT: valid 9 minutes, issued 60s in the past for clock skew."""
        issued = int(self._now()) - 60
        return jwt.encode({"iat": issued, "exp": issued + 600, "iss": self.app_id}, self.private_key,
                          algorithm="RS256")

    def headers(self) -> dict[str, str]:
        if self._token is None or self._now() >= self._expires - 60:
            response = self._http.post(f"/app/installations/{self.installation_id}/access_tokens",
                                       headers={"Authorization": f"Bearer {self.app_jwt()}"})
            response.raise_for_status()
            body = response.json()
            self._token = body["token"]
            self._expires = parse_iso(body["expires_at"]).timestamp()
        return {"Authorization": f"Bearer {self._token}"}


class TokenAuth:
    def __init__(self, token: str):
        self.mode = "token"
        self._token = token

    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}"}


def from_env(env: dict[str, str] | None = None) -> AppAuth | TokenAuth:
    env = dict(os.environ if env is None else env)
    if env.get("GITHUB_APP_ID"):
        key = env.get("GITHUB_APP_PRIVATE_KEY") or Path(env["GITHUB_APP_PRIVATE_KEY_PATH"]).read_text()
        return AppAuth(env["GITHUB_APP_ID"], key, env["GITHUB_INSTALLATION_ID"])
    if env.get("GITHUB_TOKEN"):
        return TokenAuth(env["GITHUB_TOKEN"])
    raise SystemExit("GitHub credentials missing: set GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY_PATH and "
                     "GITHUB_INSTALLATION_ID, or GITHUB_TOKEN")
