"""The recommendation agent's model steps, against a scripted model: what each step may change, what
the code refuses, and what happens when the model is down. No model is called.

    python recommend/test_judge.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "graph"), str(HERE.parent)]
sys.stdout.reconfigure(encoding="utf-8")

from types import SimpleNamespace  # noqa: E402

import prompts  # noqa: E402
from judge import Judge  # noqa: E402

from recommend import markdown, opportunities  # noqa: E402
from swarm.model import Completion, ModelUnavailable  # noqa: E402
from swarm.records import Tokens  # noqa: E402

ok = True


def check(name, condition, detail=""):
    global ok
    ok = ok and condition
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{('  ' + str(detail)) if detail else ''}")


AS_OF = datetime(2026, 10, 27, tzinfo=timezone.utc)
items = {}


def add(title, days_ago, text="", kind="issue"):
    n = len(items) + 1
    items[f"i{n}"] = {"id": f"i{n}", "kind": kind, "source": "jira" if kind == "issue" else "slack", "title": title,
                      "text": text, "container": "jira:IT", "url": f"https://x/{n}", "meta": {},
                      "created_at": (AS_OF - timedelta(days=days_ago)).isoformat(), "acl": {"visibility": "internal"},
                      "people": [{"id": f"person:c:u{n}", "roles": ["reporter"]}, {"id": "person:c:raj", "roles": ["assignee"]}]}
    return f"i{n}"


# Three wordings of one ask that title overlap cannot join, one body-only ask, and noise.
a = [add(f"Access to prod database for {w}", 3 + d, text="Added to db-readers group, closed.")
     for d, w in enumerate(["Dana", "Lee"])]
b = [add(t, 10 + d) for d, t in enumerate(["Need prod Postgres creds", "Can't log into production DB"])]
c = add("quick q", 20, kind="thread", text="hey, could someone give me read access on the prod database?")
noise = add("Prod database migration plan", 6, text="We will move to the new cluster in Q1.")
other = [add(f"Reset VPN token ({k})", 4 + k) for k in range(3)]

PHRASE = "grant read access to the production database"


class Scripted:
    """Answers by step, from the system prompt's tag; records what it was asked."""
    name = "scripted"

    def __init__(self, review=None, merge=None, fail_on=None):
        self.asked, self._review, self._merge, self.fail_on = [], review, merge, fail_on

    def extract(self, system, text, schema):
        step = system.split("]")[0].split()[-1]
        self.asked.append(step)
        if step == self.fail_on:
            raise ModelUnavailable("RateLimitError")
        if step == "read":
            t = text.lower()
            if "migration plan" in t:
                data = {"is_request": False, "request": ""}
            elif "vpn" in t:
                data = {"is_request": True, "request": "reset a VPN token"}
            else:
                data = {"is_request": True, "request": PHRASE}
        elif step == "merge":
            data = self._merge or {"merges": []}
        else:
            data = self._review(text) if self._review else None
        return Completion(data=data, model="claude-opus-5-5", stop_reason="end_turn" if data else "refusal",
                          tokens=Tokens(input=100, output=10))


state = SimpleNamespace(items=items, as_of=AS_OF.isoformat())

print("plain code alone")
plain = opportunities(state, AS_OF)
check("misses the differently worded asks", all(o.frequency < 5 for o in plain), [(o.label, o.frequency) for o in plain])

print("READ: one phrase per ask, from the body when the title says nothing")


def good_review(text):
    ids = [line.split()[1] for line in text.splitlines() if line.startswith("Item ")]
    if "vpn" in text.lower():
        return {"name": "VPN token reset", "same_steps": 0.5, "steps": [], "evidence": []}
    return {"name": "Production database read access", "same_steps": 0.9,
            "steps": ["Confirm the manager approved", "Add to db-readers"], "evidence": ids[:2] + ["made-up-id"]}


judge = Judge(Scripted(review=good_review))
ops = opportunities(state, AS_OF, judge)
top = ops[0]
check("five asks, three wordings and a body-only thread, are one pattern", top.frequency == 5,
      [(o.label, o.frequency) for o in ops])
check("the 'quick q' thread is in it", c in [i["id"] for i in top.items])
check("the migration plan, not a request, is dropped", all(noise not in [i["id"] for i in o.items] for o in ops))
check("VPN resets stay their own pattern", any("vpn" in o.label.lower() for o in ops))

print("REVIEW: name and steps, checked")
check("named by the model", top.label == "Production database read access", top.label)
check("steps kept with their evidence", top.steps and top.judged_by == "claude-opus-5-5", top.steps)
check("an invented item id is dropped from evidence", "made-up-id" not in top.steps_evidence
      and judge.counts["review: cited ids not in the group, dropped"] >= 1, top.steps_evidence)
check("the model moves definition, not counts", top.frequency == 5 and "model_same_steps" in top.signals)
check("the report shows steps and who read them", "Steps (read by claude-opus-5-5 from" in markdown(ops))


def no_evidence(_text):
    return {"name": "x", "same_steps": 0.95, "steps": ["do it"], "evidence": ["nope"]}


j2 = Judge(Scripted(review=no_evidence))
o2 = opportunities(state, AS_OF, j2)[0]
check("steps citing nothing real are dropped, same_steps held to 0.3",
      o2.steps == [] and o2.signals["model_same_steps"] <= 0.3, (o2.steps, o2.signals))

print("MERGE: only groups that exist")
# Without READ, plain grouping leaves the asks apart; the merge step must join them.
reqs_only = SimpleNamespace(items={k: v for k, v in items.items() if k in a + b}, as_of=AS_OF.isoformat())


class NoRead(Scripted):
    def extract(self, system, text, schema):
        if system.startswith(f"[{prompts.VERSION} read]"):
            return Completion(data=None, model="m", stop_reason="refusal")
        return super().extract(system, text, schema)


j3 = Judge(NoRead(merge={"merges": [{"groups": [1, 2, 3], "reason": "all ask for prod DB read access"},
                                    {"groups": [1, 99], "reason": "bad"}, {"groups": [4], "reason": "single"}]}))
o3 = opportunities(reqs_only, AS_OF, j3)
check("three title groups joined into one pattern of four", len(o3) == 1 and o3[0].frequency == 4,
      [(o.label, o.frequency) for o in o3])
check("each join keeps the model's reason and the items it joined", len(o3[0].merges) == 2
      and all(m["reason"] and m["items"] for m in o3[0].merges), o3[0].merges)
check("unknown, reused and single-group merges refused", j3.counts["merge: refused (unknown, repeated or single group)"] == 2,
      dict(j3.counts))

print("model down")
j4 = Judge(Scripted(review=good_review, fail_on="read"))
o4 = opportunities(state, AS_OF, j4)
check("falls back to plain code and says so", [(o.label, o.frequency) for o in o4] == [(o.label, o.frequency) for o in plain]
      and j4.stopped and "plain code" in j4.summary(), j4.summary())
check("stops asking once down", j4.model.asked.count("read") == 1, j4.model.asked)

print("\nALL PASS" if ok else "\nFAILURES")
sys.exit(0 if ok else 1)
