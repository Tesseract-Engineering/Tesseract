"""
extract.py - stage 8, LLM extraction (docs/SWARM.md, "Pipeline stages").

For each item a run writes or sees again, in order:

  cost filter     kinds with no prompt, and trivial items, are never sent:
                  Slack conversations under 3 messages or 200 characters,
                  empty PRs and issues, documents with no text
  already done    an extraction for this item version and prompt version
                  exists, so an unchanged item is not re-extracted; a new
                  prompt version re-extracts what the run fetches
  budget          once the run's tokens reach SWARM_LLM_TOKEN_BUDGET, the
                  rest wait in the backlog; fetching goes on
  extract         the model answers in a fixed JSON shape, with quotes as
                  evidence; code finds each quote in the item text to make
                  the span, and drops what it cannot place

The backlog is items that were due an extraction but did not get one: over
budget, or the model was unavailable. It lives in state/{source}/ with the
checkpoints and is worked first on the next run, so nothing waits for its
item to change again. An item that changed since it was queued is dropped
from the backlog; its new version is extracted when fetched.

What the model gets wrong is dropped, never written: an entity or relation
whose evidence is not in the text, a type outside the ontology, a relation
between ids it was not given, or one the ontology does not allow between
those types. Counts are logged, never contents.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Any

from .. import ids, ontology
from ..model import Completion, Model, ModelUnavailable
from ..records import Entity, ExtractionRecord, Extractor, ItemRecord, Relation, Span, Tokens
from . import prompts
from .write import RunWriter

log = logging.getLogger("swarm.extract")

MIN_MESSAGES = 3
MIN_CHARS = 200
MAX_QUOTES = 3


def worth_extracting(item: ItemRecord) -> bool:
    """The cost filter. Deterministic extraction runs on every item anyway."""
    if prompts.for_kind(item.kind) is None or not item.text.strip():
        return False
    if item.kind == "thread":
        return item.meta.get("message_count", 0) >= MIN_MESSAGES and len(item.text) >= MIN_CHARS
    return True


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
                 now: datetime | None = None):
        self.model = model
        self.writer = writer
        self.budget = budget
        self.now = now or datetime.now(timezone.utc)
        self.tokens = Tokens()
        self.versions: set[str] = set()
        self.budget_hit = False
        self.unavailable = False
        self.dropped: Counter = Counter()
        self.skipped: Counter = Counter()

    @property
    def stopped(self) -> bool:
        return self.budget_hit or self.unavailable

    def work_backlog(self) -> None:
        """Items queued by earlier runs, before this run's own."""
        for item_id, line in sorted(self.writer.backlog.items()):
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
        if self.stopped:
            self.writer.backlog[item.id] = item.model_dump_json()
            return
        try:
            done = self.model.extract(prompt.system, message(item), output_schema())
        except ModelUnavailable as e:
            log.warning("model unavailable (%s); the rest of this run's extractions wait in the backlog", e)
            self.unavailable = True
            self.writer.backlog[item.id] = item.model_dump_json()
            return
        self._spend(done)
        self.writer.backlog.pop(item.id, None)
        if done.data is None:
            # A refusal or a cut-off answer: asking again would get the same,
            # so it is settled until the item or the prompt changes.
            log.warning("no extraction for %s: stop_reason %s", item.id, done.stop_reason)
            self.skipped[done.stop_reason or "no_answer"] += 1
            self.writer.settle_extraction(item.id, item.version, prompt.version)
            return
        record, dropped = build_record(item, done.data, model=done.model, prompt_version=prompt.version,
                                       run_id=self.writer.run_id, now=self.now)
        self.dropped.update(dropped)
        if self.writer.add_extraction(record):
            self.versions.add(prompt.version)
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

    def finish(self) -> dict[str, Any]:
        if self.dropped:
            log.info("dropped from model answers: %s", json.dumps(dict(sorted(self.dropped.items()))))
        if self.skipped:
            log.info("items with no answer: %s", json.dumps(dict(sorted(self.skipped.items()))))
        return {"extractor_versions": sorted(self.versions), "tokens": self.tokens,
                "llm_budget_hit": self.budget_hit}
