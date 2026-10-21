"""
acl.py - permissions travel with data.

The Acl model already refuses a malformed ACL. This stage is the run-level
rule on top: an item whose connector could not produce an ACL is not written,
and is counted so the manifest shows it. ACL coverage is a hard-fail check on
every run.
"""

from __future__ import annotations

from ..records import Acl


class MissingAcl(ValueError):
    pass


def require_acl(item_id: str, acl: Acl | None) -> Acl:
    if acl is None:
        raise MissingAcl(f"{item_id} has no acl; it is not written")
    return acl


def coverage(written: int, rejected_for_acl: int) -> float:
    """Share of fetched items that carried an ACL. 1.0 is the only passing value."""
    total = written + rejected_for_acl
    return 1.0 if total == 0 else written / total
