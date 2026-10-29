"""Shared fixtures: a Neo4j store and a namespace of its own for each test that asks for one."""
import os
import uuid

import pytest


@pytest.fixture(scope="session")
def neo4j_store():
    if not (os.environ.get("NEO4J_URI") and os.environ.get("NEO4J_PASSWORD")):
        pytest.skip("NEO4J_URI and NEO4J_PASSWORD not set")
    from graph.store import Neo4jStore  # imported late: graph.store reads NEO4J_URI at import
    store = Neo4jStore(uri=os.environ["NEO4J_URI"])
    try:
        store.driver.verify_connectivity()
    except Exception as e:                  # no database answering: skip, as the s3 tests do
        pytest.skip(f"Neo4j not reachable: {e}")
    yield store
    store.close()


@pytest.fixture
def fresh_ns(neo4j_store):
    ns = f"test:{uuid.uuid4().hex[:12]}"
    yield ns
    neo4j_store.wipe_namespace(ns)
