"""
drive.py - Grant, run as the file's owner.

The swarm's Drive service account is a Viewer: it can read files but not share them, by design. So an
owner who clicks Grant connects their own Google account once (OAuth for a desktop app, loopback
redirect, PKCE), and the bot shares each file as them: reader, expiring after the requested days,
no notification email (the requester hears from the bot).

The OAuth client is a "Desktop app" client in the swarm's Google Cloud project, its JSON in
data/gdrive/oauth-client.json (git-ignored). The consent screen stays in Testing with the sandbox's
four accounts as test users. The account that consents must be the owner's own: a different Google
account is refused, since it could share nothing the owner could not.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
USERINFO = "https://openidconnect.googleapis.com/v1/userinfo"
SCOPES = "openid email https://www.googleapis.com/auth/drive"


class DriveError(RuntimeError):
    pass


class Drive:
    def __init__(self, client_file: str | Path, port: int):
        self.client_file = Path(client_file)
        self.port = port
        self.redirect = f"http://127.0.0.1:{port}/google/callback"
        self._client: dict | None = None

    @property
    def configured(self) -> bool:
        return self.client_file.exists()

    @property
    def client(self) -> dict:
        if self._client is None:
            raw = json.loads(self.client_file.read_text(encoding="utf-8"))
            self._client = raw.get("installed") or raw.get("web") or raw
        return self._client

    # -- connecting an owner ------------------------------------------------------------------ #

    @staticmethod
    def verifier() -> str:
        return secrets.token_urlsafe(48)

    def auth_url(self, state: str, verifier: str, email: str) -> str:
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        return AUTH_URI + "?" + urlencode({
            "client_id": self.client["client_id"], "redirect_uri": self.redirect, "response_type": "code",
            "scope": SCOPES, "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
            "access_type": "offline", "prompt": "consent", "login_hint": email})

    def exchange(self, code: str, verifier: str) -> tuple[str, str]:
        """Code -> (the consenting account's email, refresh token)."""
        r = httpx.post(TOKEN_URI, data={"code": code, "client_id": self.client["client_id"],
                                        "client_secret": self.client.get("client_secret", ""),
                                        "redirect_uri": self.redirect, "grant_type": "authorization_code",
                                        "code_verifier": verifier}, timeout=20)
        if r.status_code != 200:
            raise DriveError(f"token exchange failed ({r.status_code})")
        tok = r.json()
        info = httpx.get(USERINFO, headers={"Authorization": f"Bearer {tok['access_token']}"}, timeout=20).json()
        if not info.get("email_verified"):
            raise DriveError("Google did not confirm that account's email")
        if not tok.get("refresh_token"):
            raise DriveError("Google returned no refresh token")
        return info["email"].lower(), tok["refresh_token"]

    def _access(self, refresh_token: str) -> str:
        r = httpx.post(TOKEN_URI, data={"refresh_token": refresh_token, "client_id": self.client["client_id"],
                                        "client_secret": self.client.get("client_secret", ""),
                                        "grant_type": "refresh_token"}, timeout=20)
        if r.status_code != 200:
            raise DriveError("the owner's Google connection has expired; connect again")
        return r.json()["access_token"]

    # -- sharing ------------------------------------------------------------------------------- #

    def share(self, refresh_token: str, item_ids: list[str], email: str, expires: datetime) -> list[dict]:
        """Share each Drive item with `email` as reader. One result per item: ok, and whether the
        expiry stuck (Drive refuses expiries on some files, e.g. in shared drives)."""
        token = self._access(refresh_token)
        out = []
        for iid in item_ids:
            fid = iid.removeprefix("gdrive:file:")
            body = {"type": "user", "role": "reader", "emailAddress": email,
                    "expirationTime": expires.strftime("%Y-%m-%dT%H:%M:%SZ")}
            url = f"https://www.googleapis.com/drive/v3/files/{fid}/permissions"
            params = {"sendNotificationEmail": "false", "supportsAllDrives": "true"}
            headers = {"Authorization": f"Bearer {token}"}
            r = httpx.post(url, params=params, json=body, headers=headers, timeout=20)
            expiry = True
            if r.status_code in (400, 403) and "expiration" in r.text.lower():
                body.pop("expirationTime")
                expiry = False
                r = httpx.post(url, params=params, json=body, headers=headers, timeout=20)
            out.append({"id": iid, "ok": r.status_code == 200, "expiry": expiry,
                        "error": None if r.status_code == 200 else _reason(r)})
        return out


def _reason(r: httpx.Response) -> str:
    try:
        return r.json()["error"]["message"]
    except Exception:
        return f"HTTP {r.status_code}"


def serve(drive: Drive, on_code: Callable[[str, str], str]) -> ThreadingHTTPServer:
    """The loopback page Google redirects to. on_code(state, code) returns the text to show."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            if url.path != "/google/callback":
                self.send_error(404)
                return
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            if "error" in q:
                msg = "Google sign-in was cancelled. You can close this tab."
            else:
                try:
                    msg = on_code(q.get("state", ""), q.get("code", ""))
                except Exception as e:     # shown to the person who clicked; details stay in the log
                    msg = f"Could not connect: {e}"
            page = (f"<!doctype html><meta charset=utf-8><title>Tesseract</title>"
                    f"<body style='font:15px system-ui;margin:40px'><h3>Tesseract</h3><p>{_html(msg)}</p>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page.encode("utf-8"))

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", drive.port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
