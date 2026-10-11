"""graph.py - the one read connection to Neo4j that answering uses.

Every query runs inside the configured namespace: `$ns` is always bound, so a
query cannot forget it and read another company's graph.
"""

from __future__ import annotations

from typing import Any

from neo4j import Driver, GraphDatabase

from .config import settings

_driver: Driver | None = None


def driver() -> Driver:
    global _driver
    if _driver is None:
        s = settings()
        # Notifications off: usage properties (askCount) are unknown keys until
        # the first query-log sync, and every read would otherwise log a warning.
        _driver = GraphDatabase.driver(s.neo4j_uri, auth=(s.neo4j_user, s.neo4j_password),
                                       notifications_min_severity="OFF")
    return _driver


def database() -> str:
    return settings().neo4j_database


def namespace() -> str:
    return settings().namespace


def run(cypher: str, **params: Any) -> list[dict]:
    """Rows of `cypher` as dicts, with `$ns` bound to the configured namespace."""
    records = driver().execute_query(cypher, database_=database(), ns=namespace(), **params).records
    return [dict(r) for r in records]


def date(value: Any) -> str:
    """A Neo4j or ISO datetime as YYYY-MM-DD; empty for None."""
    if value is None:
        return ""
    if hasattr(value, "iso_format"):
        value = value.iso_format()
    return str(value)[:10]
