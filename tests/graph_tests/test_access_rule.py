"""The access rule in Python (graph/swarm_queries.py readable, edge_readable, principals). No database."""

import pytest

from graph.swarm_queries import GROUP_HOPS, edge_readable, principals, readable

from .access_cases import CASES


@pytest.mark.parametrize("vis, groups, v, node, _edge", [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_node_rule(vis, groups, v, node, _edge):
    assert readable(vis, groups, v) is node


@pytest.mark.parametrize("vis, groups, v, _node, edge", [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_edge_rule(vis, groups, v, _node, edge):
    assert edge_readable(vis, groups, v) is edge


def group(gid, *members):
    return gid, {"id": gid, "members": list(members)}


def test_principals_from_email_and_accounts():
    groups = dict([group("g:repo", "user:sam@acme.com"), group("g:chan", "person:slack:U1"), group("g:other", "user:x@acme.com")])
    assert principals("Sam@Acme.com", ["person:slack:U1"], groups) == [
        "user:sam@acme.com", "domain:acme.com", "g:chan", "g:repo"]


def test_principals_follow_groups_inside_groups_up_to_the_hop_limit():
    chain = dict(group(f"g{i}", f"g{i - 1}" if i else "user:a@x.com") for i in range(GROUP_HOPS + 1))
    held = principals("a@x.com", [], chain)
    assert [f"g{i}" for i in range(GROUP_HOPS)] == sorted(p for p in held if p.startswith("g"))
    assert f"g{GROUP_HOPS}" not in held, "MEMBER_OF*1..GROUP_HOPS stops here"


def test_principals_survive_a_cycle_and_an_unknown_person():
    groups = dict([group("g:a", "user:a@x.com", "g:b"), group("g:b", "g:a")])
    assert principals("a@x.com", [], groups) == ["user:a@x.com", "domain:x.com", "g:a", "g:b"]
    assert principals("nobody@y.com", [], groups) == ["user:nobody@y.com", "domain:y.com"]
