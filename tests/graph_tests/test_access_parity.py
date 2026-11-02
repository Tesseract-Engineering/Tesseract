"""The access rule in Cypher agrees with its Python versions: READABLE and EDGE_READABLE with readable() and
edge_readable() case by case, and viewer() with principals() on the sample graph."""

from pathlib import Path

import pytest

from graph.build_swarm import build
from graph.load import Loader
from graph.swarm_queries import EDGE_READABLE, READABLE, edge_readable, principals, readable, viewer
from graph.swarm_records import LocalStore, load

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


# The sample's people, by every id a group record can name them with (their identity clusters' accounts).
# Sam's Jira account is in the Jira browsers group, but no cluster holds it, so it grants him nothing.
PEOPLE = {"sam@acme.com": ["person:slack:U123SAM", "person:github:slee"],
          "priya@acme.com": ["person:slack:U456PRI", "person:github:priya-r"]}


def test_viewer_and_principals_agree_on_the_sample(neo4j_store, fresh_ns):
    state = load(LocalStore(Path(__file__).resolve().parents[2] / "samples" / "run-0"))
    proposals, _ = build(state, fresh_ns)
    with Loader(fresh_ns) as loader:
        assert loader.ingest(proposals).rejected == 0
    for email, accounts in PEOPLE.items():
        from_graph = viewer(neo4j_store, fresh_ns, email)["principals"]
        from_records = principals(email, accounts, state.groups)
        assert from_graph[:2] == from_records[:2] and sorted(from_graph[2:]) == from_records[2:], email
