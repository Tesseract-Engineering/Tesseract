"""READABLE and EDGE_READABLE in Cypher agree with readable() and edge_readable() in Python, case by case."""

import pytest

from graph.swarm_queries import EDGE_READABLE, READABLE, edge_readable, readable

from .access_cases import CASES

pytestmark = pytest.mark.neo4j


@pytest.mark.parametrize("vis, groups, v", [c[1:4] for c in CASES], ids=[c[0] for c in CASES])
def test_cypher_and_python_agree(neo4j_store, fresh_ns, vis, groups, v):
    access = {k: val for k, val in {"visibility": vis, "aclGroups": groups}.items() if val is not None}
    neo4j_store.driver.execute_query(
        "CREATE (a:ParityCase {ns: $ns, id: 'a'}) SET a += $access "
        "CREATE (b:ParityCase {ns: $ns, id: 'b'}) "
        "CREATE (b)-[r:PARITY]->(b) SET r += $access", ns=fresh_ns, access=access,
        database_=neo4j_store.database)
    rows = neo4j_store.read(
        f"MATCH (n:ParityCase {{ns: $ns, id: 'a'}}), (:ParityCase {{ns: $ns, id: 'b'}})-[r:PARITY]->() "
        # A NULL from the rule (no visibility) is what a WHERE clause treats as false.
        f"RETURN coalesce({READABLE.format(n='n')}, false) AS node, "
        f"coalesce({EDGE_READABLE.format(n='r')}, false) AS edge",
        ns=fresh_ns, member=v["member"], principals=v["principals"])
    assert rows == [{"node": readable(vis, groups, v), "edge": edge_readable(vis, groups, v)}]
