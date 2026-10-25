"""
extract.py - stage 8, LLM extraction (docs/SWARM.md, "Pipeline stages").

For each item a run writes or sees again, in order:

  cost filter     kinds with no prompt, and trivial items, are never sent:
                  Slack conversations under 3 messages or 200 characters,
                  empty PRs and issues, Jira tickets no person has touched,
                  documents with no text
  already done    an extraction for this item version and prompt version
                  exists, so an unchanged item is not re-extracted; a new
                  prompt version re-extracts what the run fetches
  budget          once the run's tokens reach SWARM_LLM_TOKEN_BUDGET, the
                  rest wait in the backlog; fetching goes on
  extract         the model answers in a fixed JSON shape, with quotes as
                  evidence; code finds each quote in the item text to make
                  the span, and drops what it cannot place

The backlog is items that were due an extraction but did not get one: over
budget, the model was unavailable or turned off (defer), or their batch is
still running. It lives
in state/{source}/ with the checkpoints and is worked first on the next run,
so nothing waits for its item to change again. An item that changed since it
was queued is dropped from the backlog; its new version is extracted when
fetched.

Batches (a model with batch on, swarm/model.py): instead of one call per
item, the run queues its items and, once fetching is done, sends them as one
Message Batch at half the price, then waits up to SWARM_BATCH_WAIT_MINUTES
for it. Answers read in time are written by this run. A batch still running
when the wait ends is recorded in state/{source}/batches.json, its items stay
in the backlog, and the next run reads it before anything else. The token
budget is checked before sending, against an estimate (about 3 characters
per input token plus OUTPUT_ESTIMATE); the manifest records the real counts.

What the model gets wrong is dropped, never written: an entity or relation
whose evidence is not in the text, a type outside the ontology, a relation
between ids it was not given, or one the ontology does not allow between
those types. Counts are logged, never contents.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from .. import ids, ontology
from ..model import BatchRequest, Completion, Model, ModelUnavailable
from ..records import Entity, ExtractionRecord, Extractor, ItemRecord, Relation, Span, Tokens
from . import prompts
from .write import RunWriter

log = logging.getLogger("swarm.extract")

MIN_MESSAGES = 3
MIN_CHARS = 200
MAX_QUOTES = 3
OUTPUT_ESTIMATE = 2000          # tokens per answer, for the budget check before a batch is sent
BATCH_WAIT = 60 * 60.0          # seconds a run waits for its batch (SWARM_BATCH_WAIT_MINUTES)
BATCH_POLL = 30.0


def worth_extracting(item: ItemRecord) -> bool:
    """The cost filter. Deterministic extraction runs on every item anyway."""
    if prompts.for_kind(item.kind) is None or not item.text.strip():
        return False
    if item.kind == "thread":
        return item.meta.get("message_count", 0) >= MIN_MESSAGES and len(item.text) >= MIN_CHARS
    if item.source == "jira" and not item.people and item.meta.get("bots"):
        return False                                          # a ticket only automation has touched
    return True


def defer(writer: RunWriter, item: ItemRecord) -> None:
    """A run with no model: queue what a model would have been asked, so the
    next run with one extracts it. Turning the model off defers extraction;
    it never skips it."""
    prompt = prompts.for_kind(item.kind)
    if prompt is None or not worth_extracting(item) or writer.has_extraction(item.id, item.version, prompt.version):
        writer.backlog.pop(item.id, None)
        return
    writer.backlog[item.id] = item.model_dump_json()


def output_schema() -> dict[str, Any]:
    """The JSON shape the model must answer in. Structured outputs take no
    length or range limits, and every object closes its properties, so
    attributes are key-value pairs rather than an open object."""
    onto = ontology.load()
    quotes = {"type": "array", "items": {"type": "string"}}

    def obj(properties: dict[str, Any]) -> dict[str, Any]:
        return {"type": "object", "properties": properties, "required": sorted(properties),
                "additionalProperties": False}

    entity = obj({
        "id": {"type": "string", "description": "e1, e2, ... unique in this answer"},
        "type": {"type": "string", "enum": sorted(onto.llm_entities)},
        "name": {"type": "string"},
        "attributes": {"type": "array", "items": obj({"key": {"type": "string"}, "value": {"type": "string"}})},
        "evidence": quotes,
    })
    relation = obj({
        "type": {"type": "string", "enum": sorted(onto.llm_relations)},
        "source": {"type": "string", "description": "An entity id from this answer, or a known id"},
        "target": {"type": "string", "description": "An entity id from this answer, or a known id"},
        "confidence": {"type": "number"},
        "evidence": quotes,
    })
    return obj({"entities": {"type": "array", "items": entity},
                "relations": {"type": "array", "items": relation}})


def known_ids(item: ItemRecord) -> dict[str, str]:
    """The ids the model may point a relation at, with what each one is."""
    known = {item.id: f"this {item.kind}: {item.title}"}
    for p in item.people:
        known[p.id] = f"person: {p.name or p.handle} ({', '.join(p.roles)})"
    return known


def message(item: ItemRecord) -> str:
    lines = [f"Item: {item.kind}, {item.title}", "", "Known ids:"]
    lines += [f"- {k}  {v}" for k, v in known_ids(item).items()]
    lines += ["", "<item_text>", item.text, "</item_text>"]
    return "\n".join(lines)


def _spans(text: str, quotes: Any) -> list[Span]:
    """Place each quote in the text. Offsets are str indexes, which count
    code points, as the contract says."""
    spans = []
    for quote in quotes if isinstance(quotes, list) else []:
        if not isinstance(quote, str) or not quote.strip():
            continue
        quote = quote.strip()
        start = text.find(quote)
        if start >= 0:
            spans.append(Span(start=start, end=start + len(quote), quote=quote))
        if len(spans) == MAX_QUOTES:
            break
    return spans


def build_record(item: ItemRecord, answer: dict[str, Any], *, model: str, prompt_version: str,
                 run_id: str, now: datetime) -> tuple[ExtractionRecord, Counter]:
    """The extraction record from the model's answer, keeping only what
    checks out. Returns the record and a count of what was dropped, by why."""
    onto = ontology.load()
    dropped: Counter = Counter()
    entities: list[Entity] = []
    renamed: dict[str, str] = {}
    for raw in answer.get("entities") or []:
        evidence = _spans(item.text, raw.get("evidence"))
        if raw.get("type") not in onto.llm_entities:
            dropped["entity_type"] += 1
            continue
        if not evidence:
            dropped["entity_evidence"] += 1
            continue
        if not str(raw.get("name") or "").strip():
            dropped["entity_name"] += 1
            continue
        local_id = f"e{len(entities) + 1}"
        renamed[str(raw.get("id"))] = local_id
        attributes = {str(a["key"]): a["value"] for a in raw.get("attributes") or []
                      if isinstance(a, dict) and a.get("key")}
        entities.append(Entity(local_id=local_id, type=raw["type"], name=raw["name"].strip(),
                               attributes=attributes, evidence=evidence))

    types = {e.local_id: frozenset({e.type}) for e in entities}
    known = known_ids(item)
    relations: list[Relation] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in answer.get("relations") or []:
        ends = []
        for end in (raw.get("source"), raw.get("target")):
            end = str(end)
            ends.append(renamed.get(end) or (end if end in known else None))
        source, target = ends
        if source is None or target is None:
            dropped["relation_endpoint"] += 1
            continue
        kind = raw.get("type")
        if kind not in onto.llm_relations:
            dropped["relation_type"] += 1
            continue
        from_types = types.get(source) or ids.entity_types(source)
        to_types = types.get(target) or ids.entity_types(target)
        if not onto.allows(kind, set(from_types), set(to_types)):
            dropped["relation_not_allowed"] += 1
            continue
        evidence = _spans(item.text, raw.get("evidence"))
        if not evidence:
            dropped["relation_evidence"] += 1
            continue
        try:
            confidence = min(max(float(raw.get("confidence")), 0.0), 1.0)
        except (TypeError, ValueError):
            dropped["relation_confidence"] += 1
            continue
        if (kind, source, target) in seen:
            continue
        seen.add((kind, source, target))
        relations.append(Relation(type=kind, source=source, target=target, confidence=confidence,
                                  evidence=evidence))

    record = ExtractionRecord(
        item_id=item.id, item_version=item.version, source=item.source,
        extractor=Extractor(model=model, prompt_version=prompt_version),
        entities=entities, relations=relations, extracted_at=now, run_id=run_id)
    return record, dropped


class ExtractStage:
    """Stage 8 for one run. The runner offers it every item it writes or
    sees again; finish() reports what the manifest needs."""

    def __init__(self, model: Model, writer: RunWriter, *, budget: int | None = None,
                 now: datetime | None = None, batch_wait: float = BATCH_WAIT,
                 sleep: Callable[[float], None] | None = None, clock: Callable[[], float] | None = None):
        self.model = model
        self.writer = writer
        self.budget = budget
        self.now = now or datetime.now(timezone.utc)
        self.batch = bool(getattr(model, "batch", False))
        self.batch_wait = batch_wait
        # Looked up when the stage is made, so tests can step a fake clock.
        self._sleep = sleep or time.sleep
        self._clock = clock or time.monotonic
        self.tokens = Tokens()
        self.estimated = 0                                   # tokens queued for this run's batch
        self.queued: list[tuple[ItemRecord, str]] = []       # (item, prompt version), for the batch
        self.versions: set[str] = set()
        self.budget_hit = False
        self.unavailable = False
        self.dropped: Counter = Counter()
        self.skipped: Counter = Counter()

    @property
    def stopped(self) -> bool:
        return self.budget_hit or self.unavailable

    def _in_flight(self) -> set[tuple[str, str]]:
        """(item id, item version) of every request in a batch not yet read."""
        return {(item_id, version) for mapping in self.writer.batches.values()
                for item_id, _, version in mapping.values()}

    def work_backlog(self) -> None:
        """Batches and items left by earlier runs, before this run's own."""
        for batch_id in sorted(self.writer.batches) if hasattr(self.model, "results") else []:
            try:
                if self.model.ended(batch_id):
                    self.collect(batch_id)
            except ModelUnavailable as e:
                log.warning("model unavailable (%s) checking batch %s; it is read next run", e, batch_id)
                self.unavailable = True
                break
        waiting = {item_id for item_id, _ in self._in_flight()}
        for item_id, line in sorted(self.writer.backlog.items()):
            if item_id in waiting:
                continue                                      # its batch is still running
            seen = self.writer.seen_items.get(item_id)
            if not seen or seen[0] != json.loads(line).get("content_hash"):
                del self.writer.backlog[item_id]          # changed since; its new version comes by fetch
                continue
            self.offer(ItemRecord.model_validate_json(line))

    def offer(self, item: ItemRecord) -> None:
        prompt = prompts.for_kind(item.kind)
        if prompt is None or not worth_extracting(item):
            self.writer.backlog.pop(item.id, None)
            return
        if self.writer.has_extraction(item.id, item.version, prompt.version):
            self.writer.backlog.pop(item.id, None)
            return
        if (item.id, item.version) in self._in_flight() or any(q.id == item.id for q, _ in self.queued):
            return                                            # already asked; the answer is on its way
        if self.stopped:
            self.writer.backlog[item.id] = item.model_dump_json()
            return
        if self.batch:
            self._queue(item, prompt)
            return
        try:
            done = self.model.extract(prompt.system, message(item), output_schema())
        except ModelUnavailable as e:
            log.warning("model unavailable (%s); the rest of this run's extractions wait in the backlog", e)
            self.unavailable = True
            self.writer.backlog[item.id] = item.model_dump_json()
            return
        self.writer.backlog.pop(item.id, None)
        self._answer(item, prompt.version, done)

    def _answer(self, item: ItemRecord, prompt_version: str, done: Completion) -> None:
        """Write what one answer supports, or settle the item when there is none."""
        self._spend(done)
        if done.data is None:
            # A refusal, a cut-off answer or an invalid request: asking again
            # would get the same, so it is settled until the item or the prompt
            # changes.
            log.warning("no extraction for %s: stop_reason %s", item.id, done.stop_reason)
            self.skipped[done.stop_reason or "no_answer"] += 1
            self.writer.settle_extraction(item.id, item.version, prompt_version)
            return
        record, dropped = build_record(item, done.data, model=done.model, prompt_version=prompt_version,
                                       run_id=self.writer.run_id, now=self.now)
        self.dropped.update(dropped)
        if self.writer.add_extraction(record):
            self.versions.add(prompt_version)
        log.info("extracted %s: %d entities, %d relations, %d+%d tokens", item.id, len(record.entities),
                 len(record.relations), done.tokens.input, done.tokens.output)

    def _spend(self, done: Completion) -> None:
        self.tokens = Tokens(input=self.tokens.input + done.tokens.input,
                             output=self.tokens.output + done.tokens.output)
        if self.budget is not None and self.tokens.input + self.tokens.output >= self.budget:
            if not self.budget_hit:
                log.warning("token budget %d reached; the rest of this run's extractions wait in the backlog",
                            self.budget)
            self.budget_hit = True

    # -- batches ------------------------------------------------------------ #

    def _queue(self, item: ItemRecord, prompt: prompts.Prompt) -> None:
        estimate = (len(prompt.system) + len(message(item))) // 3 + OUTPUT_ESTIMATE
        spent = self.tokens.input + self.tokens.output
        if self.budget is not None and spent + self.estimated + estimate > self.budget:
            log.warning("token budget %d would be passed; the rest of this run's extractions wait in the backlog",
                        self.budget)
            self.budget_hit = True
            self.writer.backlog[item.id] = item.model_dump_json()
            return
        self.estimated += estimate
        self.queued.append((item, prompt.version))
        # Kept until its answer is read, so a batch that outlives the run loses nothing.
        self.writer.backlog[item.id] = item.model_dump_json()

    def send_batch(self) -> None:
        """Send what this run queued as one batch, wait for it, and read it.
        The runner calls this once fetching is done, before writing."""
        if not self.queued:
            return
        mapping = {f"r{i:05d}": [item.id, prompt_version, item.version]
                   for i, (item, prompt_version) in enumerate(self.queued)}
        requests = [BatchRequest(custom_id=cid, system=prompts.for_kind(item.kind).system, text=message(item),
                                 schema=output_schema())
                    for cid, (item, _) in zip(mapping, self.queued, strict=True)]
        self.queued = []
        try:
            batch_id = self.model.submit(requests)
        except ModelUnavailable as e:
            log.warning("model unavailable (%s); this run's extractions wait in the backlog", e)
            self.unavailable = True
            return
        self.writer.batches[batch_id] = mapping
        log.info("sent batch %s: %d items, about %d tokens", batch_id, len(requests), self.estimated)
        deadline = self._clock() + self.batch_wait
        try:
            while not self.model.ended(batch_id):
                if self._clock() >= deadline:
                    log.warning("batch %s still running after %.0f minutes; the next run reads it",
                                batch_id, self.batch_wait / 60)
                    return
                self._sleep(BATCH_POLL)
            self.collect(batch_id)
        except ModelUnavailable as e:
            log.warning("model unavailable (%s) waiting on batch %s; the next run reads it", e, batch_id)
            self.unavailable = True

    def collect(self, batch_id: str) -> None:
        """Read an ended batch: write each answer, keep unanswered items in
        the backlog for a new request, and forget the batch."""
        mapping = self.writer.batches[batch_id]
        for custom_id, done in self.model.results(batch_id):
            if custom_id not in mapping:
                continue
            item_id, prompt_version, item_version = mapping[custom_id]
            line = self.writer.backlog.get(item_id)
            if line is None:
                continue
            item = ItemRecord.model_validate_json(line)
            if item.version != item_version:
                continue                                      # a newer version was queued since; this answer is stale
            seen = self.writer.seen_items.get(item_id)
            if not seen or seen[0] != item.content_hash:
                del self.writer.backlog[item_id]              # changed since it was sent
                continue
            if done is None:
                self.skipped["batch_unanswered"] += 1         # stays in the backlog; asked again next run
                continue
            del self.writer.backlog[item_id]
            self._answer(item, prompt_version, done)
        del self.writer.batches[batch_id]

    def finish(self) -> dict[str, Any]:
        if self.dropped:
            log.info("dropped from model answers: %s", json.dumps(dict(sorted(self.dropped.items()))))
        if self.skipped:
            log.info("items with no answer: %s", json.dumps(dict(sorted(self.skipped.items()))))
        return {"extractor_versions": sorted(self.versions), "tokens": self.tokens,
                "llm_budget_hit": self.budget_hit}
