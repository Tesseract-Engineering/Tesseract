"""End to end against a real Neo4j: build a small company from the workspace
layout into a throwaway namespace, then answer, redact, draft and log.

Needs NEO4J_URI / NEO4J_PASSWORD; skipped when Neo4j is unreachable. Writes
only to the namespace `tesseract:test` and deletes it afterwards.
"""

from __future__ import annotations

import contextlib
import io
import json
import os

import pytest

NS = "tesseract:test"
pytestmark = pytest.mark.neo4j


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture(scope="module")
def company(tmp_path_factory):
    root = tmp_path_factory.mktemp("company")
    _write(root / "people" / "directory.json", json.dumps({"employees": [
        {"id": "e1", "name": "Ada Lovelace", "email": "ada@example.com", "slack": "ada", "team": "Payments"},
        {"id": "e2", "name": "Grace Hopper", "email": "grace@example.com", "slack": "grace", "team": "Security"},
        {"id": "e3", "name": "Alan Turing", "email": "alan@example.com", "slack": "alan", "team": "Payments"},
        {"id": "e4", "name": "Old Timer", "status": "alumni", "departed_at": "2025-06-30"}]}))
    _write(root / "docs" / "payments" / "refund-retries.md",
           "# Refund retry pipeline\n\n**Owner:** Ada Lovelace\n**Author:** Old Timer\n\n"
           "The refund retry pipeline retries failed refunds with idempotency keys, "
           "backing off for up to 24 hours before paging the payments on-call.\n")
    _write(root / "docs" / "handbook" / "leave.md",
           "# Parental leave policy\n\nEvery employee gets 16 weeks of paid parental leave.\n")
    _write(root / "sharepoint" / "security" / "runbooks" / "break-glass.md",
           "# Break-glass procedure\n\n**Owner:** Grace Hopper\n\nThe break-glass vault code rotates weekly.\n")
    _write(root / "sharepoint" / "security" / "runbooks" / "break-glass.md.meta.json",
           json.dumps({"author": "Grace Hopper", "site": "security", "library": "runbooks",
                       "sensitivity": "Confidential"}))
    return root


@pytest.fixture(scope="module")
def graph_ready(company):
    from tesseract import build, config, graph

    os.environ["TESSERACT_NS"] = NS
    os.environ["TESSERACT_STATE"] = str(company.parent / "state")
    config.settings.cache_clear()
    try:
        graph.driver().verify_connectivity()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"Neo4j unreachable: {type(e).__name__}")
    with contextlib.redirect_stdout(io.StringIO()):
        build.main(["--root", str(company), "--adapter", "workspace", "--ns", NS])
    yield
    graph.run("MATCH (n {ns: $ns}) DETACH DELETE n")
    os.environ.pop("TESSERACT_NS", None)
    os.environ.pop("TESSERACT_STATE", None)
    config.settings.cache_clear()


@pytest.fixture(autouse=True)
def isolated_settings():
    """Override the unit-test fixture: these tests share one built namespace."""
    yield


def test_build_writes_people_documents_and_credits(graph_ready):
    from tesseract import graph

    people = {r["name"] for r in graph.run("MATCH (p:Person {ns: $ns}) RETURN p.name AS name")}
    assert {"Ada Lovelace", "Grace Hopper", "Old Timer"} <= people
    credits = graph.run("MATCH (d:Document {ns: $ns})-[c:CREDITS]->(p:Person) RETURN d.id AS d, p.name AS p")
    assert {"d": "doc:payments/refund-retries.md", "p": "Ada Lovelace"} in credits


def test_answer_is_cited_and_grounded(graph_ready):
    from tesseract.answer import answer

    out = answer("How does the refund retry pipeline handle failed refunds?")
    assert out["verdict"] == "INFO", out.get("reason")
    assert any(c["node_id"] == "doc:payments/refund-retries.md" for c in out["claims"])


def test_an_unrelated_question_is_not_found(graph_ready):
    from tesseract.answer import answer

    out = answer("What is the capital of the moon base?")
    assert out["verdict"] == "NOT_FOUND" and out["claims"] == []


def test_a_restricted_document_is_counted_never_quoted(graph_ready):
    from tesseract.answer import answer

    out = answer("What is the break-glass vault code procedure?", viewer={"name": "eng", "acl_groups": []})
    assert "rotates weekly" not in json.dumps(out)
    assert all(c["node_id"] != "sharepoint:security/runbooks/break-glass.md" for c in out["claims"])

    security = answer("What is the break-glass vault code procedure?",
                      viewer={"name": "sec", "acl_groups": ["sharepoint:security"]})
    assert any(c["node_id"] == "sharepoint:security/runbooks/break-glass.md" for c in security["claims"])


def test_precedent_question_drafts_to_the_current_owner(graph_ready):
    from tesseract.answer import answer

    out = answer("Has anyone already built a refund retry pipeline?", asker="Alan Turing")
    assert out["verdict"] == "INFO", (out.get("reason"), out["searched"])
    hands = out.get("hands") or {}
    assert [o["name"] for o in hands.get("owners", [])] == ["Ada Lovelace"]
    assert [a["name"] for a in hands.get("skipped_alumni", [])] == ["Old Timer"]
    email = next(a for a in out["proposed_actions"] if a["type"] == "email.draft")
    assert email["payload"]["to"] == ["ada@example.com"]


def test_usage_reorders_never_admits(graph_ready, monkeypatch):
    from tesseract import answer as answer_mod

    assert answer_mod.usage_boost(0) == 0 and answer_mod.usage_boost(10**9) == answer_mod.USAGE_CAP
    monkeypatch.setattr(answer_mod, "_usage",
                        lambda ids: {i: {"askCount": 10**6, "lastAskedAt": "2026-10-10"} for i in ids if i})
    assert answer_mod.answer("What is the capital of the moon base?")["verdict"] == "NOT_FOUND"


def test_api_routes(graph_ready):
    from tesseract import api

    code, health = api.route("GET", "/health", {})
    assert code == 200 and health["neo4j"] is True
    code, company = api.route("GET", "/company", {})
    assert code == 200 and company["viewers"]
    code, doc = api.route("GET", "/doc", {}, {"node_id": "sharepoint:security/runbooks/break-glass.md"})
    assert code == 403 and "text" not in doc
    code, out = api.route("POST", "/ask", {"question": "What is our parental leave policy?"})
    assert code == 200 and out["verdict"] in ("INFO", "NOT_FOUND")
    code, _ = api.route("GET", "/usage/top", {}, {"limit": "5"})
    assert code == 200
