"""
directory.py - the workspace's users, for names, emails and bot checks.

Filled once per run, from users.json in an export or users.list from the API.
Both give the same user objects, so both connectors build it the same way.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

# Slackbot is a bot that the API does not mark as one.
_ALWAYS_BOTS = {"USLACKBOT"}


@dataclass(frozen=True)
class User:
    id: str
    name: str | None
    email: str | None
    is_bot: bool
    deleted: bool


@dataclass
class Directory:
    users: dict[str, User] = field(default_factory=dict)

    def load(self, members: Iterable[dict[str, Any]]) -> None:
        for m in members:
            profile = m.get("profile") or {}
            name = (profile.get("real_name") or m.get("real_name") or profile.get("display_name")
                    or m.get("name") or None)
            email = profile.get("email")
            self.users[m["id"]] = User(
                id=m["id"], name=name, email=email.lower() if email else None,
                is_bot=bool(m.get("is_bot") or m.get("is_app_user")) or m["id"] in _ALWAYS_BOTS,
                deleted=bool(m.get("deleted")))

    def get(self, user_id: str | None) -> User | None:
        return self.users.get(user_id) if user_id else None

    def name(self, user_id: str | None) -> str | None:
        user = self.get(user_id)
        return user.name if user else None
