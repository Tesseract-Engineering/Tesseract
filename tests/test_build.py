"""The builder, against tiny hand-made bundles. No database: load.Loader is
replaced by a fake that keeps the proposals. Each test is a rule the builder
must hold for ANY adapter."""

from __future__ import annotations

import contextlib
import io
import json
import types
from collections import Counter
from pathlib import Path

import pytest

from tesseract import adapters, build, load
from tesseract.adapters import workspace
from tesseract.bundle import Bundle
from tesseract.connectors import SourceRecord


class FakeLoader:
    written: list = []

    def __init__(self, ns, **_):
        self.ns = ns
        self.store = types.SimpleNamespace(counts=lambda ns: {})

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def wipe(self):
        return {}

    def ingest(self, proposals):
        FakeLoader.written = list(proposals)
        return types.SimpleNamespace(reasons=Counter(), __str__=lambda self: "fake")


@pytest.fixture
def run_build(tmp_path, monkeypatch):
    """Run the builder on a Bundle factory; return the proposals it would write."""
    monkeypatch.setattr(load, "Loader", FakeLoader)

    def run(factory):
        module = types.SimpleNamespace(NAME="test", looks_like=lambda root: True, load=factory)
        monkeypatch.setattr(adapters, "get", lambda name: module)
        with contextlib.redirect_stdout(io.StringIO()):
            build.main(["--root", str(tmp_path), "--adapter", "test"])
        return FakeLoader.written
    return run


def edges(proposals, kind):
    return [(p.src_id, p.dst_id, p.props) for p in proposals if getattr(p, "src_id", None) and p.type == kind]


def person(name, handle, source="slack"):
    return {"source": source, "handle": handle, "name": name, "email": ""}


def messy(root: Path) -> Bundle:
    b = Bundle(name="test", root=root, speaker_sources={"chat"})
    b.services = {"billing-service": {
        "id": "service:billing-service", "name": "billing-service",
        "props": {"name": "billing-service", "kind": "microservice"}, "source": "org", "producer": "test"}}
    b.teams = [{"id": f"team:{t}", "name": t, "source": "org", "source_id": t, "producer": "test",
                "props": {"name": t}} for t in ("billing", "platform")]
    # Two sources disagree about the owner.
    b.ownership = [
        {"team": "team:billing", "service": "billing-service", "basis": "org-chart",
         "source": "org", "source_id": "x", "producer": "test", "quote": ""},
        {"team": "team:platform", "service": "billing-service", "basis": "service-catalog",
         "source": "catalog", "source_id": "y", "producer": "test", "quote": "owner: platform"}]
    b.people = [{"name": "Ada Lovelace", "empId": "emp-001", "status": "employee"},
                {"name": "Alan Turing", "empId": "emp-002", "status": "employee"}]
    b.memberships = [
        {"empId": "emp-001", "team": "team:billing", "teamLabel": "billing", "via": "org-chart",
         "producer": "test", "rank": 0},
        # A lower-ranked source says otherwise; the org chart wins.
        {"name": "Ada Lovelace", "team": "team:platform", "teamLabel": "platform", "via": "directory",
         "producer": "test", "rank": 1},
        {"name": "Alan Turing", "team": None, "teamLabel": "Platfrom Eng", "via": "directory",
         "producer": "test", "rank": 1}]
    b.records = [
        # One chat handle used by two different people.
        SourceRecord(kind="document", source="chat", id="thread:1", title="outage",
                     text="Ada Lovelace: billing-service is down\nAlan Turing: on it", url="",
                     created_at="2026-01-01",
                     people=[person("Ada Lovelace", "shared.bot"), person("Alan Turing", "shared.bot")]),
        SourceRecord(kind="issue", source="tracker", id="ticket:T-1", title="Billing-service refunds fail",
                     text="See thread.", url="", created_at="2026-01-02",
                     people=[person("Ada Lovelace", "ada", "tracker")],
                     meta={"key": "T-1", "declaredService": "billing-service", "assigneeName": "Alan Turing",
                           "customer": "Acme Corp"}),
        SourceRecord(kind="document", source="docs", id="doc:runbook", title="runbook",
                     text="**Owner:** Alan Turing\n\n## Steps\nAsk Ada Lovelace.", url="", created_at="2026-01-03"),
    ]
    return b


def test_an_empty_bundle_builds_without_error(run_build):
    assert run_build(lambda root: Bundle(name="test", root=root)) == []


def test_identity_ownership_and_extraction_rules(run_build):
    proposals = run_build(messy)
    nodes = Counter(p.type for p in proposals if not getattr(p, "src_id", None))
    people = {p.id: p.props.get("name") for p in proposals if p.type == "Person"}
    ada = next(i for i, n in people.items() if n == "Ada Lovelace")
    alan = next(i for i, n in people.items() if n == "Alan Turing")

    # a shared handle does not weld two people into one, and is queued for a human
    assert sorted(people.values()) == ["Ada Lovelace", "Alan Turing"]
    assert any(p.type == "ReviewItem" and "shared.bot" in p.id for p in proposals)
    # conflicting owners are both kept, each with its basis
    assert sorted(e[2]["basis"] for e in edges(proposals, "OWNS")) == ["org-chart", "service-catalog"]
    # the better-ranked membership wins; a membership naming no team is not invented
    assert [(e[1], e[2]["via"]) for e in edges(proposals, "MEMBER_OF")] == [("team:billing", "org-chart")]
    # declared fields
    assert edges(proposals, "ABOUT") == [("ticket:T-1", "service:billing-service", {"basis": "declared-field"})]
    assert [p.provenance.source for p in proposals if p.type == "ASSIGNED_TO"] == ["tracker"]
    assert nodes["Customer"] == 1 and len(edges(proposals, "FOR_CUSTOMER")) == 1
    # extraction
    mentions = {(e[0], e[1]) for e in edges(proposals, "MENTIONS")}
    assert ("thread:1", ada) not in mentions, "a speaker is not also mentioned in their own line"
    assert ("doc:runbook", ada) in mentions
    assert any(p.type == "MENTIONS" and p.dst_id == "service:billing-service" and p.provenance.quote
               for p in proposals)
    assert [(e[1], e[2]["role"]) for e in edges(proposals, "CREDITS")] == [(alan, "owner")]


def test_the_bundle_reports_its_own_mistakes():
    bad = Bundle(name="test", root=Path("."))
    bad.records = [SourceRecord(kind="issue", source="x", id="dup", title="", text="", url="", created_at="")] * 2
    bad.ownership = [{"team": "team:ghost", "service": "nothing", "basis": "?"}]
    problems = bad.problems()
    assert any("more than once" in p for p in problems)
    assert any("unknown team" in p for p in problems)
    assert any("unknown service" in p for p in problems)


def test_workspace_adapter_reads_the_standard_layout(tmp_path):
    (tmp_path / "people").mkdir()
    (tmp_path / "people" / "directory.json").write_text(json.dumps({
        "employees": [
            {"id": "e1", "name": "Ada Lovelace", "email": "ada@example.com", "slack": "ada", "team": "Payments"},
            {"id": "e2", "name": "Old Timer", "status": "alumni", "departed_at": "2025-01-01", "team": "Payments"}],
        "private_slack_channels": ["security"]}))
    for channel in ("payments", "security"):
        (tmp_path / "slack" / channel).mkdir(parents=True)
        (tmp_path / "slack" / channel / "2026-01-01.json").write_text(
            json.dumps([{"user": "U1", "ts": "1767225600.0001", "text": f"{channel} note"}]))
    (tmp_path / "slack" / "users.json").write_text(
        json.dumps([{"id": "U1", "name": "ada", "profile": {"real_name": "Ada Lovelace"}}]))
    (tmp_path / "company.json").write_text(json.dumps({"jira_base_url": "https://acme.atlassian.net/"}))
    (tmp_path / "jira").mkdir()
    (tmp_path / "jira" / "PAY.csv").write_text(
        "Issue key,Summary,Description,Created,Updated,Reporter,Assignee,Status\n"
        "PAY-1,Refund retries,Retry with idempotency keys,2026-01-02,2026-02-03,ada,Ada Lovelace,Done\n")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "adr-001.md").write_text("# ADR 001\n**Owner:** Ada Lovelace\n")

    assert workspace.looks_like(tmp_path) and adapters.detect(tmp_path) is workspace
    bundle = workspace.load(tmp_path)
    records = {r.id: r for r in bundle.records}
    assert bundle.problems() == []
    assert [t["name"] for t in bundle.teams] == ["Payments"]
    assert [m["name"] for m in bundle.memberships] == ["Ada Lovelace"], "alumni are not placed on a team"
    assert {p["status"] for p in bundle.people} == {"employee", "alumni"}
    ticket = records["ticket:PAY-1"]
    assert ticket.meta["assigneeName"] == "Ada Lovelace" and ticket.meta["path"] == "jira/PAY.csv"
    assert ticket.ref.uri() == "forge://jira/issue/PAY-1" and ticket.meta["updatedAt"] == "2026-02-03"
    assert ticket.url == "https://acme.atlassian.net/browse/PAY-1"
    assert records["doc:adr-001.md"].meta["path"] == "docs/adr-001.md"
    security = next(r for r in bundle.records if r.id.startswith("slack:security:"))
    assert security.visibility == "private" and security.acl_groups == ["slack:security"]
    _, unread = bundle.coverage()
    assert unread == [], f"every file in the layout is read: {unread}"


def test_generic_adapter_is_the_fallback(tmp_path):
    (tmp_path / "notes.md").write_text("hello")
    assert adapters.detect(tmp_path).NAME == "generic"
