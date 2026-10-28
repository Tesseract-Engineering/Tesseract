"""The recommendation agent on hand-made items: repeated requests rank above one-offs. No database.

    python recommend/test_recommend.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "graph")]
sys.stdout.reconfigure(encoding="utf-8")

from swarm_records import LocalStore, load  # noqa: E402

from recommend import markdown, opportunities  # noqa: E402

ok = True


def check(name, condition, detail=""):
    global ok
    ok = ok and condition
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{('  ' + str(detail)) if detail else ''}")


AS_OF = datetime(2026, 10, 27, tzinfo=timezone.utc)
items, n = {}, 0


def add(title, days_ago, kind="issue", assignee="person:c:it", text="", container="jira:IT"):
    global n
    n += 1
    created = (AS_OF - timedelta(days=days_ago)).isoformat()
    meta = {}
    items[f"i{n}"] = {"id": f"i{n}", "kind": kind, "source": "jira", "title": title, "text": text, "container": container,
                      "created_at": created, "url": f"https://x/{n}", "meta": meta, "acl": {"visibility": "internal"},
                      "people": [{"id": f"person:c:u{n}", "roles": ["reporter"]}, {"id": assignee, "roles": ["assignee"]}]}


print("repeated vs one-off")
for i, who in enumerate(["Dana", "Lee", "Priya", "Sam", "Ana", "Raj", "Kim"]):
    add(f"Access to prod database for {who}", 5 + i * 9, text="1. confirm manager\n2. add to group\n3. close")
for i in range(4):
    add(f"Reset VPN token (IT-{100 + i})", 3 + i * 10, assignee=f"person:c:a{i}", container=f"jira:C{i}")
add("Migrate billing to new ledger", 4)
add("Access to prod database for Old", 200)                      # outside the window
add("Weekly sync notes", 2, kind="commit")                       # not a request

from types import SimpleNamespace  # noqa: E402

state = SimpleNamespace(items=items, as_of="2026-10-27T00:00:00Z")
ops = opportunities(state, AS_OF)
check("two patterns found", len(ops) == 2, [o.label for o in ops])
top = ops[0]
check("access requests rank first", "access" in top.label and "database" in top.label, top.label)
check("old and non-request items excluded", top.frequency == 7, top.frequency)
check("one handler detected", top.resolver == "person:c:it", top.resolver)
check("steps in the text raise definition", top.definition > ops[1].definition, (top.definition, ops[1].definition))
check("evidence quotes the count", "asked 7 times" in top.evidence, top.evidence)
check("every evidence item has a link", all(i["url"] for i in top.items))
check("the one-off ticket is not a pattern", all("billing" not in o.label for o in ops))

print("samples/run-0")
real = load(LocalStore(HERE.parent / "samples" / "run-0"))
check("tiny sample yields no patterns, and no crash", opportunities(real) == [] and "No repeated" in markdown([]))
check("report renders", "asked 7 times" in markdown(ops))

print("\nALL PASS" if ok else "\nFAILURES")
sys.exit(0 if ok else 1)
