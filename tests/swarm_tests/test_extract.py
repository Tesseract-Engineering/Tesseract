"""Stage 8, LLM extraction, on fixtures with a scripted model: no model is
called in CI. The answers below are hand-written in the shape the model
returns, including the mistakes the stage must drop."""

import hashlib
import json
from datetime import timedelta

import pytest

from swarm import model as model_mod
from swarm import schema
from swarm.model import Bedrock, ClaudeApi, Completion, ModelUnavailable, Replay
from swarm.records import ItemRecord, Tokens, parse_line
from swarm.review import sample, sheet
from swarm.run import run_source
from swarm.sources.github import GitHubNormalizer
from swarm.sources.links import GitHubLinks
from swarm.sources.slack import SlackNormalizer
from swarm.stages import prompts
from swarm.stages.extract import build_record, output_schema, worth_extracting
from swarm.stages.manifest import read_manifest
from swarm.store import LocalStore

from . import github_fixture
from .slack_fixture import NOW, api_connector

THREAD = "slack:T01ACME:thread:C42AUTH:1791986400.000100"
SAM = "person:slack:U123SAM"
# Every item of the fixture runs that the cost filter lets through.
WORTH = {"github:repo:9001:issue:1", "github:repo:9001:pr:2", "github:repo:9001:issue:3", "github:repo:9002:issue:1",
         "github:repo:9001:file:.github/CODEOWNERS", "github:repo:9001:file:README.md",
         "github:repo:9001:file:docs/adr/0002%20use%20oidc.md", "github:repo:9002:file:README.md",
         "slack:T01ACME:thread:C42AUTH:1785578400.000100", THREAD}

THREAD_ANSWER = {
    "entities": [
        {"id": "d1", "type": "decision", "name": "Use OIDC instead of SAML for staff login", "attributes": [],
         "evidence": ["we use OIDC instead of SAML for staff login", "Decided then."]},
        {"id": "a1", "type": "action_item", "name": "Sam to draft the OIDC rollout plan",
         "attributes": [{"key": "due", "value": "Friday"}], "evidence": ["I'll draft the rollout plan by Friday"]},
        {"id": "x1", "type": "system", "name": "IdP proxy", "attributes": [],
         "evidence": ["the proxy, which is not quoted anywhere"]},                           # not in the text
        {"id": "t1", "type": "thread", "name": "This thread", "attributes": [], "evidence": ["Decided then."]},
    ],
    "relations": [
        {"type": "decided_in", "source": "d1", "target": THREAD, "confidence": 0.95, "evidence": ["Decided then."]},
        {"type": "decided_by", "source": "d1", "target": SAM, "confidence": 0.8,
         "evidence": ["Sam Lee: Decided then."]},
        {"type": "assigned_to", "source": "a1", "target": SAM, "confidence": 1.4,
         "evidence": ["I'll draft the rollout plan by Friday"]},
        {"type": "decided_by", "source": "d1", "target": "person:slack:U000NOBODY", "confidence": 0.9,
         "evidence": ["Decided then."]},                                                     # not a known id
        {"type": "decided_in", "source": "a1", "target": THREAD, "confidence": 0.9,
         "evidence": ["Decided then."]},                                                     # action items aren't decided
        {"type": "works_on", "source": SAM, "target": "x1", "confidence": 0.9,
         "evidence": ["Decided then."]},                                                     # x1 was dropped
        {"type": "decided_in", "source": "d1", "target": THREAD, "confidence": 0.9,
         "evidence": ["the decision, paraphrased"]},                                         # duplicate, unquoted
    ],
}


class Scripted:
    """A model that answers from a script, keyed by the item's id (the first
    known id in the message), and counts its calls."""

    name = "scripted"

    def __init__(self, answers=None, *, unavailable_after=None, refuse=()):
        self.answers = answers or {}
        self.unavailable_after = unavailable_after
        self.refuse = set(refuse)
        self.calls = []

    def extract(self, system, text, schema):
        item_id = text.split("Known ids:\n- ", 1)[1].split()[0]
        if self.unavailable_after is not None and len(self.calls) >= self.unavailable_after:
            raise ModelUnavailable("RateLimitError")
        self.calls.append(item_id)
        if item_id in self.refuse:
            return Completion(data=None, model="scripted", stop_reason="refusal", tokens=Tokens(input=100))
        answer = self.answers.get(item_id, {"entities": [], "relations": []})
        return Completion(data=answer, model="scripted", stop_reason="end_turn", tokens=Tokens(input=100, output=20))


def run_both(store, model, *, now=NOW, budget=None):
    """A night: GitHub, then Slack, into one bucket."""
    gh, _ = github_fixture.connector(now=now)
    github = run_source(gh, GitHubNormalizer(gh.index), store, now=now, model=model, llm_budget=budget)[0]
    sl, _ = api_connector()
    slack = run_source(sl, SlackNormalizer(sl.directory, GitHubLinks.from_store(store)), store, now=now,
                       model=model, llm_budget=budget)[0]
    return github, slack


def extractions(store, *manifests):
    out = []
    for m in manifests:
        for name in m.files:
            if name.startswith("extractions/"):
                out += [parse_line(line) for line in store.get(f"runs/{m.source}/{m.run_id}/{name}").decode().splitlines()]
    return out


def items(store, *manifests):
    out = {}
    for m in manifests:
        for name in m.files:
            if name.startswith("items/"):
                for line in store.get(f"runs/{m.source}/{m.run_id}/{name}").decode().splitlines():
                    record = parse_line(line)
                    out[record.id] = record
    return out


@pytest.fixture
def store(tmp_path):
    return LocalStore(tmp_path / "bucket")


# -- prompts and the answer shape ------------------------------------------- #

# A prompt's text and its version move together: change one, change both.
PINNED = {
    "document": ("document-v3", "6f764fe2d80370eb"),
    "issue": ("issue-v3", "4d43e63aa2623010"),
    "pull_request": ("pull_request-v2", "7b6fab9c5e91b9f5"),
    "thread": ("thread-v2", "382070e7e9370746"),
}


def test_every_prompt_version_pins_its_text():
    rendered = {k: (p.version, hashlib.sha256(p.system.encode()).hexdigest()[:16])
                for k in prompts.kinds() if (p := prompts.for_kind(k))}
    assert rendered == PINNED, "a prompt changed: bump its version, then update PINNED"


def test_prompts_render_the_whole_llm_vocabulary():
    from swarm import ontology

    onto = ontology.load()
    system = prompts.for_kind("thread").system
    assert all(f"- {t}" in system for t in onto.llm_entities | onto.llm_relations)
    assert "- person" not in system and "- repo" not in system


def test_the_answer_schema_is_closed_and_has_no_limits_structured_outputs_refuse():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False and set(node["required"]) == set(node["properties"])
            assert not {"minLength", "maxLength", "minimum", "maximum", "minItems", "maxItems"} & set(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(output_schema())


# -- the cost filter --------------------------------------------------------- #

def test_only_items_worth_it_go_to_the_model(store):
    model = Scripted()
    github, slack = run_both(store, model)
    assert set(model.calls) == WORTH
    sent = [items(store, github, slack)[i] for i in WORTH]
    assert all(worth_extracting(i) for i in sent)
    # Short Slack exchanges, commits, repos and channels never are.
    assert not [i for i in items(store, github, slack).values() if i.id not in WORTH and worth_extracting(i)]


# -- what the stage keeps from an answer ------------------------------------- #

def thread_item(store):
    _, slack = run_both(store, Scripted())
    return items(store, slack)[THREAD]


def test_an_answer_keeps_only_what_checks_out(store):
    item = thread_item(store)
    record, dropped = build_record(item, THREAD_ANSWER, model="scripted", prompt_version="thread-v2",
                                   run_id="r", now=NOW)
    assert [(e.local_id, e.type, e.name) for e in record.entities] == [
        ("e1", "decision", "Use OIDC instead of SAML for staff login"),
        ("e2", "action_item", "Sam to draft the OIDC rollout plan")]
    assert record.entities[1].attributes == {"due": "Friday"}
    assert [(r.type, r.source, r.target, r.confidence) for r in record.relations] == [
        ("decided_in", "e1", THREAD, 0.95), ("decided_by", "e1", SAM, 0.8), ("assigned_to", "e2", SAM, 1.0)]
    assert dropped == {"entity_evidence": 1, "entity_type": 1, "relation_endpoint": 2,
                       "relation_not_allowed": 1, "relation_evidence": 1}
    for span in [s for e in record.entities for s in e.evidence] + [s for r in record.relations for s in r.evidence]:
        assert item.text[span.start:span.end] == span.quote


def test_spans_count_code_points(store):
    item = thread_item(store).model_copy(update={"text": "🚀 Ünïcode first. Decided: ship OIDC."})
    record, _ = build_record(item, {"entities": [{"id": "e1", "type": "decision", "name": "Ship OIDC",
                                                  "attributes": [], "evidence": ["Decided: ship OIDC."]}],
                                    "relations": []}, model="m", prompt_version="thread-v2", run_id="r", now=NOW)
    span = record.entities[0].evidence[0]
    assert (span.start, span.end) == (17, 36) and item.text[span.start:span.end] == span.quote


# -- a run with a model ------------------------------------------------------ #

def test_a_run_writes_valid_extractions_and_reports_tokens_and_versions(store):
    jsonschema = pytest.importorskip("jsonschema")
    model = Scripted({THREAD: THREAD_ANSWER})
    github, slack = run_both(store, model)
    assert read_manifest(store, "slack", slack.run_id) == slack
    assert slack.counts["extractions"] == 2 and github.counts["extractions"] == 8
    assert slack.extractor_versions == ["thread-v2"]
    assert github.extractor_versions == ["document-v3", "issue-v3", "pull_request-v2"]
    assert slack.tokens == Tokens(input=200, output=40) and not slack.llm_budget_hit
    spec = json.loads((schema.OUT / "extraction.json").read_text(encoding="utf-8"))
    for record in extractions(store, github, slack):
        jsonschema.validate(json.loads(record.model_dump_json()), spec)
        assert record.item_version == items(store, github, slack)[record.item_id].version


def test_rerunning_the_window_asks_the_model_nothing(store):
    run_both(store, Scripted())
    again = Scripted()
    github, slack = run_both(store, again, now=NOW + timedelta(hours=1))
    assert again.calls == []
    assert github.counts["extractions"] == slack.counts["extractions"] == 0
    assert slack.tokens == Tokens() and slack.extractor_versions == []


def test_a_new_prompt_version_re_extracts_what_the_run_fetches(store, monkeypatch):
    run_both(store, Scripted())
    monkeypatch.setitem(prompts._KINDS, "thread", ("thread-v3",) + prompts._KINDS["thread"][1:])
    prompts.for_kind.cache_clear()
    try:
        again = Scripted()
        _, slack = run_both(store, again, now=NOW + timedelta(hours=1))
    finally:
        prompts.for_kind.cache_clear()
    assert sorted(again.calls) == sorted(i for i in WORTH if i.startswith("slack:"))
    assert slack.extractor_versions == ["thread-v3"] and slack.counts["items"] == 0


def test_over_budget_items_wait_for_the_next_run(store):
    model = Scripted()
    github, _ = run_both(store, model, budget=150)              # each call costs 120 tokens
    assert len(model.calls) == 4 and github.llm_budget_hit       # GitHub hit it on its second call
    assert github.counts["extractions"] == 2 and github.counts["items"] > 0   # fetching went on
    backlog = json.loads(store.get("state/github/backlog.json"))
    assert len(backlog) == 6

    later = Scripted()
    github2, slack2 = run_both(store, later, now=NOW + timedelta(hours=1))
    assert sorted(later.calls) == sorted(backlog)                # only the backlog; Slack was done
    assert github2.counts["extractions"] == 6 and not github2.llm_budget_hit
    assert json.loads(store.get("state/github/backlog.json")) == {}


def test_a_model_outage_defers_extraction_and_the_run_still_completes(store):
    github, slack = run_both(store, Scripted(unavailable_after=0))
    assert github.counts["extractions"] == slack.counts["extractions"] == 0
    assert github.counts["items"] > 0 and read_manifest(store, "github", github.run_id)
    assert len(json.loads(store.get("state/slack/backlog.json"))) == 2

    later = Scripted()
    run_both(store, later, now=NOW + timedelta(hours=1))
    assert set(later.calls) == WORTH


def test_a_refusal_writes_nothing_and_is_not_asked_again(store):
    run_both(store, Scripted(refuse={THREAD}))
    again = Scripted()
    _, slack = run_both(store, again, now=NOW + timedelta(hours=1))
    assert again.calls == [] and slack.counts["extractions"] == 0


def test_a_queued_item_that_changed_since_is_dropped_from_the_backlog(store):
    run_both(store, Scripted(unavailable_after=0))
    backlog = json.loads(store.get("state/slack/backlog.json"))
    stale = ItemRecord.model_validate_json(backlog[THREAD]).model_copy(update={"content_hash": "sha256:" + "0" * 64})
    backlog[THREAD] = stale.model_dump_json()
    store.put("state/slack/backlog.json", json.dumps(backlog).encode())
    seen = json.loads(store.get("state/slack/seen.json"))
    seen["items"][THREAD][0] = "sha256:" + "1" * 64          # the bucket has a newer version than the queued one
    store.put("state/slack/seen.json", json.dumps(seen).encode())

    later = Scripted()
    sl, _ = api_connector()
    run_source(sl, SlackNormalizer(sl.directory, GitHubLinks.from_store(store)), store,
               now=NOW + timedelta(hours=1), model=later)
    assert later.calls.count(THREAD) == 1                     # once, as the fetched item, not from the backlog


# -- the model backends ------------------------------------------------------ #

class FakeResponse:
    def __init__(self, text, stop_reason="end_turn", model="claude-opus-5-5"):
        self.content = [type("B", (), {"type": "text", "text": text})()]
        self.stop_reason = stop_reason
        self.model = model
        self.usage = type("U", (), {"input_tokens": 10, "cache_creation_input_tokens": 5,
                                    "cache_read_input_tokens": 100, "output_tokens": 7})()


class FakeCreate:
    def __init__(self, result):
        self.result = result
        self.kwargs = None

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def fake_client(create):
    messages = type("M", (), {"create": create})()
    return type("C", (), {"messages": messages, "beta": type("B", (), {"messages": messages})()})()


def test_the_direct_api_asks_for_the_schema_with_fallbacks_and_a_cached_system_prompt():
    create = FakeCreate(FakeResponse('{"entities": [], "relations": []}'))
    done = ClaudeApi(client=fake_client(create)).extract("SYSTEM", "TEXT", {"type": "object"})
    assert done == Completion(data={"entities": [], "relations": []}, model="claude-opus-5-5",
                              stop_reason="end_turn", tokens=Tokens(input=115, output=7))
    k = create.kwargs
    assert k["model"] == "claude-opus-5-5" and k["fallbacks"] == "default"
    assert k["betas"] == [model_mod.FALLBACK_BETA]
    assert k["system"] == [{"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral"}}]
    assert k["output_config"] == {"effort": "medium", "format": {"type": "json_schema", "schema": {"type": "object"}}}
    assert "temperature" not in k and "thinking" not in k


def test_bedrock_prefixes_the_model_and_sends_no_fallbacks():
    create = FakeCreate(FakeResponse("{}"))
    Bedrock(client=fake_client(create)).extract("S", "T", {})
    assert create.kwargs["model"] == "anthropic.claude-opus-5-5" and "fallbacks" not in create.kwargs


def test_a_refusal_or_cut_off_answer_has_no_data():
    for stop in ("refusal", "max_tokens"):
        done = ClaudeApi(client=fake_client(FakeCreate(FakeResponse('{"entities": [', stop)))).extract("S", "T", {})
        assert done.data is None and done.stop_reason == stop


def test_an_unreachable_model_raises_model_unavailable():
    import anthropic
    import httpx2

    error = anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
    with pytest.raises(ModelUnavailable):
        ClaudeApi(client=fake_client(FakeCreate(error))).extract("S", "T", {})


def test_replay_records_once_then_answers_from_disk(tmp_path):
    live = Scripted({"x:1": {"entities": [], "relations": [{"type": "t"}]}})
    text = "Known ids:\n- x:1  this"
    first = Replay(tmp_path, record=live).extract("S", text, {})
    assert Replay(tmp_path).extract("S", text, {}) == first and live.calls == ["x:1"]
    with pytest.raises(FileNotFoundError):
        Replay(tmp_path).extract("S", text + " changed", {})


def test_the_backend_follows_the_environment(monkeypatch):
    monkeypatch.setenv("SWARM_MODEL_BACKEND", "off")
    assert model_mod.from_env() is None
    monkeypatch.setenv("SWARM_MODEL_BACKEND", "nonsense")
    with pytest.raises(SystemExit):
        model_mod.from_env()


# -- the spot-check sheet ---------------------------------------------------- #

def test_the_review_sheet_shows_each_sample_with_its_text_and_quotes(store):
    run_both(store, Scripted({THREAD: THREAD_ANSWER}))
    picked = sample(store, n=20, prompt="thread-v2", all_runs=False, seed=0)
    assert len(picked) == 2 and all(item is not None for _, item in picked)
    out = sheet(picked, now=NOW)
    assert "Use OIDC instead of SAML for staff login" in out and "“I'll draft the rollout plan by Friday”" in out
    assert out.count("- Invented:") == 2 and "Nothing extracted." in out


# -- Message Batches ---------------------------------------------------------- #

class ScriptedBatch(Scripted):
    """Scripted answers through the batch interface. A batch ends after
    `polls` checks; items in `unanswered` come back with no answer (expired or
    a server error) and items in `invalid` as an invalid request."""

    batch = True

    def __init__(self, answers=None, *, polls=0, unanswered=(), invalid=(), submit_fails=False, **kw):
        super().__init__(answers, **kw)
        self.polls = polls
        self.unanswered = set(unanswered)
        self.invalid = set(invalid)
        self.submit_fails = submit_fails
        self.batches = {}           # batch id -> [(custom_id, item id, request)]
        self.checks = {}

    def submit(self, requests):
        if self.submit_fails:
            raise ModelUnavailable("APIConnectionError")
        batch_id = f"msgbatch_{len(self.batches) + 1}"
        self.batches[batch_id] = [(r.custom_id, r.text.split("Known ids:\n- ", 1)[1].split()[0], r) for r in requests]
        self.checks[batch_id] = 0
        return batch_id

    def ended(self, batch_id):
        self.checks[batch_id] += 1
        return self.checks[batch_id] > self.polls

    def results(self, batch_id):
        for custom_id, item_id, r in self.batches[batch_id]:
            if item_id in self.unanswered:
                yield custom_id, None
            elif item_id in self.invalid:
                yield custom_id, Completion(data=None, model="scripted", stop_reason="invalid_request")
            else:
                yield custom_id, self.extract(r.system, r.text, r.schema)

    def sent(self):
        return [item_id for reqs in self.batches.values() for _, item_id, _ in reqs]


def run_batched(store, model, *, now=NOW, budget=None, wait=3600.0):
    gh, _ = github_fixture.connector(now=now)
    github = run_source(gh, GitHubNormalizer(gh.index), store, now=now, model=model, llm_budget=budget,
                        batch_wait=wait)[0]
    sl, _ = api_connector()
    slack = run_source(sl, SlackNormalizer(sl.directory, GitHubLinks.from_store(store)), store, now=now,
                       model=model, llm_budget=budget, batch_wait=wait)[0]
    return github, slack


@pytest.fixture
def no_sleep(monkeypatch):
    """Batch polls sleep for real; tests step a fake clock instead."""
    clock = [0.0]
    monkeypatch.setattr("swarm.stages.extract.time.sleep", lambda s: clock.__setitem__(0, clock[0] + s))
    monkeypatch.setattr("swarm.stages.extract.time.monotonic", lambda: clock[0])


def test_a_batch_run_writes_what_direct_calls_would(store, tmp_path, no_sleep):
    direct = LocalStore(tmp_path / "direct")
    d_github, d_slack = run_both(direct, Scripted({THREAD: THREAD_ANSWER}))
    model = ScriptedBatch({THREAD: THREAD_ANSWER}, polls=3)
    github, slack = run_batched(store, model)
    assert len(model.batches) == 2 and set(model.sent()) == WORTH          # one batch per source run
    strip = lambda rs: sorted((r.item_id, r.entities, r.relations) for r in rs)          # noqa: E731
    assert strip(extractions(store, github, slack)) == strip(extractions(direct, d_github, d_slack))
    assert slack.tokens == d_slack.tokens and slack.extractor_versions == ["thread-v2"]
    assert json.loads(store.get("state/slack/batches.json")) == {}
    assert json.loads(store.get("state/slack/backlog.json")) == {}


def test_a_batch_that_outlives_the_run_is_read_by_the_next_one(store, no_sleep):
    slow = ScriptedBatch({THREAD: THREAD_ANSWER}, polls=10_000)
    _, slack = run_batched(store, slow, wait=120.0)
    assert slack.counts["extractions"] == 0 and slack.counts["items"] > 0          # the run still completes
    pending = json.loads(store.get("state/slack/batches.json"))
    assert list(pending) == ["msgbatch_2"]
    assert set(json.loads(store.get("state/slack/backlog.json"))) == {i for i in WORTH if i.startswith("slack:")}

    slow.polls = 0                                                                 # it has ended since
    _, slack2 = run_batched(store, slow, now=NOW + timedelta(hours=1))
    assert slack2.counts["extractions"] == 2 and len(slow.batches) == 2          # read, not sent again
    assert json.loads(store.get("state/slack/batches.json")) == {}
    assert json.loads(store.get("state/slack/backlog.json")) == {}


def test_unanswered_items_are_sent_again_and_invalid_ones_settle(store, no_sleep):
    model = ScriptedBatch(unanswered={THREAD}, invalid={"github:repo:9001:issue:3"})
    run_batched(store, model)
    assert THREAD in json.loads(store.get("state/slack/backlog.json"))
    assert "github:repo:9001:issue:3" not in json.loads(store.get("state/github/backlog.json"))

    again = ScriptedBatch()
    _, slack = run_batched(store, again, now=NOW + timedelta(hours=1))
    assert again.sent() == [THREAD] and slack.counts["extractions"] == 1


def test_the_budget_is_checked_before_sending(store, no_sleep):
    model = ScriptedBatch()
    github, _ = run_batched(store, model, budget=5000)
    assert github.llm_budget_hit and 0 < len([i for i in model.sent() if i.startswith("github:")]) < 4
    assert json.loads(store.get("state/github/backlog.json"))


def test_a_failed_send_leaves_everything_for_the_next_run(store, no_sleep):
    github, _ = run_batched(store, ScriptedBatch(submit_fails=True))
    assert github.counts["extractions"] == 0 and read_manifest(store, "github", github.run_id)
    later = ScriptedBatch()
    run_batched(store, later, now=NOW + timedelta(hours=1))
    assert set(later.sent()) == WORTH


def test_an_answer_for_an_older_version_is_not_written_to_the_new_one(store, no_sleep):
    slow = ScriptedBatch(polls=10_000)
    run_batched(store, slow, wait=0.0)
    mapping = json.loads(store.get("state/slack/batches.json"))["msgbatch_2"]
    for entry in mapping.values():
        entry[2] = "an-older-version"                     # as if the item changed after it was sent
    store.put("state/slack/batches.json", json.dumps({"msgbatch_2": mapping}).encode())
    slow.polls = 0
    _, slack = run_batched(store, slow, now=NOW + timedelta(hours=1))
    # The old answers are ignored and the current versions are sent anew.
    assert set(slow.sent()[-2:]) == {i for i in WORTH if i.startswith("slack:")}
    assert slack.counts["extractions"] == 2 and json.loads(store.get("state/slack/batches.json")) == {}


class FakeBatches:
    def __init__(self, lines=(), status="ended"):
        self.created = None
        self.lines = list(lines)
        self.status = status

    def create(self, **kwargs):
        self.created = kwargs
        return type("Batch", (), {"id": "msgbatch_x"})()

    def retrieve(self, message_batch_id):
        return type("Batch", (), {"processing_status": self.status})()

    def results(self, message_batch_id):
        return iter(self.lines)


def ns(**kw):
    return type("NS", (), kw)()


def test_the_api_sends_a_batch_without_fallbacks_and_reads_each_kind_of_result():
    batches = FakeBatches([
        ns(custom_id="r00000", result=ns(type="succeeded", message=FakeResponse('{"entities": [], "relations": []}'))),
        ns(custom_id="r00001", result=ns(type="errored", error=ns(error=ns(type="invalid_request_error")))),
        ns(custom_id="r00002", result=ns(type="errored", error=ns(error=ns(type="overloaded_error")))),
        ns(custom_id="r00003", result=ns(type="expired")),
    ])
    client = ns(messages=ns(batches=batches), beta=None)
    api = ClaudeApi(client=client)
    assert api.submit([model_mod.BatchRequest("r00000", "SYSTEM", "TEXT", {"type": "object"})]) == "msgbatch_x"
    [request] = batches.created["requests"]
    assert request["custom_id"] == "r00000" and "fallbacks" not in request["params"]
    assert request["params"]["output_config"]["format"]["type"] == "json_schema"
    assert api.ended("msgbatch_x")
    got = dict(api.results("msgbatch_x"))
    assert got["r00000"].data == {"entities": [], "relations": []} and got["r00000"].tokens == Tokens(input=115, output=7)
    assert got["r00001"].data is None and got["r00001"].stop_reason == "invalid_request"
    assert got["r00002"] is None and got["r00003"] is None


def test_batching_follows_the_environment(monkeypatch):
    monkeypatch.setenv("SWARM_MODEL_BACKEND", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    assert model_mod.from_env().batch is True
    monkeypatch.setenv("SWARM_MODEL_BATCH", "off")
    assert model_mod.from_env().batch is False
    monkeypatch.setenv("SWARM_MODEL_BATCH", "maybe")
    with pytest.raises(SystemExit):
        model_mod.from_env()


def test_a_workspace_id_goes_on_every_request(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")
    monkeypatch.delenv("ANTHROPIC_WORKSPACE_ID", raising=False)
    assert "anthropic-workspace-id" not in ClaudeApi().client.default_headers
    monkeypatch.setenv("ANTHROPIC_WORKSPACE_ID", "wrkspc_test")
    assert ClaudeApi().client.default_headers["anthropic-workspace-id"] == "wrkspc_test"


def test_a_ticket_only_automation_touched_is_not_sent():
    from swarm.records import Acl, Person
    base = dict(id="jira:site:issue:1", kind="issue", source="jira", container="jira:site:project:1", title="t",
                text="Automation: SLA breached, escalating.", url="https://x.atlassian.net/browse/A-1",
                created_at=NOW, updated_at=NOW, version="v", acl=Acl(visibility="public"), fetched_at=NOW,
                run_id="r", content_hash="sha256:" + "0" * 64)
    bot_only = ItemRecord.model_construct(**base, people=[], meta={"bots": ["Automation for Jira"]})
    touched = ItemRecord.model_construct(**base, meta={"bots": ["Automation for Jira"]}, people=[
        Person(id="person:jira:a", source="jira", handle="a", roles=["commenter"])])
    assert not worth_extracting(bot_only) and worth_extracting(touched)
