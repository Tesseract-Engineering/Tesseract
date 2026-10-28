"""
judge.py - the recommendation agent's model steps, around the plain code in patterns.py and score.py.

    requests ──READ──▶ a plain phrase per item ──patterns.detect──▶ groups ──MERGE──▶ fewer groups
             ──score.rank──▶ top groups ──REVIEW──▶ name, steps, same_steps ──score.rank──▶ report

The model goes through swarm/model.py, so it is the same client, backend switch (anthropic, bedrock,
replay, off) and recordings as the swarm's extraction. Every answer is checked here before it is used:
a merge must name groups that exist, steps must cite items of the group, and nothing the model says
changes a count. When the model is down the rest of the run is plain code, and the report says so.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import prompts
import rec_weights as W
from patterns import Pattern, words
from rec_weights import when

from swarm.model import Model, ModelUnavailable


def _handled(item: dict) -> str:
    """The last reply or comment: how the ask was handled, when the item says ("Same script with
    --report logins"). Slack lines are "Name: message"; Jira comments follow a "--- comment ---" line."""
    lines = [ln.strip() for ln in (item.get("text") or "").splitlines() if ln.strip() and not ln.startswith("---")]
    if len(lines) < 2:
        return ""
    last = lines[-1]
    return last.split(": ", 1)[1] if item.get("source") == "slack" and ": " in last else last


def _sample(item: dict) -> str:
    what = item.get("_request") or item.get("title") or ""
    how = _handled(item)
    return f"{what} (handled: {_cut(how, W.MERGE_HANDLED_CHARS)})" if how else what


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n[cut here: {len(text) - limit} more characters]"


@dataclass
class Judge:
    model: Model
    counts: Counter = field(default_factory=Counter)
    notes: list[str] = field(default_factory=list)
    stopped: bool = False

    def _ask(self, step: str, system: str, text: str, schema: dict) -> tuple[dict | None, str]:
        if self.stopped:
            self.counts[f"{step}: not asked, model unavailable"] += 1
            return None, ""
        try:
            done = self.model.extract(system, text, schema)
        except ModelUnavailable as e:
            self.stopped = True
            self.notes.append(f"model unavailable during {step} ({e}); the rest of this run is plain code")
            return None, ""
        self.counts[f"{step}: asked"] += 1
        self.counts["tokens in"] += done.tokens.input
        self.counts["tokens out"] += done.tokens.output
        if done.data is None:
            self.counts[f"{step}: no answer ({done.stop_reason})"] += 1
        return done.data, done.model

    # -- READ ------------------------------------------------------------------------------------- #

    def read(self, requests: list[dict]) -> list[dict]:
        """Each item with `_request` set to the model's phrase for it. Items the model says ask for
        nothing are dropped; items it was not asked about, or did not answer, are kept as they are."""
        out = []
        for n, item in enumerate(requests):
            if n >= W.MAX_READ_ITEMS:
                if n == W.MAX_READ_ITEMS:
                    self.notes.append(f"read the first {W.MAX_READ_ITEMS} of {len(requests)} items; "
                                      "the rest are grouped by title alone")
                out.append(item)
                continue
            text = (f"Source: {item.get('source')} {item.get('kind')}\nTitle: {item.get('title') or ''}\n\n"
                    + _cut(item.get("text") or "", W.READ_TEXT_CHARS))
            data, _ = self._ask("read", prompts.READ, text, prompts.READ_SCHEMA)
            if data is None:
                out.append(item)
            elif not data.get("is_request"):
                self.counts["read: not a request, dropped"] += 1
            elif not words({"title": data.get("request") or ""}):
                self.counts["read: request with no usable phrase, kept by title"] += 1
                out.append(item)
            else:
                out.append({**item, "_request": data["request"].strip(), "_system": (data.get("system") or "").strip()})
        return out

    # -- MERGE ------------------------------------------------------------------------------------ #

    def merge(self, patterns: list[Pattern]) -> list[Pattern]:
        """Join groups the model says are the same request. Singletons are shown too: a request
        worded unlike the rest is exactly what plain code leaves on its own."""
        if len(patterns) < 2:
            return patterns
        shown = sorted(patterns, key=lambda p: -len(p.items))[:W.MAX_MERGE_GROUPS]
        if len(patterns) > len(shown):
            self.notes.append(f"showed the {len(shown)} largest of {len(patterns)} groups to MERGE")
        lines = []
        for n, p in enumerate(shown, 1):
            samples = list(dict.fromkeys(_sample(i) for i in p.items))[:3]
            # the model's phrases leave systems out; plain titles may not, which is fine: merging
            # "read access to Grafana" with "read access to the replica" is what MERGE is for
            lines.append(f"Group {n} ({len(p.items)} items): " + " | ".join(samples))
        data, model = self._ask("merge", prompts.MERGE, "\n".join(lines), prompts.MERGE_SCHEMA)
        if not data:
            return patterns
        used: set[int] = set()
        gone: set[int] = set()
        for m in data.get("merges") or []:
            nums = list(dict.fromkeys(m.get("groups") or []))
            if len(nums) < 2 or any(not 1 <= g <= len(shown) or g in used for g in nums):
                self.counts["merge: refused (unknown, repeated or single group)"] += 1
                continue
            used.update(nums)
            keep, *rest = (shown[g - 1] for g in nums)
            for other in rest:
                keep.merges.append({"joined": other.label, "items": [i["id"] for i in other.items],
                                    "reason": m.get("reason", ""), "model": model})
                keep.items.extend(other.items)
                keep.sims.extend(other.sims)
                gone.add(id(other))
            keep.items.sort(key=when)
            self.counts["merge: groups joined"] += len(rest)
        return [p for p in patterns if id(p) not in gone]

    # -- REVIEW ----------------------------------------------------------------------------------- #

    def review(self, p: Pattern, rb: dict | None = None, rb_text: str = "") -> dict | None:
        """Name, steps and same_steps for one pattern, or None. Steps must cite the group's own items or
        its runbook; steps with no valid citation are dropped and same_steps is held to what that means."""
        items = sorted(p.items, key=when, reverse=True)[:W.REVIEW_ITEMS]
        text = "\n\n".join(f"Item {i['id']}\nTitle: {i.get('title') or ''}\n"
                           + _cut(i.get("text") or "", W.REVIEW_TEXT_CHARS) for i in items)
        if rb:
            text = f"Runbook {rb['id']}\nTitle: {rb['title']}\n{_cut(rb_text, W.REVIEW_TEXT_CHARS)}\n\n" + text
        data, model = self._ask("review", prompts.REVIEW, text, prompts.REVIEW_SCHEMA)
        if not data:
            return None
        given = {i["id"] for i in items} | ({rb["id"]} if rb else set())
        cited = [e for e in dict.fromkeys(data.get("evidence") or []) if e in given]
        if len(cited) < len(data.get("evidence") or []):
            self.counts["review: cited ids not in the group, dropped"] += len(data["evidence"]) - len(cited)
        steps = [s.strip() for s in data.get("steps") or [] if isinstance(s, str) and s.strip()]
        same = min(max(float(data.get("same_steps") or 0.0), 0.0), 1.0)
        if steps and not cited:
            self.counts["review: steps with no evidence, dropped"] += 1
            steps, same = [], min(same, 0.3)
        return {"name": (data.get("name") or "").strip() or None, "same_steps": round(same, 4), "steps": steps,
                "evidence": cited, "model": model}

    def summary(self) -> str:
        parts = [f"{k} {v}" for k, v in sorted(self.counts.items())]
        return "; ".join(parts + self.notes) or "model not asked"
