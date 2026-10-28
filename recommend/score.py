"""
score.py - rank patterns by how often they come back and how well-defined their steps are.

Each axis is 0..1 and the composite is their weighted sum (rec_weights.SCORE_WEIGHTS). Frequency
counts asks, not items: an ask and the ticket filed from it are one (linking.py). The evidence and
the "why" lines are built from the same numbers the score uses, so what a person reads is what was
ranked. A model review (judge.py) may name the pattern and judge its steps; it moves the definition
axis by MODEL_DEFINITION_SHARE and never touches a count.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

import rec_weights as W
from directory import Directory, norm_name
from linking import Links, asks
from owners import Owner, system_of
from patterns import Pattern, requesters, resolvers, stepped
from rec_weights import when
from runbooks import runbook


@dataclass
class Context:
    """What scoring reads besides the pattern, all built from items the viewer may read."""
    directory: Directory
    links: Links
    owners: list[Owner]
    viewer: str | None = None                # an email, or None for everything the swarm can read


@dataclass
class Opportunity:
    label: str
    score: float
    frequency: int                           # asks
    definition: float
    parts: dict[str, float]                  # the axes, 0..1, before weighting
    signals: dict[str, float]                # the definition signals, 0..1
    requesters: int
    resolver: str | None                     # who handles it today, if one person does (a name)
    evidence: str
    why: list[str] = field(default_factory=list)
    approvers: list[dict] = field(default_factory=list)   # people (names), systems {name: asks}, how, source, url
    unrouted: int = 0                        # asks naming no system with a known owner
    runbook: dict | None = None
    steps: list[str] = field(default_factory=list)
    steps_from: str | None = None            # "runbook" | "model"
    steps_evidence: list[str] = field(default_factory=list)
    items: list[dict] = field(default_factory=list)      # id, title, url, source, created_at, visibility, ask
    merges: list[dict] = field(default_factory=list)     # groups the model joined, with its reason
    judged_by: str | None = None


def _share(counts: Counter, total: int) -> float:
    return counts.most_common(1)[0][1] / total if counts and total else 0.0


_APPROVES = re.compile(r"\b([A-Z][a-z]+(?: [A-Z][a-z]+)?) (?:approves|must approve|signs off)\b")


def _runbook_owner(rb: dict | None, ctx: Context) -> Owner | None:
    """For asks that name no system: the approver the runbook names ("Hamilton approves the request"),
    else whoever owns the runbook (a scheduled key rotation's runbook is its owner's)."""
    if not rb:
        return None
    doc = ctx.links.items[rb["id"]]
    for m in _APPROVES.finditer(doc.get("text") or ""):
        if key := ctx.directory.find(m.group(1)):
            return Owner(rb["title"], [key], rb["id"], rb["url"], "named in runbook")
    if ":file:" in rb["id"]:                 # a file in a repo: the repo's owner, not whoever committed it
        repo = (ctx.links.items.get(rb["id"].split(":file:")[0]) or {}).get("title", "").split("/")[-1]
        hit = next((o for o in ctx.owners if norm_name(repo) in o.aliases or norm_name(f"{repo} repo") in o.aliases), None)
        if repo and hit:
            return Owner(rb["title"], hit.people, hit.source, hit.url, f"owner of the {repo} repo, per the {hit.how}")
    people = [p["id"] for p in doc.get("people") or [] if "owner" in (p.get("roles") or [])] or \
             [p["id"] for p in doc.get("people") or [] if "author" in (p.get("roles") or [])]
    if not people:
        return None
    return Owner(rb["title"], [ctx.directory.key(p) for p in people[:1]], rb["id"], rb["url"], "runbook owner")


def _approvers(units: list[list[dict]], ctx: Context, rb: dict | None) -> tuple[list[dict], int]:
    by_owner: dict[int, tuple[Owner, Counter]] = {}
    unrouted = 0
    fallback = _runbook_owner(rb, ctx)
    for unit in units:
        # The ask's subject first; then the runbook it follows; a system only its body names comes last.
        owner = (next((o for i in unit if (o := system_of(i, ctx.owners, deep=False))), None) or fallback
                 or next((o for i in unit if (o := system_of(i, ctx.owners))), None))
        if owner is None:
            unrouted += 1
            continue
        by_owner.setdefault(id(owner), (owner, Counter()))[1][owner.system] += 1
    groups: dict[tuple, dict] = {}
    for owner, systems in by_owner.values():
        people = tuple(ctx.directory.name(p) for p in owner.people)
        g = groups.setdefault(people, {"people": list(people), "systems": Counter(), "sources": []})
        g["systems"].update(systems)
        g["sources"].append({"system": owner.system, "how": owner.how, "id": owner.source, "url": owner.url})
    out = sorted(groups.values(), key=lambda g: -sum(g["systems"].values()))
    for g in out:
        g["systems"] = dict(g["systems"].most_common())
    return out, unrouted


def _why(signals: dict, rb: dict | None, defined: float, n: int) -> list[str]:
    why = []
    if rb and rb["steps"]:
        why.append(f"{len(rb['steps'])} steps written down in \"{rb['title']}\", linked from {rb['linked_by']} of {n} asks")
    elif rb:
        why.append(f"linked runbook \"{rb['title']}\", but no list of steps in it")
    if signals["stepped_text"] >= 0.5:
        why.append("most instances list their steps")
    if (m := signals.get("model_same_steps")) is not None:
        why.append(f"read by the model: handled {'the same way' if m >= 0.7 else 'differently' if m <= 0.4 else 'similarly'} "
                   f"each time ({m:.2f})")
    if defined < W.LOW_DEFINITION:
        varies = (signals.get("model_same_steps", 1.0) <= 0.4) or (not rb and signals["stepped_text"] < 0.3)
        why.insert(0, "ranks low: " + ("steps vary between instances" if varies else "no written steps"))
    return why


def score(p: Pattern, ctx: Context, review: dict | None = None) -> Opportunity:
    units = asks(p.items, ctx.links)
    n = len(units)
    rb = runbook(units, ctx.links)
    handlers = Counter(ctx.directory.key(r) for i in p.items for r in resolvers(i))
    places = Counter(i.get("container") for i in p.items if i.get("container"))
    signals = {
        "runbook": (1.0 if rb["steps"] else W.RUNBOOK_NO_STEPS) if rb else 0.0,
        "stepped_text": sum(stepped(i) for i in p.items) / len(p.items),
        "same_resolver": _share(handlers, len(p.items)),
        "tight_wording": sum(p.sims) / len(p.items),
        "same_place": _share(places, len(p.items)),
    }
    defined = sum(W.DEFINITION_WEIGHTS[k] * v for k, v in signals.items())
    if review:
        signals["model_same_steps"] = review["same_steps"]
        defined = (1 - W.MODEL_DEFINITION_SHARE) * defined + W.MODEL_DEFINITION_SHARE * review["same_steps"]
    parts = {"frequency": min(1.0, math.log1p(n) / math.log1p(W.FREQ_SATURATES_AT)), "definition": defined}
    total = sum(W.SCORE_WEIGHTS[k] * v for k, v in parts.items())
    asked_by = {ctx.directory.key(r) for i in p.items for r in requesters(i)}
    resolver = ctx.directory.name(handlers.most_common(1)[0][0]) if handlers and signals["same_resolver"] >= 0.8 else None
    approvers, unrouted = _approvers(units, ctx, rb)
    sources = sorted({i["source"] for i in p.items})
    evidence = (f"asked {n} times in the last {W.WINDOW_DAYS} days" + (f" by {len(asked_by)} {'person' if len(asked_by) == 1 else 'people'}" if asked_by else "")
                + f", across {', '.join(sources)}"
                + (f" ({len(p.items)} items; {len(p.items) - n} filed from or duplicating another ask)" if len(p.items) > n else ""))
    evidence = evidence.replace("asked 1 times", "asked once")
    if review and review.get("steps"):
        steps, steps_from, steps_evidence = review["steps"], "model", review["evidence"]
    elif rb and rb["steps"]:
        steps, steps_from, steps_evidence = rb["steps"], "runbook", [rb["id"]]
    else:
        steps, steps_from, steps_evidence = [], None, []
    ask_of = {i["id"]: k for k, unit in enumerate(units, 1) for i in unit}
    return Opportunity(
        label=(review or {}).get("name") or p.label, score=round(total, 4), frequency=n, definition=round(defined, 4),
        parts={k: round(v, 4) for k, v in parts.items()}, signals={k: round(v, 4) for k, v in signals.items()},
        requesters=len(asked_by), resolver=resolver, evidence=evidence, why=_why(signals, rb, defined, n),
        approvers=approvers, unrouted=unrouted, runbook=rb,
        steps=steps, steps_from=steps_from, steps_evidence=steps_evidence,
        items=[{"id": i["id"], "title": i.get("title"), "url": i.get("url"), "source": i["source"],
                "created_at": i.get("created_at"), "visibility": (i.get("acl") or {}).get("visibility"),
                "ask": ask_of[i["id"]]}
               for i in sorted(p.items, key=when)],
        merges=p.merges, judged_by=(review or {}).get("model"))


def rank(patterns: list[Pattern], ctx: Context, reviews: dict[int, dict] | None = None) -> list[Opportunity]:
    """`reviews` is keyed by id(pattern). Patterns with fewer than MIN_OCCURRENCES asks are dropped:
    one project mentioned three times, all linked, is one ask."""
    reviews = reviews or {}
    ops = (score(p, ctx, reviews.get(id(p))) for p in patterns)
    return sorted((o for o in ops if o.frequency >= W.MIN_OCCURRENCES), key=lambda o: (-o.score, -o.frequency, o.label))
