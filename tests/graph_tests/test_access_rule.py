"""The access rule in Python (graph/swarm_queries.py readable, edge_readable). No database."""

import pytest

from graph.swarm_queries import edge_readable, readable

from .access_cases import CASES


@pytest.mark.parametrize("vis, groups, v, node, _edge", [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_node_rule(vis, groups, v, node, _edge):
    assert readable(vis, groups, v) is node


@pytest.mark.parametrize("vis, groups, v, _node, edge", [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_edge_rule(vis, groups, v, _node, edge):
    assert edge_readable(vis, groups, v) is edge
