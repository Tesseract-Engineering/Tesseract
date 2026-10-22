"""
auth.py - a Google service account's credentials for the Drive API.

The customer creates a service account in a Google Cloud project with the
Drive API enabled, downloads its JSON key, and adds the account's email to
the shared drives (or shares the folders) in scope as a Viewer. The key
signs a short-lived JWT that buys an access token for the read-only Drive
scope, cached until a minute before it expires.

  GDRIVE_SERVICE_ACCOUNT_KEY_PATH   the JSON key file (mounted read-only)
  GDRIVE_SERVICE_ACCOUNT_KEY        or the key's JSON itself, from a secret store
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import jwt

TOKEN_URI = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/drive.readonly"


class ServiceAccountAuth:
    def __init__(self, key: dict, *, scope: str = SCOPE, transport: httpx.BaseTransport | None = None,
                 now: Callable[[], float] = time.time):
        self.email = key["client_email"]
        self._private_key = key["private_key"]
        self.scope = scope
        self._now = now
        self._http = httpx.Client(transport=transport, trust_env=True, timeout=30)
        self._token: str | None = None
        self._expires = 0.0

    def assertion(self) -> str:
        """The signed request for a token: valid one hour, the most Google allows."""
        issued = int(self._now())
        return jwt.encode({"iss": self.email, "scope": self.scope, "aud": TOKEN_URI,
                           "iat": issued, "exp": issued + 3600}, self._private_key, algorithm="RS256")

    def headers(self) -> dict[str, str]:
        if self._token is None or self._now() >= self._expires - 60:
            response = self._http.post(TOKEN_URI, data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": self.assertion()})
            response.raise_for_status()
            body = response.json()
            self._token = body["access_token"]
            self._expires = self._now() + float(body.get("expires_in", 3600))
        return {"Authorization": f"Bearer {self._token}"}


def from_env(env: dict[str, str] | None = None) -> ServiceAccountAuth:
    env = dict(os.environ if env is None else env)
    if env.get("GDRIVE_SERVICE_ACCOUNT_KEY"):
        return ServiceAccountAuth(json.loads(env["GDRIVE_SERVICE_ACCOUNT_KEY"]))
    path = Path(env.get("GDRIVE_SERVICE_ACCOUNT_KEY_PATH") or "")
    if path.is_file():
        return ServiceAccountAuth(json.loads(path.read_text()))
    # swarm/compose.yaml always sets the path, so a missing file means no key yet.
    raise SystemExit("Drive credentials missing: put the service account's JSON key at "
                     "GDRIVE_SERVICE_ACCOUNT_KEY_PATH (data/gdrive/service-account.json in the compose "
                     "stack), or set GDRIVE_SERVICE_ACCOUNT_KEY")
