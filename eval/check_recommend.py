"""
check_recommend.py - the sandbox's answer key for the recommendation agent, checked against a run.

The key is sandbox/storyline.md, "Automation opportunities (round 3)", in swarm-beta (branch
ishan-sandbox-env). The truth comes from the seed files themselves, not from the agent: every round-3
Jira ticket has a pattern slug (r3-p1-..., r3-d2-...) and a summary, every Slack ask sits under a known
channel section of slack_round3.txt, and {id:...} / {slack:...} mark the asks that were ticketed. Those
are matched to the swarm's records, which gives each pattern's distinct asks as record ids. Then the
agent runs on the same records and each row of the key is checked:

  found, as one pattern       P1-P5 each appear, not split in two
  counts                      distinct asks: P1 24, P2 14, P3 8, P4 6, P5 4; nothing from #leadership
  no strays                   a pattern holds no other pattern's asks (asks from earlier rounds are noted)
  ranking                     P1, P2, P3, P4, P5
  approvers                   from ownership; Karthik never approves Grafana (the wrong-approver trap)
  steps                       from the runbook each pattern links; P5's from its tickets
  decoys                      D1-D4 absent, or below P5 with a "ranks low" reason
  as a viewer                 someone outside #acme-escalations sees P2 as 9

    python -m eval.check_recommend --sandbox path/to/swarm-beta/sandbox --root s3 --env .env
    SWARM_MODEL_BACKEND=off python -m eval.check_recommend ...        plain code only

Exit 1 if any check fails. --report writes the agent's markdown report next to the result.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path[:0] = [str(ROOT / "recommend"), str(ROOT / "graph"), str(ROOT)]

import yaml  # noqa: E402
from judge import Judge  # noqa: E402
from swarm_records import load, open_store  # noqa: E402

from recommend import markdown, opportunities  # noqa: E402

PATTERNS = ("P1", "P2", "P3", "P4", "P5")
DECOYS = ("D1", "D2", "D3", "D4")
# The key's numbers. Derived from the seed files too; a mismatch means the seeds and the key disagree.
ASKS = {"P1": 24, "P2": 14, "P3": 8, "P4": 6, "P5": 4}
P2_OUTSIDE = 9
OUTSIDER = "ishan.tesseract@gmail.com"          # not a member of #acme-escalations (hamilton, karthik)

# slack_round3.txt's sections in order, and the pattern each one seeds.
SECTIONS = [("help-eng", "P1"), ("infra", "P1"), ("help-eng", "P1"), ("infra", "P1"), ("leadership", "OUT"),
            ("acme-escalations", "P2"), ("customer-success", "P2"), ("incidents", "D1"), ("help-eng", "D3"),
            ("eng", "D4"), ("platform-standup", "D4"), ("all-tesseract-sandbox", "D4")]

# Who approves, by what an ask is about (storyline.md, Patterns table; the handbook's Service owners).
P1_OWNERS = [("replica", {"Dhruv Pendharkar", "Hamilton Park"}), ("pg-prod-ro", {"Dhruv Pendharkar", "Hamilton Park"}),
             ("grafana", {"Dhruv Pendharkar"}), ("infra", {"Dhruv Pendharkar"}), ("auth-service", {"Ishan Sinha"}),
             ("web-app", {"Karthik Valluri"})]
APPROVER = {"P2": {"Hamilton Park"}, "P3": {"Dhruv Pendharkar"}, "P4": {"Karthik Valluri"}, "P5": {"Hamilton Park"}}
RUNBOOK = {"P1": "access-policy", "P2": "customer-data-export", "P3": "rotate-staging-keys",
           "P4": "retrain-onboarding-guide"}
MIN_ROUTED = 0.8                                  # share of P1 asks the agent must route to an owner

_TAG = re.compile(r"\{(?:id|slack):([\w-]+)\}")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


# -- the truth, from the seed files ------------------------------------------------------------- #

def seeds(sandbox: Path) -> dict:
    slack, heads = [], []
    for line in (sandbox / "slack_round3.txt").read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            heads.append(line[3:].split("|")[0].strip())
            continue
        if not line.startswith("@") or not heads:
            continue
        if len(heads) > len(SECTIONS):
            raise SystemExit(f"slack_round3.txt has a section past the {len(SECTIONS)} this checker knows")
        channel, pattern = SECTIONS[len(heads) - 1]
        if heads[-1] != channel:
            raise SystemExit(f"slack_round3.txt section {len(heads)} is #{heads[-1]}, expected #{channel}")
        body = line.split(":", 1)[1].strip()
        tags = _TAG.findall(body)
        slack.append({"pattern": pattern, "channel": channel, "text": _TAG.sub("", body).strip(), "raw": body,
                      "pair": tags[0] if tags and "{id:" in body else None})
    jira = []
    for t in yaml.safe_load((sandbox / "jira_round3.yaml").read_text(encoding="utf-8"))["tickets"]:
        pattern = t["id"].split("-")[1].upper()
        tags = _TAG.findall(t.get("description") or "")
        jira.append({"pattern": pattern, "slug": t["id"], "summary": t["summary"], "pair": tags[0] if tags else None})
    return {"slack": slack, "jira": jira}


def truth(state, s: dict) -> dict:
    """pattern -> list of asks (sets of record ids); plus what did not match."""
    threads = [i for i in state.items.values() if i.get("source") == "slack" and i.get("kind") == "thread"]
    by_summary: dict[str, list[dict]] = {}
    for i in state.items.values():
        if i.get("source") == "jira" and i.get("kind") == "issue":
            by_summary.setdefault((i.get("title") or "").strip(), []).append(i)
    asks: dict[str, list[set[str]]] = {p: [] for p in (*PATTERNS, *DECOYS, "OUT")}
    paired: dict[str, set[str]] = {}
    missing, seen_slack = [], Counter()
    for m in s["slack"]:
        key = _norm(re.split(r"https?://|\{", m["raw"])[0])[:60]
        hits = [t for t in threads if key and key in _norm(t.get("text") or "")]
        if m["pattern"] == "OUT":
            if hits:
                missing.append(f"#leadership ask is in the records: {m['text'][:50]}")
            continue
        if not hits:
            missing.append(f"Slack #{m['channel']}: {m['text'][:50]}")
            continue
        seen_slack[hits[0]["id"]] += 1
        ask = {hits[0]["id"]}
        if m["pair"]:
            paired[m["pair"]] = ask
        asks[m["pattern"]].append(ask)
    merged = [tid for tid, n in seen_slack.items() if n > 1]
    for t in s["jira"]:
        hits = by_summary.get(t["summary"].strip(), [])
        if not hits:
            missing.append(f"Jira {t['slug']}: {t['summary']}")
            continue
        if t["pair"] and t["pair"] in paired:
            paired[t["pair"]].add(hits[0]["id"])
        else:
            asks[t["pattern"]].append({hits[0]["id"]})
    return {"asks": asks, "missing": missing, "merged": merged}


# -- matching the agent's output to the truth ------------------------------------------------------- #

def assign(ops, t: dict) -> dict[str, list[int]]:
    """pattern -> indexes of the opportunities that are mostly made of its asks."""
    owner = {i: p for p, asks in t["asks"].items() for ask in asks for i in ask}
    out: dict[str, list[int]] = {}
    for n, o in enumerate(ops):
        votes = Counter(owner.get(it["id"]) for it in o.items)
        votes.pop(None, None)
        if votes:
            p, k = votes.most_common(1)[0]
            if k * 2 > len(o.items):
                out.setdefault(p, []).append(n)
    return out


def checks(state, t: dict, ops, ops_outside) -> list[tuple[str, str, bool, str]]:
    res = []

    def add(group, name, ok, detail=""):
        res.append((group, name, None if ok is None else bool(ok), detail))

    asks = t["asks"]
    add("seeds", "every round-3 ask is in the records, and none from #leadership", not t["missing"],
        "; ".join(t["missing"][:6]))
    add("seeds", "no two seeded asks merged into one Slack item", not t["merged"], ", ".join(t["merged"][:4]))
    add("seeds", "the seeds add up to the key's counts", all(len(asks[p]) == ASKS[p] for p in PATTERNS),
        str({p: len(asks[p]) for p in PATTERNS}))

    where = assign(ops, t)
    owner = {i: p for p, a in asks.items() for ask in a for i in ask}
    for p in PATTERNS:
        idx = where.get(p, [])
        add("found", f"{p} is found, as one pattern", len(idx) == 1,
            f"{len(idx)} patterns: " + ", ".join(f"{ops[k].label!r} ({ops[k].frequency})" for k in idx))
    seeded = {i for a in asks.values() for ask in a for i in ask}
    for p in PATTERNS:
        idx = where.get(p, [])
        o = ops[idx[0]] if idx else None
        if not o:
            add("counts", f"{p}: {ASKS[p]} distinct asks", False, "agent says nothing")
            continue
        # Asks made only of items from earlier sandbox rounds: real work of the same kind, not in the key.
        by_ask: dict[int, list[dict]] = {}
        for it in o.items:
            by_ask.setdefault(it["ask"], []).append(it)
        older = [a for a in by_ask.values() if not any(it["id"] in seeded for it in a)]
        n = o.frequency - len(older)
        add("counts", f"{p}: {ASKS[p]} distinct asks", n == ASKS[p],
            f"agent says {n} round-3 asks: {o.evidence}")
        if older:
            add("counts", f"{p}: also holds {len(older)} asks from earlier rounds (not in the key)", None,
                "; ".join((a[0]["title"] or "")[:50] for a in older))
        stray = [it for it in o.items if it["id"] in seeded and owner.get(it["id"]) != p]
        missed = {i for ask in asks[p] for i in ask} - {it["id"] for it in o.items}
        add("counts", f"{p}: holds no other pattern's asks, and all of its own", not stray and not missed,
            (f"{len(stray)} from other patterns: " + "; ".join((it["title"] or "")[:40] for it in stray[:4]) if stray else "")
            + (f" {len(missed)} missed: " + "; ".join((state.items[i].get("title") or "")[:40]
                                                   for i in list(missed)[:4]) if missed else ""))

    pos = [where[p][0] if where.get(p) else None for p in PATTERNS]
    found = [x for x in pos if x is not None]
    add("ranking", "P1, P2, P3, P4, P5 in that order", None not in pos and found == sorted(found),
        " > ".join(f"{p}@{x + 1}" if x is not None else f"{p}@-" for p, x in zip(PATTERNS, pos, strict=True)))

    if where.get("P1"):
        o = ops[where["P1"][0]]
        wrong = []
        for a in o.approvers:
            for system in a["systems"]:
                exp = next((people for k, people in P1_OWNERS if k in system.lower()), None)
                if exp is not None and set(a["people"]) != exp:
                    wrong.append(f"{system}: {' and '.join(a['people'])}, expected {' and '.join(sorted(exp))}")
        routed = 1 - o.unrouted / max(o.frequency, 1)
        add("approvers", "P1: each system's owner approves (the replica needs both)", not wrong, "; ".join(wrong))
        add("approvers", "P1: Karthik never approves Grafana (the wrong-approver trap)",
            not any("grafana" in s.lower() and "Karthik Valluri" in a["people"] for a in o.approvers for s in a["systems"]))
        add("approvers", f"P1: at least {MIN_ROUTED:.0%} of asks routed to an owner", routed >= MIN_ROUTED,
            f"{o.frequency - o.unrouted} of {o.frequency}")
    for p, exp in APPROVER.items():
        o = ops[where[p][0]] if where.get(p) else None
        groups = o.approvers if o else []
        main = set(groups[0]["people"]) if groups else set()      # approvers are listed by how many asks they cover
        add("approvers", f"{p}: {' and '.join(sorted(exp))} approves", main == exp,
            "agent says " + ("; ".join(f"{' and '.join(a['people'])} ({sum(a['systems'].values())})" for a in groups)
                             or "nobody") + (f", {o.unrouted} unrouted" if o and o.unrouted else ""))

    for p in PATTERNS:
        o = ops[where[p][0]] if where.get(p) else None
        if p in RUNBOOK:
            title = (o.runbook or {}).get("title", "") if o else ""
            add("steps", f"{p}: steps from its runbook", o and RUNBOOK[p] in _norm(title).replace(" ", "-") and o.steps,
                f"runbook {title!r}, {len(o.steps) if o else 0} steps")
        else:
            add("steps", f"{p}: steps listed (from its tickets)", o and len(o.steps) >= 2,
                f"{len(o.steps) if o else 0} steps from {o.steps_from if o else '-'}")

    p5 = where.get("P5", [None])[0]
    for d in DECOYS:
        idx = where.get(d, [])
        low = all((p5 is not None and k > p5) and ops[k].why and ops[k].why[0].startswith("ranks low") for k in idx)
        add("decoys", f"{d} absent, or below P5 with a reason", not idx or low,
            "; ".join(f"{ops[k].label!r}@{k + 1}: {ops[k].why[:1]}" for k in idx))

    w2 = assign(ops_outside, t)
    o2 = ops_outside[w2["P2"][0]] if w2.get("P2") else None
    add("viewer", f"P2 as {OUTSIDER}: {P2_OUTSIDE} asks", o2 and o2.frequency == P2_OUTSIDE,
        f"agent says {o2.frequency if o2 else 'nothing'}")
    private = {c["id"] for c in state.items.values() if c.get("kind") == "channel" and c.get("title") == "#acme-escalations"}
    add("viewer", "nothing from #acme-escalations in that view",
        not any(state.items[it["id"]].get("container") in private for o in ops_outside for it in o.items))

    others = [k for k in range(len(ops)) if not any(k in v for v in where.values())]
    add("noise", "nothing that isn't a seeded pattern ranks above P5", p5 is None or not any(k < p5 for k in others),
        "; ".join(f"{ops[k].label!r}@{k + 1} ({ops[k].frequency})" for k in others if p5 is None or k < p5))
    return res


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--sandbox", type=Path, required=True, help="swarm-beta's sandbox/ folder (the seed files)")
    ap.add_argument("--root", required=True, help="a folder of runs/, a .zip, or 's3'")
    ap.add_argument("--env", type=Path, help="the swarm's .env, for --root s3")
    ap.add_argument("--report", type=Path, help="also write the agent's report here (markdown)")
    args = ap.parse_args(argv)
    env = dict(os.environ)
    if args.env:
        for line in args.env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env.setdefault(k.strip(), v.strip().strip('"'))
    os.environ.update({k: v for k, v in env.items() if k.startswith(("SWARM_MODEL", "ANTHROPIC_", "AWS_"))})
    from swarm import model as swarm_model

    state = load(open_store(args.root, env))
    live = swarm_model.from_env()
    judge = Judge(live) if live else None
    t = truth(state, seeds(args.sandbox))
    ops = opportunities(state, judge=judge)
    ops_outside = opportunities(state, judge=judge, as_email=OUTSIDER)
    if args.report:
        args.report.write_text(markdown(ops, limit=len(ops)), encoding="utf-8")

    sys.stdout.reconfigure(encoding="utf-8")
    print(f"items {len(state.items)}, as of {state.as_of}; model: {judge.summary() if judge else 'off'}")
    failed, group = 0, None
    results = checks(state, t, ops, ops_outside)
    for g, name, ok, detail in results:
        if g != group:
            print(f"\n{g}")
            group = g
        if ok is None:                                   # a note: printed, never counted
            print(f"  NOTE  {name}\n        {detail}")
            continue
        failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail}" if detail and not ok else ""))
    counted = [r for r in results if r[2] is not None]
    print(f"\n{len(counted) - failed}/{len(counted)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
