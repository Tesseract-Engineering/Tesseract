"""
check_ticket_desk.py - how the Ticket Desk's recorded runs did against the seed tickets' answer key.

Each seed ticket in ticket_desk/tickets.json says what a good answer finds: its kind (repeat,
near-repeat, new, and any other a careful reader could defend), the past resolutions a known-fix card may cite, the sources the history lives in (Jira keys, GitHub refs like
github:auth-service#7, Drive titles) and the people to ping. This reads the runs the desk saved in
var/ticket_desk/state.json and scores both modes side by side. No model and no graph: run tickets
in the desk (or POST /api/tickets/<id>/run) first.

    python -m eval.check_ticket_desk            a table per ticket, then totals; exit 1 if any graph
                                                run classifies a ticket wrongly or misses every source
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TICKETS = ROOT / "ticket_desk" / "tickets.json"
STATE = ROOT / "var" / "ticket_desk" / "state.json"


def matches(expected: str, node: dict) -> bool:
    """An answer-key entry against a graph node: a Jira key, github:<repo>#<n>, or an exact title."""
    gh = re.fullmatch(r"github:([\w-]+)#(\d+)", expected)
    if gh:
        return bool(re.search(rf"/{gh[1]}/(pull|issues)/{gh[2]}$", node.get("url") or ""))
    if re.fullmatch(r"[A-Z]+-\d+", expected):
        return node.get("key") == expected
    return (node.get("title") or "").strip() == expected


OUTDATED = re.compile(r"out of date|outdated|stale|no longer|still (describes|points|lists|says)", re.I)


def _claims(obj) -> list[dict]:
    if isinstance(obj, dict):
        return ([obj] if "text" in obj and "sources" in obj else []) + [c for v in obj.values() for c in _claims(v)]
    if isinstance(obj, list):
        return [c for v in obj for c in _claims(v)]
    return []


def seen_nodes(run: dict) -> dict[str, dict]:
    """Every node a tool returned in the run, from its saved events."""
    nodes = {}
    for e in run.get("events", []):
        for n in e.get("nodes") or []:
            nodes.setdefault(n["id"], n)
    return nodes


def score(ticket: dict, run: dict | None) -> dict | None:
    if not run or not run.get("result"):
        return None
    exp, res = ticket["expected"], run["result"]
    answer = res["answer"]
    seen = seen_nodes(run)
    cited = {i: seen.get(i) or res["sources"].get(i) or {"id": i} for i in res["sources"]}
    want = exp.get("sources", [])
    found = [w for w in want if any(matches(w, n) for n in seen.values())]
    used = [w for w in want if any(matches(w, n) for n in cited.values())]
    names = " ".join(o["name"] for o in answer["triage"]["owners"]).lower()
    owners = [o for o in exp.get("owners", []) if o.split()[0].lower() in names]
    # Citing an out-of-date doc is fine when the answer says it is out of date; relying on it is not.
    flagged = [c for c in _claims(answer) if OUTDATED.search(c["text"])]
    stale = [s for s in exp.get("stale", [])
             if any(matches(s, n) for n in cited.values())
             and not any(matches(s, cited.get(i) or {}) for c in flagged for i in c["sources"])]
    kind = answer["triage"]["classification"]
    kf = answer.get("known_fix") or {"found": False, "sources": []}
    kf_nodes = [cited.get(i) or seen.get(i) or {"id": i} for i in kf.get("sources", [])]
    if "known_fix" in exp:          # must show a known fix, traced to one of the past resolutions
        kf_ok = kf["found"] and any(matches(w, n) for w in exp["known_fix"] for n in kf_nodes)
    elif exp["kind"] == "new":      # nothing happened before, so nothing worked before
        kf_ok = not kf["found"]
    else:
        kf_ok = None
    return {"kind": kind, "kind_ok": kind == exp["kind"] or kind in exp.get("accept", []),
            "found": found, "cited": used, "want": want, "owners": owners, "want_owners": exp.get("owners", []),
            "fix": answer["fix"]["warranted"], "fix_ok": answer["fix"]["warranted"] == exp.get("fix", False),
            "stale_cited": stale, "n_cited": len(cited), "kf": kf["found"], "kf_ok": kf_ok}


def main() -> int:
    tickets = json.loads(TICKETS.read_text())["tickets"]
    if not STATE.exists():
        print(f"No saved runs at {STATE}; run tickets in the desk first.")
        return 1
    runs = json.loads(STATE.read_text()).get("runs", {})
    failures, totals = [], {m: {"kind": 0, "cited": 0, "want": 0, "owners": 0, "want_owners": 0, "n": 0}
                            for m in ("graph", "baseline")}
    print(f"{'ticket':<8}{'expected':<12}{'graph':<40}{'baseline':<40}")
    for t in tickets:
        row = [t["id"], t["expected"]["kind"]]
        for mode in ("graph", "baseline"):
            s = score(t, runs.get(t["id"], {}).get(mode))
            if s is None:
                row.append("not run")
                continue
            tot = totals[mode]
            tot["n"] += 1
            tot["kind"] += s["kind_ok"]
            tot["cited"] += len(s["cited"])
            tot["want"] += len(s["want"])
            tot["owners"] += len(s["owners"])
            tot["want_owners"] += len(s["want_owners"])
            cell = (f"{'ok ' if s['kind_ok'] else 'NO '}{s['kind']:<12}"
                    f"src {len(s['cited'])}/{len(s['want'])} ppl {len(s['owners'])}/{len(s['want_owners'])}"
                    + (" fix✓" if s["kf_ok"] else " FIX✗" if s["kf_ok"] is False else "")
                    + (" RELIES ON STALE" if s["stale_cited"] else ""))
            row.append(cell)
            if mode == "graph":
                if not s["kind_ok"]:
                    failures.append(f"{t['id']}: classified {s['kind']}, expected {t['expected']['kind']}")
                if s["want"] and not s["found"]:
                    failures.append(f"{t['id']}: found none of {s['want']}")
                if s["kf_ok"] is False:
                    failures.append(f"{t['id']}: known fix {'missing or untraced' if 'known_fix' in t['expected'] else 'shown for a new issue'}")
                if s["stale_cited"]:
                    failures.append(f"{t['id']}: cites the out-of-date {s['stale_cited']}")
        print(f"{row[0]:<8}{row[1]:<12}" + "".join(f"{c:<40}" for c in row[2:]))
    print()
    for mode, tot in totals.items():
        if tot["n"]:
            print(f"{mode:<9} classified right {tot['kind']}/{tot['n']}, expected sources cited "
                  f"{tot['cited']}/{tot['want']}, owners named {tot['owners']}/{tot['want_owners']}")
    for f in failures:
        print("FAIL", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
