"""Owner resolution and drafting, against a fake graph (no Neo4j)."""

from tesseract import hands


def fake_run(credits=(), assigned=(), authored=(), speakers=(), contacts=()):
    tables = {hands.Q_CREDITS: credits, hands.Q_ASSIGNED: assigned, hands.Q_AUTHORED: authored,
              hands.Q_SPEAKERS: speakers, hands.Q_CONTACTS: contacts}

    def run(cypher, **params):
        return list(tables.get(cypher, ()))
    return run


def credit(doc, name, role="owner", status="employee", departed=None, quote="Owner: X"):
    return {"doc": doc, "name": name, "role": role, "status": status, "departed": departed, "quote": quote}


def claim(node_id, title="A doc", source="docs", quote="a quote", date="2026-10-01", restricted=False):
    return {"node_id": node_id, "title": title, "source": source, "quote": quote, "date": date,
            **({"restricted": True} if restricted else {})}


def test_contact_prefers_an_account_then_the_company_convention():
    row = {"emails": ["owen@corp.example"], "handles": ["owen.o"], "title": "Staff Engineer"}
    assert hands.contact("Owen Owner", row, domain="example.com") == {
        "name": "Owen Owner", "email": "owen@corp.example", "slack": "@owen.o",
        "title": "Staff Engineer", "email_basis": "account"}
    c = hands.contact("Zed Nobody", None, domain="example.com")
    assert (c["email"], c["slack"], c["email_basis"]) == ("zed.nobody@example.com", "@zed.nobody", "convention")
    assert hands.contact("Zed Nobody", None, domain="")["email_basis"] == "unknown"


def test_contacts_come_from_the_graph():
    run = fake_run(contacts=[{"name": "Owen Owner", "emails": ["o@x.io"], "handles": [], "title": ""}])
    reach = hands.contacts(run, ["Owen Owner", "Ada Author"])
    assert reach["Owen Owner"]["email"] == "o@x.io"
    assert reach["Ada Author"]["email_basis"] in ("convention", "unknown")


def test_header_owner_outranks_author_and_alumni_are_skipped_and_named():
    run = fake_run(credits=[credit("d1", "Owen Owner", "owner"), credit("d1", "Ada Author", "author"),
                            credit("d1", "Kiran Rao", "author", "alumni", "2026-08-14T00:00:00")])
    owners, alumni = hands.resolve_owners(run, ["d1"])
    assert [o["name"] for o in owners] == ["Owen Owner", "Ada Author"]
    assert alumni == [{"name": "Kiran Rao", "departed": "2026-08-14", "doc": "d1"}]


def test_asker_is_never_their_own_owner_and_thread_asker_is_not_an_answerer():
    first = "Chris Asker: how do I get access?"
    run = fake_run(speakers=[{"doc": "thread:x", "name": n, "first_line": first}
                             for n in ("Chris Asker", "Amara Answerer", "Nathan Wu")])
    owners, _ = hands.resolve_owners(run, ["thread:x"], exclude={"Nathan Wu"})
    assert [o["name"] for o in owners] == ["Amara Answerer"]


def test_access_to_a_restricted_doc_never_leaks_its_text_or_title():
    run = fake_run(credits=[credit("sp:sec/doc", "Katherine Mills", quote="SECRET HEADER LINE")])
    res = {"verdict": "NOT_FOUND", "claims": [], "restricted_matches": 1,
           "restricted_groups": ["sharepoint:security"], "headline": "x"}
    out = hands.attach(res, "How do I read the break-glass runbook?", {"restricted_ids": ["sp:sec/doc"]},
                       asker="Nathan Wu", run=run)
    assert out["hands"]["intent"] == "access"
    blob = repr(out)
    assert "SECRET HEADER LINE" not in blob and "sp:sec/doc" not in blob
    body = out["proposed_actions"][0]["payload"]["body"]
    assert "Security SharePoint site" in body and body.rstrip().endswith("Nathan Wu")
    assert [a["type"] for a in out["proposed_actions"]] == ["email.draft", "slack.draft"]


def test_precedent_goes_to_the_builders_with_the_evidence():
    run = fake_run(credits=[credit("doc:ct", "Fatima Al-Hassan"), credit("doc:ct", "Tom Bradley")])
    res = {"verdict": "INFO", "headline": "Yes.", "claims": [claim("doc:ct", "Continuous training")]}
    out = hands.attach(res, "Has anyone built continuous retraining for a recommendation model?", {}, run=run)
    assert out["hands"]["intent"] == "precedent"
    email = out["proposed_actions"][0]
    assert email["type"] == "email.draft" and "Continuous training" in email["payload"]["body"]
    assert email["payload"]["citations"] == ["doc:ct"]


def test_nothing_is_proposed_without_a_signal():
    run = fake_run(credits=[credit("doc:a", "Owen Owner")])
    res = {"verdict": "INFO", "headline": "h", "claims": [claim("doc:a")]}
    assert hands.attach(res, "What does the company do?", {}, run=run) is res


def test_no_current_owner_means_no_actions_and_says_why():
    run = fake_run(credits=[credit("doc:a", "Kiran Rao", status="alumni", departed="2026-08-14")])
    res = {"verdict": "INFO", "headline": "h", "claims": [claim("doc:a")]}
    out = hands.attach(res, "Has anyone built this already?", {}, run=run)
    assert "proposed_actions" not in out and out["hands"]["skipped_alumni"][0]["name"] == "Kiran Rao"


def test_people_planning_the_same_thing_are_not_offered_as_precedent():
    run = fake_run(credits=[credit("doc:plan", "Jordan Planner", "author") | {"draft": True},
                            credit("doc:adr", "Elena Builder", "owner")],
                   assigned=[{"doc": "ticket:OPEN-1", "name": "Luis Seeker", "status": "employee", "state": "Open"},
                             {"doc": "ticket:DONE-1", "name": "Elena Builder", "status": "employee",
                              "state": "Closed"}])
    owners, _ = hands.resolve_owners(run, ["doc:plan", "doc:adr", "ticket:OPEN-1", "ticket:DONE-1"],
                                     built_only=True)
    assert [o["name"] for o in owners] == ["Elena Builder"]


def test_a_build_request_counts_as_a_precedent_question():
    assert hands.PRECEDENT_Q.search("Help me design a payment retry system for partner payouts")
    assert hands.PRECEDENT_Q.search("I want to build a nightly customer data export to S3, how should I design it?")


def test_repo_is_read_off_record_paths():
    assert hands._repo_of(["doc:eng/ml-platform-repo/docs/ct.md"]) == "eng/ml-platform-repo"
    assert hands._repo_of(["doc:monorepo/services/billing/README.md"]) == "monorepo/services/billing"
    assert hands._repo_of(["doc:handbook/leave.md"]) == ""


def test_precedent_in_a_repo_offers_a_coding_agent_first():
    nid = "doc:engineering/ml-platform-repo/docs/ct.md"
    run = fake_run(credits=[credit(nid, "Fatima Al-Hassan")])
    res = {"verdict": "INFO", "headline": "Yes.", "claims": [claim(nid, "continuous training")]}
    out = hands.attach(res, "Help me design a retraining pipeline", {}, run=run)
    assert [a["type"] for a in out["proposed_actions"]] == ["agent.launch", "email.draft", "slack.draft"]
    brief = out["proposed_actions"][0]["payload"]
    assert brief["repo"] == "engineering/ml-platform-repo" and "Fatima Al-Hassan" in brief["steps"][-1]
    assert brief["will_not"] == ["merge", "deploy", "touch production"]
