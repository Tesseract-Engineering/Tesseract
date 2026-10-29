"""Access control against a real database: samples/run-0 built into Neo4j, read through graph.swarm_queries.

In the sample everything is restricted to groups that Sam and Priya are in: the repo's readers, the
#eng-auth members, the Jira project's browsers. The decision to use OIDC is read from an #eng-auth
thread. An email the sample does not know is outside every group and outside the company.
"""
from pathlib import Path

import pytest

from graph.build_swarm import build
from graph.load import Loader
from graph.swarm_queries import READABLE, contributors, decisions, viewer
from graph.swarm_records import LocalStore, load

pytestmark = pytest.mark.neo4j

SAMPLE = Path(__file__).resolve().parents[2] / "samples" / "run-0"
INSIDER, OUTSIDER = "sam@acme.com", "outsider@example.com"
DECISION = "Use OIDC instead of SAML for staff login"


@pytest.fixture
def ns(fresh_ns):
    proposals, _ = build(load(LocalStore(SAMPLE)), fresh_ns)
    with Loader(fresh_ns) as loader:
        receipt = loader.ingest(proposals)
    assert receipt.rejected == 0, receipt
    assert receipt.accepted > 0, receipt
    return fresh_ns


def test_viewer_membership(neo4j_store, ns):
    sam = viewer(neo4j_store, ns, INSIDER)
    assert sam["member"] is True
    assert "group:slack:T01ACME:channel:C42AUTH:members" in sam["principals"]
    assert viewer(neo4j_store, ns, OUTSIDER)["member"] is False


def test_restricted_decision_only_for_its_group(neo4j_store, ns):
    seen = [row["decision"] for row in decisions(neo4j_store, ns, viewer(neo4j_store, ns, INSIDER))]
    assert DECISION in seen
    hidden = [row["decision"] for row in decisions(neo4j_store, ns, viewer(neo4j_store, ns, OUTSIDER))]
    assert DECISION not in hidden


def test_contributors_never_name_an_unreadable_container(neo4j_store, ns):
    sam, outsider = viewer(neo4j_store, ns, INSIDER), viewer(neo4j_store, ns, OUTSIDER)
    assert contributors(neo4j_store, ns, sam), "the insider sees contributors, so the next check is not vacuous"
    readable = {row["title"] for row in neo4j_store._run(
        f"MATCH (c:Container {{ns: $ns}}) WHERE {READABLE.format(n='c')} RETURN c.title AS title",
        ns=ns, member=outsider["member"], principals=outsider["principals"])}
    named = {row["container"] for row in contributors(neo4j_store, ns, outsider)}
    assert named <= readable, named - readable
