"""
acl.py - permissions travel with data.

The Acl model already refuses a malformed ACL. This stage is the run-level
rule on top: an item whose connector could not produce an ACL is not written,
and is counted so the manifest shows it: acl_coverage is the share of
fetched items that had an ACL, and rejections says how many were refused for
each reason. A written item without an ACL is a hard-fail check on every run
(checks.py); a fetched one without an ACL is reported, not fatal, since a
source can hide one file's permissions without anything being wrong.
"""

from __future__ import annotations

from ..records import Acl


class MissingAcl(ValueError):
    pass


def require_acl(item_id: str, acl: Acl | None) -> Acl:
    if acl is None:
        raise MissingAcl(f"{item_id} has no acl; it is not written")
    return acl


def coverage(with_acl: int, rejected_for_acl: int) -> float:
    """Share of fetched items that carried an ACL."""
    total = with_acl + rejected_for_acl
    return 1.0 if total == 0 else with_acl / total
