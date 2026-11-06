"""The Slack app's logic without Slack or Neo4j: answers, the lock line, the approval ledger, the cards."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

pytest.importorskip("slack_bolt")

from slackbot import cards  # noqa: E402
from slackbot.answer import Answer, answer, verify  # noqa: E402
from slackbot.graph import Hit, Who, keywords, lucene  # noqa: E402
from slackbot.ledger import Ledger, payload_hash  # noqa: E402
from swarm.model import Completion  # noqa: E402

ADR = ("# ADR-014: Nightly retraining pipeline\n\nWe retrain the ranking model nightly on Airflow, reading "
       "features from the feature store. Owner: Karthik Valluri.")
KARTHIK = Who("person:c:k", "Karthik Valluri", "karthik.tesseract@gmail.com", "U0CB2DPLNSH")


def hit(id, text=ADR, readable=True, score=1.0, source="github", kind="document", owners=None):
    return Hit(id=id, label="Document", title=id.split(":")[-1], url=f"https://x/{id}", source=source, kind=kind,
               text=text, when=datetime(2026, 10, 3), score=score, readable=readable, authors=["Karthik Valluri"],
               owners=owners or [])


class FakeGraph:
    def __init__(self, hits):
        self.hits = hits
        self.asked = None

    def search(self, v, terms, granted=None, limit=8):
        self.asked = (terms, granted)
        return [h if not (granted and h.id in granted) else
                Hit(**{**h.__dict__, "readable": True}) for h in self.hits]


class Scripted:
    """A model that answers from a script: expand gets no extra terms, answer gets `data`."""
    name = "scripted"

    def __init__(self, data):
        self.data = data

    def extract(self, system, text, schema):
        data = {"terms": []} if "terms" in schema["properties"] else self.data
        return Completion(data=data, model="scripted", stop_reason="end_turn")


def test_keywords_and_lucene_escape():
    assert keywords("Has anyone built a retraining pipeline? See ML-1") == ["built", "retraining", "pipeline",
                                                                           "See", "ML-1"]
    assert lucene(["ML-1", "a:b", "two words"]) == 'ML\\-1 OR a\\:b OR "two words"'


def test_verify_quotes_word_for_word():
    assert verify("retrain the ranking model   nightly on Airflow", ADR) == "retrain the ranking model nightly on Airflow"
    assert verify("retrain the model nightly", ADR) is None          # paraphrase
    assert verify("nightly", ADR) is None                            # too short to mean anything


def test_answer_keeps_only_checkable_claims():
    g = FakeGraph([hit("github:repo:1:file:adr-014.md")])
    model = Scripted({"status": "INFO", "headline": "Yes, ADR-014",
                      "claims": [{"text": "Nightly on Airflow", "record": 1,
                                  "quote": "We retrain the ranking model nightly on Airflow"},
                                 {"text": "invented", "record": 1, "quote": "We retrain hourly on Spark clusters"},
                                 {"text": "bad record", "record": 9, "quote": "We retrain the ranking model"}]})
    a = answer(g, {"principals": []}, "has anyone built a retraining pipeline", model)
    assert a.status == "INFO" and [c.text for c in a.claims] == ["Nightly on Airflow"]


def test_no_checkable_claim_is_not_found():
    g = FakeGraph([hit("github:repo:1:file:adr-014.md")])
    model = Scripted({"status": "INFO", "headline": "x", "claims": [{"text": "x", "record": 1, "quote": "not in it at all"}]})
    a = answer(g, {"principals": []}, "retraining pipeline", model)
    assert a.status == "NOT_FOUND" and a.misses


def test_locked_items_are_counted_never_shown_to_the_model():
    secret = "SECRET-WORDING about the retraining budget"
    g = FakeGraph([hit("github:repo:1:file:adr-014.md"),
                   hit("gdrive:file:abc", text=secret, readable=False, score=0.9, source="gdrive", owners=[KARTHIK]),
                   hit("gdrive:file:weak", text="retraining", readable=False, score=0.1, source="gdrive")])
    seen = []

    class Spy(Scripted):
        def extract(self, system, text, schema):
            seen.append(text)
            return super().extract(system, text, schema)

    a = answer(g, {"principals": []}, "retraining pipeline",
               Spy({"status": "INFO", "headline": "h", "claims": [
                   {"text": "t", "record": 1, "quote": "We retrain the ranking model nightly"}]}))
    assert [h.id for h in a.locked] == ["gdrive:file:abc"]            # the weak match is not counted
    assert not any(secret in t for t in seen)
    line = cards.lock_line(a)
    assert "1 related item (1 Drive doc) is restricted" in line and "Karthik Valluri" in line
    assert secret not in json.dumps(cards.answer(a, "k")) and "abc" not in json.dumps(cards.answer(a, "k"))


def test_granted_items_become_readable():
    g = FakeGraph([hit("gdrive:file:abc", readable=False, source="gdrive", owners=[KARTHIK])])
    a = answer(g, {"principals": []}, "retraining pipeline", None, granted=["gdrive:file:abc"])
    assert g.asked[1] == ["gdrive:file:abc"] and a.status == "INFO" and not a.locked


def test_no_model_quotes_the_best_sentence():
    g = FakeGraph([hit("github:repo:1:file:adr-014.md")])
    a = answer(g, {"principals": []}, "retraining Airflow", None)
    assert a.status == "INFO" and "Airflow" in a.claims[0].quote


def test_hash_covers_the_payload_and_edits_need_a_new_approval(tmp_path):
    led = Ledger(tmp_path / "t.db")
    r = led.draft(requester_slack="U1", requester_email="h@x.com", requester_name="Hamilton Park", owner_slack="U2",
                  owner_email="k@x.com", owner_name="Karthik", item_ids=["gdrive:file:b", "gdrive:file:a"],
                  sources=["gdrive"], message="please", days=30, question="q")
    assert r.hash == payload_hash(requester="h@x.com", owner="k@x.com", item_ids=["gdrive:file:a", "gdrive:file:b"],
                                  days=30, message="please")
    old = r.hash
    r = led.edit(r.id, "please, for the ML review")
    assert r.hash != old
    assert not led.transition(r.id, "draft", "sent", hash=old)        # the stale card cannot approve
    assert led.transition(r.id, "draft", "sent", hash=r.hash)
    assert not led.transition(r.id, "draft", "sent", hash=r.hash)     # once


def test_grants_expire(tmp_path):
    led = Ledger(tmp_path / "t.db")
    led.grant("H@x.com", ["gdrive:file:a"], 30, "r1")
    assert led.granted("h@x.com") == ["gdrive:file:a"]
    led.grant("h@x.com", ["gdrive:file:b"], -1, "r2")
    assert led.granted("h@x.com") == ["gdrive:file:a"]


def test_oauth_state_works_once(tmp_path):
    led = Ledger(tmp_path / "t.db")
    s = led.oauth_state("k@x.com", "r1", "v")
    assert led.take_oauth_state(s)["request_id"] == "r1"
    assert led.take_oauth_state(s) is None


def test_channel_card_and_thread_post():
    g = FakeGraph([hit("github:repo:1:file:adr-014.md"),
                   hit("gdrive:file:abc", readable=False, source="gdrive", owners=[KARTHIK])])
    a = answer(g, {"principals": []}, "retraining Airflow", None)
    on = json.dumps(cards.answer(a, "k", channel=True, can_thread=True))
    off = json.dumps(cards.answer(a, "k", channel=True, can_thread=False, thread_reason="Someone here can't read it"))
    assert "send_thread" in on and "send_thread" not in off and "Someone here" in off
    post = json.dumps(cards.thread_post(a, "U1", "abc123"))
    assert "restricted" not in post and "Shared by <@U1>" in post


def test_review_card_names_no_titles():
    led_r = type("R", (), {"id": "r1", "hash": "abc123", "days": 30, "message": "please"})()
    locked = [hit("gdrive:file:secret-plan", readable=False, source="gdrive")]
    blocks = json.dumps(cards.review(led_r, "Karthik Valluri", locked))
    assert "secret-plan" not in blocks and "1 Drive doc" in blocks and "abc123" in blocks


def test_context_steers_the_search_but_is_never_a_source():
    g = FakeGraph([hit("github:repo:1:file:adr-014.md")])
    convo = "Hamilton Park: should we build a nightly retraining job for search on Airflow?"
    model = Scripted({"status": "INFO", "headline": "h", "claims": [
        {"text": "from the chat", "record": 1, "quote": "should we build a nightly retraining job"},
        {"text": "from the ADR", "record": 1, "quote": "We retrain the ranking model nightly on Airflow"}]})
    a = answer(g, {"principals": []}, "what do you think of this?", model, context=convo)
    assert "retraining" in g.asked[0] and "Airflow" in g.asked[0]
    assert [c.text for c in a.claims] == ["from the ADR"] and a.context == convo
    assert "never quoted as a source" in json.dumps(cards.answer(a, "k", channel=True))


def test_answer_dataclass_cites_each_item_once():
    h = hit("x:1")
    from slackbot.answer import Claim
    a = Answer("q", "INFO", "h", claims=[Claim("a", h, "quote one here"), Claim("b", h, "quote two here")])
    assert a.cited == [h]
