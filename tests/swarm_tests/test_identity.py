"""The identity pass: email merges in plain code, the model only for the rest."""

import json
from collections import Counter
from datetime import timedelta

import pytest

from swarm import schema
from swarm.identity import SourceIdentity, resolve, run_identity
from swarm.model import Completion, ModelUnavailable
from swarm.records import Tokens, parse_line
from swarm.run import run_source
from swarm.sources.gdrive import DriveNormalizer
from swarm.sources.github import GitHubNormalizer
from swarm.sources.jira import JiraNormalizer
from swarm.sources.links import GitHubLinks, JiraLinks
from swarm.sources.slack import SlackNormalizer
from swarm.store import LocalStore

from . import gdrive_fixture, github_fixture, jira_fixture, slack_fixture

NOW = jira_fixture.NOW


class Judge:
    """A model that says same person when `rule(text)` is true, with `confidence`."""

    name = "judge"

    def __init__(self, rule=lambda text: True, confidence=0.95, down=False):
        self.rule, self.confidence, self.down = rule, confidence, down
        self.calls = []

    def extract(self, system, text, schema):
        if self.down:
            raise ModelUnavailable("RateLimitError")
        self.calls.append(text)
        same = self.rule(text)
        return Completion(data={"same_person": same, "confidence": self.confidence, "evidence": "names match"},
                          model="judge", stop_reason="end_turn", tokens=Tokens(input=50, output=10))


def ident(source, handle, *names, email=None):
    from swarm import ids
    i = SourceIdentity(ids.source_person(source, handle), source, handle, Counter(names))
    if email:
        i.emails.add(email)
    return i


def clusters_of(identities, **kw):
    kw.setdefault("decisions", {})
    kw.setdefault("usage", Counter())
    c = resolve({i.id: i for i in identities}, **kw)
    return sorted(sorted(ms) for ms in c.groups().values()), c


DHRUV = [ident("slack", "U5", "Dhruv Pendharkar", email="dhruv@x.dev"),
         ident("gdrive", "dhruv@x.dev", "Dhruv Pendharkar", email="dhruv@x.dev"),
         ident("github", "dhruv@x.dev", "Raj Patel", email="dhruv@x.dev"),
         ident("jira", "712020:d", "D. Pendharkar")]


def test_identities_sharing_an_email_are_one_person_without_a_model():
    groups, c = clusters_of(DHRUV[:3], model=None)
    assert groups == [sorted(i.id for i in DHRUV[:3])]
    assert all(h[0] == "email" for h in c.how[c.root(DHRUV[0].id)])


def test_githubs_shared_noreply_address_joins_nobody():
    a = ident("github", "a", "Ann")
    b = ident("github", "b", "Bob")
    # collect() drops noreply@github.com; resolve sees no email on them at all.
    groups, _ = clusters_of([a, b], model=None)
    assert len(groups) == 2


def test_with_no_model_an_identity_no_email_joins_stays_apart():
    groups, _ = clusters_of(DHRUV, model=None)
    assert ["person:jira:712020%3Ad"] in groups


def test_the_model_joins_an_initial_to_its_full_name():
    judge = Judge()
    groups, c = clusters_of(DHRUV, model=judge)
    assert groups == [sorted(i.id for i in DHRUV)]
    assert len(judge.calls) == 1 and "D. Pendharkar" in judge.calls[0] and "dhruv@x.dev" in judge.calls[0]
    assert ("llm", 0.95, "names match") in c.how[c.root(DHRUV[0].id)]


@pytest.mark.parametrize(("same", "confidence"), [(False, 0.95), (True, 0.7)])
def test_a_no_or_an_unsure_yes_keeps_them_apart(same, confidence):
    groups, _ = clusters_of(DHRUV, model=Judge(lambda t: same, confidence=confidence))
    assert ["person:jira:712020%3Ad"] in groups


def test_an_answer_is_asked_once_and_reused():
    decisions = {}
    clusters_of(DHRUV, model=Judge(), decisions=decisions)
    assert len(decisions) == 1
    groups, _ = clusters_of(DHRUV, model=Judge(down=True), decisions=decisions)   # down, but never asked
    assert len(groups) == 1


def test_only_close_names_are_offered_and_an_outage_stops_asking():
    dana = ident("jira", "712020:x", "Dana Lee")
    sam = ident("slack", "U1", "Sam Lee", email="sam@x.dev")
    kim = ident("slack", "U2", "Kim Ortiz", email="kim@x.dev")
    judge = Judge(lambda t: False)
    clusters_of([dana, sam, kim], model=judge)
    assert len(judge.calls) == 1 and "Sam Lee" in judge.calls[0] and "Kim Ortiz" not in judge.calls[0]
    groups, _ = clusters_of([dana, sam, kim], model=Judge(down=True))
    assert len(groups) == 3


def test_a_github_login_counts_as_a_name():
    login = ident("github", "dana-lee")                 # seen only on PRs: no name, no email
    slack = ident("slack", "U9", "Dana Lee", email="dana@x.dev")
    judge = Judge()
    groups, _ = clusters_of([login, slack], model=judge)
    assert len(groups) == 1 and "dana-lee" in judge.calls[0]


# -- the pass over a bucket the four pipelines wrote ---------------------------- #

@pytest.fixture
def store(tmp_path):
    store = LocalStore(tmp_path)
    gh, _ = github_fixture.connector(now=NOW)
    run_source(gh, GitHubNormalizer(gh.index), store, now=NOW)
    ji, _ = jira_fixture.connector(now=NOW)
    run_source(ji, JiraNormalizer(ji.index, GitHubLinks.from_store(store)), store, now=NOW)
    sl, _ = slack_fixture.api_connector(now=NOW)
    run_source(sl, SlackNormalizer(sl.directory, GitHubLinks.from_store(store), JiraLinks.from_store(store)),
               store, now=NOW)
    dr, _ = gdrive_fixture.connector(now=NOW)
    run_source(dr, DriveNormalizer(GitHubLinks.from_store(store), JiraLinks.from_store(store)), store, now=NOW)
    return store


def records(store, manifest):
    out = []
    for name in manifest.files:
        out += [parse_line(line) for line in store.get(f"runs/identity/{manifest.run_id}/{name}").decode().splitlines()]
    return out


def cluster_with(recs, member_id):
    return next(r for r in recs if r.merged_into is None and any(m.id == member_id for m in r.members))


SAM_JIRA = "person:jira:712020%3Abbbb-sam"


def test_the_pass_merges_by_email_and_writes_valid_records(store):
    jsonschema = pytest.importorskip("jsonschema")
    manifest, _ = run_identity(store, now=NOW)
    recs = records(store, manifest)
    assert manifest.counts["identities"] == len(recs) > 0
    spec = json.loads((schema.OUT / "identity.json").read_text(encoding="utf-8"))
    for r in recs:
        jsonschema.validate(json.loads(r.model_dump_json()), spec)
    # Priya's email is on her GitHub commit, her Jira account and in Slack.
    priya = cluster_with(recs, "person:jira:712020%3Aaaaa-priya")
    assert {m.source for m in priya.members} >= {"github", "jira"} and priya.method == "email"
    assert len({r.person_id for r in recs}) == len(recs)          # one record per cluster
    member_ids = [m.id for r in recs for m in r.members]
    assert len(member_ids) == len(set(member_ids))                 # each identity in exactly one cluster


def test_a_rerun_writes_nothing_and_ids_are_stable(store):
    first, _ = run_identity(store, now=NOW)
    again, _ = run_identity(store, now=NOW + timedelta(hours=1))
    assert again.counts["identities"] == 0
    assert json.loads(store.get("state/identity/clusters.json"))


def test_a_later_model_merge_retires_the_smaller_cluster(store):
    first, _ = run_identity(store, now=NOW)                       # no model: Sam's Jira account stands alone
    alone = cluster_with(records(store, first), SAM_JIRA)
    assert [m.id for m in alone.members] == [SAM_JIRA]
    judge = Judge(lambda text: "Sam Lee" in text and "712020:bbbb-sam" in text)
    later, _ = run_identity(store, model=judge, now=NOW + timedelta(hours=1))
    recs = records(store, later)
    sam = cluster_with(recs, SAM_JIRA)
    assert sam.method == "llm" and len(sam.members) > 1 and sam.person_id != alone.person_id
    assert any(r.person_id == alone.person_id and r.merged_into == sam.person_id for r in recs)
    assert later.tokens.input > 0 and later.extractor_versions == ["identity-v2"]


def test_an_answer_under_an_older_prompt_is_asked_again():
    decisions = {}
    clusters_of(DHRUV, model=Judge(), decisions=decisions)
    for decision in decisions.values():
        decision["prompt_version"] = "identity-v0"
    judge = Judge()
    clusters_of(DHRUV, model=judge, decisions=decisions)
    assert len(judge.calls) == 1


def test_the_model_sees_how_often_and_how_lately_each_name_was_used():
    from swarm.identity import _describe
    i = ident("github", "dhruv@x.dev", email="dhruv@x.dev")
    i.names.update({"Raj Patel": 1, "Dhruv Pendharkar": 5})
    i.last_seen.update({"Raj Patel": "2026-08-01", "Dhruv Pendharkar": "2026-10-22"})
    assert _describe([i]) == ("- github account dhruv@x.dev: names Dhruv Pendharkar (5 records, last 2026-10-22), "
                              "Raj Patel (1 record, last 2026-08-01); dhruv@x.dev")


def test_the_pass_publishes_who_is_who_for_the_extractor(store):
    from swarm.people import Directory
    run_identity(store, now=NOW)
    people = json.loads(store.get("state/identity/people.json"))
    assert people and all(set(entry) == {"names", "members"} for entry in people.values())
    directory = Directory.from_store(store)
    [(person_id, written)] = directory.named_in("Approved by Priya Raman.")
    assert written == "Priya Raman" and directory.person_of("person:jira:712020%3Aaaaa-priya") == person_id
