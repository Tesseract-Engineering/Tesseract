"""The Docs add-in without a database or a model: what reaches Lucene, how an answer's citations
are made safe to show, and what the API refuses. The graph half is eval/check_docs_addin.py."""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

pytest.importorskip("neo4j")      # docs_addin reads the graph through graph/, which needs the driver

from docs_addin.answer import finish, finish_context, prompt  # noqa: E402
from docs_addin.api import handler  # noqa: E402
from docs_addin.retrieve import Context, Locked, Source, asks, display_name, paragraph_query, search_terms  # noqa: E402


def source(i: int, **kw) -> Source:
    base = dict(id=f"jira:x:issue:{i}", title=f"Ticket {i}", source="jira", kind="issue", url=f"https://j/{i}",
                alias=f"SBX-{i}", updated="2026-10-22", container="SBX", people=["Ishan Sinha"], text=f"text {i}",
                decisions=[], score=1.0)
    base.update(kw)
    return Source(**base)


def ctx(n: int = 3, locked: Locked | None = None) -> Context:
    return Context(viewer={"email": "hamilton@x.com", "member": True, "principals": []},
                   sources=[source(i) for i in range(1, n + 1)], locked=locked or Locked())


PEOPLE = {"Dhruv Pendharkar": "dhruv@x.com", "Ishan Sinha": "ishan@x.com"}


# -- search ---------------------------------------------------------------------- #

def test_search_terms_cannot_reach_lucene_syntax():
    q = search_terms('title:secret OR "x" AND (salary*) ~fuzzy^4 -not +must \\ / [a TO z] {b} || &&')
    assert re.fullmatch(r"[a-z0-9 ]+", q)
    assert "or" not in q.split() and "and" not in q.split()


def test_search_terms_drop_stopwords_and_add_inflections():
    terms = search_terms("Are deprecated authentication endpoints inventoried, with a removal date?").split()
    assert "deprecate" in terms and "deprecated" in terms and "endpoint" in terms
    assert not {"are", "with", "a"} & set(terms)


def test_an_item_quoting_the_question_is_where_it_came_from():
    q = "How is access to token signing material restricted?"
    assert asks("SOC2 questions\n5. How is access to token signing\nmaterial restricted?\n", q)
    assert not asks("Keys live in KMS; access to token signing material is restricted to infra.", q)


def test_display_name_prefers_slack_over_a_github_profile():
    accounts = [{"name": "Raj Patel", "source": "github"}, {"name": "D. Pendharkar", "source": "jira"},
                {"name": "Dhruv Pendharkar", "source": "slack"}, {"name": "dhruv.tesseract1", "source": "gdrive"}]
    assert display_name("dhruv.tesseract1", accounts) == "Dhruv Pendharkar"
    assert display_name("Ishan Sinha", accounts) == "Ishan Sinha"
    assert display_name("handle", []) == "handle"


# -- answers --------------------------------------------------------------------- #

def test_markers_renumber_in_order_of_use_and_unknown_ones_go():
    out = finish({"status": "partial", "answer": "Keys rotate [S3]. A runbook exists [S1][S3]. Made up [S9].",
                  "gaps": ["No schedule."], "ask_person": "", "ask_reason": ""}, ctx(), PEOPLE)
    assert out["answer"] == "Keys rotate [1]. A runbook exists [2][1]. Made up."
    assert [s["alias"] for s in out["sources"]] == ["SBX-3", "SBX-1"]
    assert [s["n"] for s in out["sources"]] == [1, 2]
    assert out["read"] == 3


def test_an_answer_that_cites_nothing_is_not_shown():
    out = finish({"status": "answered", "answer": "Everything is fine.", "gaps": [], "ask_person": "",
                  "ask_reason": ""}, ctx(), PEOPLE)
    assert out["status"] == "none" and out["answer"] == "" and out["sources"] == []


def test_ask_matches_a_known_person_or_nobody():
    data = {"status": "partial", "answer": "x [S1]", "gaps": ["g"], "ask_reason": "Owns OPS-1."}
    assert finish({**data, "ask_person": "Dhruv"}, ctx(), PEOPLE)["ask"] == {
        "name": "Dhruv Pendharkar", "email": "dhruv@x.com", "reason": "Owns OPS-1"}
    assert finish({**data, "ask_person": "Someone Else"}, ctx(), PEOPLE)["ask"] is None


def test_the_selection_reaches_the_prompt_only_when_there_is_one():
    plain = prompt("Which claims?", ctx(1), "Ishan Sinha", "2026-10-28")
    about = prompt("Which claims?", ctx(1), "Ishan Sinha", "2026-10-28", about="read the top-level tenant_id")
    assert "About this passage" not in plain
    assert "About this passage of the document they are writing: read the top-level tenant_id" in about
    assert about.replace("About this passage of the document they are writing: read the top-level tenant_id\n", "") == plain


def test_locked_items_reach_the_prompt_as_counts_and_owners_only():
    locked = Locked(count=3, by_source={"github": 3}, owners=[{"name": "Ishan Sinha", "email": "ishan@x.com"}])
    text = prompt("Which claims?", ctx(2, locked), "Hamilton Park", "2026-10-28")
    assert "3 more items (3 GitHub)" in text and "Ishan Sinha" in text
    assert "[S1] Jira issue: SBX-1 Ticket 1" in text and "[S3]" not in text


def test_notes_share_one_numbering_and_uncited_ones_go():
    out = finish_context({"notes": [
        {"kind": "incident", "title": "JWKS fetched once", "text": "Broke web-app [S2] [S9]."},
        {"kind": "decided", "title": "An opinion", "text": "Just a thought."},
        {"kind": "conflict", "title": "Deprecated", "text": "SBX-5 [S3], see also [S2]."},
        {"kind": "owner", "title": "Dhruv", "text": "Owns OPS-1 [S1]."},
        {"kind": "owner", "title": "A fifth", "text": "Never shown [S1]."},
    ]}, ctx(3))
    assert [n["title"] for n in out["notes"]] == ["JWKS fetched once", "Deprecated"]
    assert out["notes"][0]["text"] == "Broke web-app [1]."
    assert out["notes"][1]["text"] == "SBX-5 [2], see also [1]."
    assert [s["alias"] for s in out["sources"]] == ["SBX-2", "SBX-3"]


def test_no_notes_is_an_answer():
    out = finish_context({"notes": []}, ctx(3))
    assert out["notes"] == [] and out["sources"] == [] and out["read"] == 3


def test_paragraph_query_leads_with_the_section():
    assert paragraph_query("Fetch the JWKS once.", "Token validation") == "Token validation. Fetch the JWKS once."
    assert paragraph_query("Fetch the JWKS once.", None) == "Fetch the JWKS once."


# -- the API ----------------------------------------------------------------------- #

class FakeApp:
    ns, key, dev = "kg:test", "k" * 20, False

    def __init__(self):
        self.asked = []

    def ask(self, question, viewer, about=None):
        self.asked.append((question, viewer) if about is None else (question, viewer, about))
        return {"status": "none", "answer": "", "sources": []}

    def context(self, paragraph, section, doc, viewer):
        self.asked.append((paragraph, section, doc, viewer))
        return {"notes": [], "sources": []}


@pytest.fixture
def api():
    app = FakeApp()
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler(app))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield app, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def post(url, body, key=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **({"X-Addin-Key": key} if key else {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_api_needs_the_key(api):
    app, url = api
    assert post(url + "/answer", {"question": "q?", "viewer": "a@b.c"})[0] == 401
    assert post(url + "/answer", {"question": "q?", "viewer": "a@b.c"}, key="wrong")[0] == 401
    assert app.asked == []


def test_api_needs_a_question_and_a_viewer(api):
    app, url = api
    assert post(url + "/answer", {"question": "q?"}, key=app.key)[0] == 400
    assert post(url + "/answer", {"viewer": "a@b.c"}, key=app.key)[0] == 400
    code, body = post(url + "/answer", {"question": " q? ", "viewer": "A@B.C"}, key=app.key)
    assert code == 200 and app.asked == [("q?", "a@b.c")] and "seconds" in body


def test_a_question_can_be_about_the_selection(api):
    app, url = api
    code, _ = post(url + "/answer", {"question": "Is this right?", "about": " fetch the JWKS once ", "viewer": "a@b.c"},
                   key=app.key)
    assert code == 200 and app.asked == [("Is this right?", "a@b.c", "fetch the JWKS once")]


def test_context_takes_a_paragraph_and_its_section(api):
    app, url = api
    assert post(url + "/context", {"paragraph": "p", "viewer": "a@b.c"})[0] == 401
    assert post(url + "/context", {"section": "Rollout", "viewer": "a@b.c"}, key=app.key)[0] == 400
    code, body = post(url + "/context", {"paragraph": " Ship it. ", "section": "Rollout", "doc": "D",
                                         "viewer": "a@b.c"}, key=app.key)
    assert code == 200 and app.asked == [("Ship it.", "Rollout", "D", "a@b.c")] and body["notes"] == []


def test_dev_routes_are_off_unless_asked_for(api):
    app, url = api
    assert post(url + "/dev/answer", {"question": "q?", "viewer": "a@b.c"})[0] == 404
    assert post(url + "/dev/context", {"paragraph": "p", "viewer": "a@b.c"})[0] == 404
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(url + "/dev")
    assert e.value.code == 404


def test_slack_goes_on_the_people_an_answer_names():
    from docs_addin.api import App
    app = App.__new__(App)
    dhruv = {"user": "U1", "team": "T1", "web": "https://x.slack.com"}
    app._directory = lambda: ({}, {"Dhruv Pendharkar": dhruv, "Ishan Sinha": {"user": "U2", "team": "T1", "web": None}})
    out = app.with_slack({
        "ask": {"name": "Dhruv Pendharkar", "email": "d@x.com", "reason": "r"},
        "locked": {"count": 1, "by_source": {"github": 1}, "owners": [{"name": "Ishan Sinha", "email": "i@x.com"}]},
        "notes": [{"kind": "owner", "title": "Talk to Dhruv Pendharkar", "text": "Owns OPS-1, then Ishan Sinha [1]."},
                  {"kind": "decided", "title": "Refresh every 10 minutes", "text": "WEB-3 [2]."}],
    })
    assert out["ask"]["slack"] == dhruv
    assert out["locked"]["owners"][0]["slack"]["user"] == "U2"
    assert out["notes"][0]["people"] == [{"name": "Dhruv Pendharkar", "slack": dhruv}]   # the title's person only
    assert out["notes"][1]["people"] == []


def test_figures_need_the_key_and_stay_in_their_folder(api):
    app, url = api
    def get(path, key=None):
        req = urllib.request.Request(url + path, headers={"X-Addin-Key": key} if key else {})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.headers.get("Content-Type")
        except urllib.error.HTTPError as e:
            return e.code, None
    assert get("/figures/fig1-current.png")[0] == 404
    assert get("/figures/fig1-current.png", key=app.key) == (200, "image/png")
    assert get("/figures/..%2Fapi.py", key=app.key)[0] == 404
    assert get("/figures/nope.png", key=app.key)[0] == 404


def test_no_slack_button_for_the_viewer_themselves():
    from docs_addin.api import App
    app = App.__new__(App)
    me = {"user": "U9", "team": "T1", "web": None}
    app._directory = lambda: ({"Ishan Sinha": "ishan@x.com"}, {"Ishan Sinha": me})
    out = app.with_slack({"viewer": {"email": "ishan@x.com"},
                          "notes": [{"kind": "owner", "title": "Ishan Sinha owns this", "text": "[1]"}]})
    assert out["notes"][0]["people"] == []
