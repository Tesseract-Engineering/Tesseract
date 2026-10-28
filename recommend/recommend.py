"""
recommend.py - read the swarm's runs, find repeated manual work, print a ranked evidence report.

    python recommend/recommend.py --root samples/run-0
    python recommend/recommend.py --root s3 --env .env --json out.json
    SWARM_MODEL_BACKEND=replay SWARM_MODEL_REPLAY=data/replay python recommend/recommend.py --root ...

The model is the swarm's (swarm/model.py, the same SWARM_MODEL_* settings); --no-llm or
SWARM_MODEL_BACKEND=off runs plain code alone. See judge.py for what the model may and may not change.

A person picks which opportunities to build; nothing here acts. Evidence lists every item behind a
count, with its visibility, so the report can be filtered per reader before it is shown to one.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "graph"), str(HERE.parent)]

import rec_weights as W  # noqa: E402
from access import viewer, visible  # noqa: E402
from directory import Directory  # noqa: E402
from judge import Judge  # noqa: E402
from linking import Links, asks  # noqa: E402
from owners import owners  # noqa: E402
from patterns import detect, requests  # noqa: E402
from rec_weights import parse_time  # noqa: E402
from runbooks import runbook  # noqa: E402
from score import Context, Opportunity, rank, score  # noqa: E402
from swarm_records import State, load, open_store  # noqa: E402


def context(state: State, as_email: str | None = None) -> tuple[dict[str, dict], Context]:
    """The items the viewer may read, and everything scoring reads, built from those items only."""
    person_of, clusters = getattr(state, "person_of", {}), getattr(state, "clusters", {})
    v = viewer(as_email, getattr(state, "groups", {}), Directory(state.items, person_of, clusters)) if as_email else None
    items = visible(state.items, v)
    directory = Directory(items, person_of, clusters)
    extractions = {k: e for k, e in (getattr(state, "extractions", {}) or {}).items() if k in items}
    return items, Context(directory, Links(items), owners(items, directory, extractions), as_email)


def opportunities(state: State, as_of: datetime | None = None, judge: Judge | None = None,
                  as_email: str | None = None) -> list[Opportunity]:
    """Plain code alone, or with a Judge: READ before grouping, MERGE after, REVIEW of the top patterns.
    With `as_email`, only what that person may read is counted, linked or shown."""
    when = as_of or parse_time(state.as_of) or datetime.now(timezone.utc)
    items, ctx = context(state, as_email)
    reqs = requests(items, when)
    systems = tuple(sorted({a for o in ctx.owners for a in o.aliases}, key=lambda a: (-len(a), a)))
    if judge is None:
        return rank(detect(reqs, links=ctx.links, systems=systems), ctx)
    patterns = detect(_keep_asks(reqs, judge.read(reqs), ctx.links), merge_hook=judge.merge, links=ctx.links,
                      systems=systems)
    reviews = {}
    for p in sorted(patterns, key=lambda p: -score(p, ctx).score)[:W.REVIEW_TOP]:
        rb = runbook(asks(p.items, ctx.links), ctx.links)
        if r := judge.review(p, rb, items[rb["id"]].get("text") or "" if rb else ""):
            reviews[id(p)] = r
    return rank(patterns, ctx, reviews)


# How each approver source reads in the report.
_SOURCE = {"owners table": "the owners table", "CODEOWNERS": "CODEOWNERS", "extracted": "extracted from the records",
           "named in runbook": "the runbook, which names them", "runbook owner": "the runbook's owner"}


def _from_runbook(how: str) -> bool:
    return how in ("named in runbook", "runbook owner") or how.startswith("owner of the ")


def _n(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _keep_asks(before: list[dict], kept: list[dict], links: Links) -> list[dict]:
    """READ judges items one by one, so it may drop a Slack thread ("Should that go through CS?") whose
    ticket it keeps. The thread is still where the ask started: an item READ dropped comes back when
    another item of its ask was kept."""
    kept_ids = {i["id"] for i in kept}
    back = [i for unit in asks(before, links) if any(i["id"] in kept_ids for i in unit)
            for i in unit if i["id"] not in kept_ids]
    return kept + back


def markdown(ops: list[Opportunity], limit: int = 10, viewer_email: str | None = None) -> str:
    seen_as = f"as {viewer_email} can see it" if viewer_email else "everything the swarm can read"
    if not ops:
        return f"No repeated requests found in the window ({seen_as}).\n"
    out = ["# Automation opportunities\n", f"_{seen_as}_\n"]
    for n, o in enumerate(ops[:limit], 1):
        out.append(f"## {n}. {o.label}  (score {o.score:.2f})\n")
        out.append(f"**{o.evidence}.**\n")
        for w in o.why:
            out.append(f"- {w}")
        out.append("")
        if o.approvers or o.unrouted:
            out.append("Approver, from ownership:")
            for a in o.approvers:
                how = {src["system"]: src["how"] for src in a["sources"]}
                systems = ", ".join((f"asks following \"{s}\" ({k})" if _from_runbook(how.get(s, "")) else f"{s} ({k})")
                                    for s, k in a["systems"].items())
                where = "; ".join(dict.fromkeys(f"{_SOURCE.get(src['how'], src['how'])} <{src['url'] or src['id']}>"
                                                for src in a["sources"]))
                out.append(f"- {' and '.join(a['people'])}: {systems}. Source: {where}")
            if o.unrouted:
                out.append(f"- {_n(o.unrouted, 'ask names', 'asks name')} no system with a known owner")
            out.append("")
        if o.steps:
            if o.steps_from == "runbook":
                src = f"\"{o.runbook['title']}\""
            else:
                rb = [e for e in o.steps_evidence if o.runbook and e == o.runbook["id"]]
                n = len(o.steps_evidence) - len(rb)
                src = f"read by {o.judged_by} from " + " and ".join(
                    ([f"\"{o.runbook['title']}\""] if rb else []) + ([f"{n} of its items"] if n else []))
            out.append(f"Steps ({src}):")
            out.extend(f"{k}. {s}" for k, s in enumerate(o.steps, 1))
            out.append("")
        decisions: dict[tuple, list[dict]] = {}
        for m in o.merges:
            decisions.setdefault((m["model"], m["reason"]), []).append(m)
        for (model, reason), joined in decisions.items():
            asks_joined = ", ".join(f"\"{m.get('sample') or m['joined']}\"" for m in joined)
            out.append(f"Also joined by {model}: {asks_joined}. Why: {reason}\n")
        out.append(f"frequency {o.parts['frequency']:.2f} · well-defined {o.parts['definition']:.2f}"
                   + (f" · handled by {o.resolver}" if o.resolver else " · no single handler") + "\n")
        for it in o.items[::-1][:6]:                     # newest first
            out.append(f"- {it['created_at'][:10] if it['created_at'] else '?'} {it['title']} <{it['url'] or it['id']}>")
        if len(o.items) > 6:
            out.append(f"- … and {len(o.items) - 6} older")
        out.append("")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", required=True, help="a folder of runs/, or 's3'")
    ap.add_argument("--env", help="the swarm's .env, for --root s3")
    ap.add_argument("--as-of", help="ISO time the window ends at; default is the newest run")
    ap.add_argument("--json", help="also write the ranked list here")
    ap.add_argument("--no-llm", action="store_true", help="plain code only; the same as SWARM_MODEL_BACKEND=off")
    ap.add_argument("--as", dest="as_email", help="show the report as this person may see it")
    args = ap.parse_args(argv)
    env = dict(os.environ)
    if args.env:
        for line in Path(args.env).read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"')
    judge = None
    if not args.no_llm:
        from swarm import model as swarm_model
        os.environ.update({k: v for k, v in env.items() if k.startswith(("SWARM_MODEL", "ANTHROPIC_", "AWS_"))})
        live = swarm_model.from_env()
        judge = Judge(live) if live else None
    ops = opportunities(load(open_store(args.root, env)), parse_time(args.as_of), judge, args.as_email)
    sys.stdout.reconfigure(encoding="utf-8")
    print(markdown(ops, viewer_email=args.as_email))
    print(f"\n_model: {judge.summary() if judge else 'off, plain code only'}_")
    if args.json:
        Path(args.json).write_text(json.dumps([asdict(o) for o in ops], indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
