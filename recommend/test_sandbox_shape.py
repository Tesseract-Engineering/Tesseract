"""The recommendation agent on a hand-made company shaped like the sandbox brief (sandbox/storyline.md,
"Automation opportunities"): owners from the handbook and CODEOWNERS, an ask and its ticket counted
once, a private channel, runbooks linked by GitHub and Drive URL, bot posts, a linked one-off project
and a decoy. Plain code only; no model, no database.

    python recommend/test_sandbox_shape.py
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent / "graph"), str(HERE.parent)]
sys.stdout.reconfigure(encoding="utf-8")

from recommend import markdown, opportunities  # noqa: E402

ok = True


def check(name, condition, detail=""):
    global ok
    ok = ok and condition
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{('  ' + str(detail)) if detail else ''}")


AS_OF = datetime(2026, 10, 27, tzinfo=timezone.utc)
INTERNAL = {"visibility": "internal", "principals": []}
PRIVATE = {"visibility": "restricted", "principals": ["group:slack:T1:channel:CESC:members"]}
items: dict[str, dict] = {}

DHRUV = {"id": "person:slack:UDH", "source": "slack", "handle": "UDH", "name": "Dhruv Pendharkar",
         "email": "dhruv@tess.dev"}
DHRUV_JIRA = {"id": "person:jira:dp", "source": "jira", "handle": "dp", "name": "D. Pendharkar", "email": "dhruv@tess.dev"}
HAMILTON = {"id": "person:slack:UHA", "source": "slack", "handle": "UHA", "name": "Hamilton Park",
            "email": "hamilton@tess.dev"}
ISHAN = {"id": "person:github:ishan-sinha-ucb", "source": "github", "handle": "ishan-sinha-ucb", "name": "Ishan Sinha",
         "email": "ishan@tess.dev"}
KARTHIK = {"id": "person:slack:UKA", "source": "slack", "handle": "UKA", "name": "Karthik Valluri",
           "email": "karthik@tess.dev"}
OUTSIDER = {"id": "person:slack:UOUT", "source": "slack", "handle": "UOUT", "name": "Sam Outside", "email": "sam@tess.dev"}


def person(n, source="slack"):
    return {"id": f"person:{source}:u{n}", "source": source, "handle": f"u{n}", "name": f"Asker {n}",
            "email": f"asker{n}@tess.dev"}


def add(iid, kind, source, title, text="", people=(), refs=(), acl=INTERNAL, container=None, days=5, meta=None, url=None):
    items[iid] = {"id": iid, "kind": kind, "source": source, "title": title, "text": text,
                  "people": [dict(p, roles=list(roles)) for p, roles in people], "refs": list(refs), "acl": acl,
                  "container": container, "url": url or f"https://x/{iid}", "meta": meta or {},
                  "created_at": (AS_OF - timedelta(days=days)).isoformat()}
    return iid


# -- what the company wrote down --------------------------------------------------------------- #
add("github:repo:1", "repo", "github", "tess/handbook")
add("github:repo:2", "repo", "github", "tess/auth-service")
add("github:repo:1:file:README.md", "document", "github", "README.md",
    "# Handbook\n\n## Service owners\n\n| Service | Team | Owner |\n|---|---|---|\n"
    "| Grafana (staging and prod dashboards) | Infra | Dhruv Pendharkar |\n"
    "| prod read replica (`pg-prod-ro`) | Infra | Dhruv Pendharkar and Hamilton Park (both approve) |\n"
    "| web-app | Product | Karthik Valluri |\n",
    people=[(DHRUV, ["author"])], meta={"path": "README.md"},
    url="https://github.com/tess/handbook/blob/main/README.md")
add("github:repo:2:file:.github/CODEOWNERS", "document", "github", ".github/CODEOWNERS",
    "# Platform owns auth-service\n*       @ishan-sinha-ucb\n", people=[(ISHAN, ["author"])],
    meta={"path": ".github/CODEOWNERS"}, url="https://github.com/tess/auth-service/blob/main/.github/CODEOWNERS")
add("github:repo:1:file:access-policy.md", "document", "github", "access-policy.md",
    "# Access Policy\n\nEvery access request:\n\n1. Find the system's owner in Service owners\n2. Owner approves\n"
    "3. Add the requester to the system's group\n4. Reply with what was granted\n",
    meta={"path": "access-policy.md"}, url="https://github.com/tess/handbook/blob/main/access-policy.md")
add("gdrive:file:RUNBOOKKEYS1", "document", "gdrive", "Runbook - Rotate Staging Keys", people=[(DHRUV, ["owner"])],
    text="Steps\n- [ ] Generate new keys\n- [ ] Upload to the secret store\n- [ ] Roll staging\n- [ ] Revoke old keys\n",
    url="https://docs.google.com/document/d/RUNBOOKKEYS1/edit")
POLICY = "https://github.com/tess/handbook/blob/main/access-policy.md"

# -- P1 access requests: 6 Slack asks, 3 of them ticketed with the permalink, 1 private, 1 wrong approver #
# The tickets carry the permalink only as text and meta.slack_links, as the swarm writes them: no refs.
# One links the thread's first message, one a reply (thread_ts), one a message inside a burst.
systems = ["staging Grafana", "the prod read replica", "auth-service", "staging Grafana", "web-app", "staging Grafana"]
TS = [f"17926594{n:02d}.000100" for n in range(len(systems))]
for n, system in enumerate(systems):
    burst = n == 2
    add(f"slack:T1:thread:CHELP:{TS[n]}", "thread", "slack", f"hey can I get read access to {system}?",
        f"hey can I get read access to {system}?\nDhruv: done, per {POLICY}",
        people=[(person(n), ["author"]), (DHRUV, ["participant"])], container="slack:T1:channel:CHELP", days=3 + n,
        meta={"grouping": "burst", "message_count": 3} if burst else {"grouping": "thread"})
    if burst:
        items[f"slack:T1:thread:CHELP:{TS[n]}"]["version"] = f"17926594{n:02d}.000900"
    if n < 3:
        p_ts = TS[n].replace(".", "")
        link = [f"https://tess.slack.com/archives/CHELP/p{p_ts}",
                f"https://tess.slack.com/archives/CHELP/p17926594{n:02d}000500?thread_ts={TS[n]}&cid=CHELP",
                f"https://tess.slack.com/archives/CHELP/p17926594{n:02d}000500"][n]
        add(f"jira:OPS-{n}", "issue", "jira", f"Access request: {system} for asker {n}",
            f"Asked in Slack: {link}\nPolicy: {POLICY}", people=[(person(n, "jira"), ["reporter"]), (DHRUV_JIRA, ["assignee"])],
            container="jira:OPS", days=2 + n, meta={"slack_links": [link]})
        if n == 0:   # Jira writes local time: 7 hours "earlier" as a string, an hour later in fact
            when = datetime.fromisoformat(items[f"slack:T1:thread:CHELP:{TS[n]}"]["created_at"]) + timedelta(hours=1)
            items[f"jira:OPS-{n}"]["created_at"] = when.astimezone(timezone(timedelta(hours=-7))).isoformat()
# the wrong approver: Karthik clicked approve on a Grafana ask; the owner is still Dhruv
items[f"slack:T1:thread:CHELP:{TS[3]}"]["text"] = "can I get read access to staging Grafana?\nKarthik: approved\nDhruv: redone, I own it"
add("slack:T1:thread:CESC:0", "thread", "slack", "hey can I get read access to the prod read replica?",
    "hey can I get read access to the prod read replica? it's for the Acme escalation", people=[(person(9), ["author"])],
    acl=PRIVATE, container="slack:T1:channel:CESC", days=4)

# -- P3 staging key rotation: 4 tickets, each linking the Drive runbook --------------------------- #
for n in range(4):
    add(f"jira:OPS-1{n}", "issue", "jira", f"Rotate staging signing keys (sprint {40 + n})",
        "Per https://docs.google.com/document/d/RUNBOOKKEYS1/edit", people=[(DHRUV_JIRA, ["reporter", "assignee"])],
        container="jira:OPS", days=2 + 14 * n % 60)

# -- P2 customer data exports: 3 tickets linking a runbook that names the approver ---------------- #
add("github:repo:1:file:customer-data-exports.md", "document", "github", "customer-data-exports.md",
    "# Customer data exports\n\n1. Hamilton approves the request on the ticket\n2. Run the export script\n"
    "3. Upload with the Reports API\n", people=[(ISHAN, ["author"])], meta={"path": "customer-data-exports.md"},
    url="https://github.com/tess/handbook/blob/main/customer-data-exports.md")
for n, tenant in enumerate(["Acme", "Northwind", "Globex"]):
    add(f"jira:CS-{n}", "issue", "jira", f"Data export: {tenant} Q3 usage",
        "Per https://github.com/tess/handbook/blob/main/customer-data-exports.md", people=[(HAMILTON, ["reporter"])],
        container="jira:CS", days=3 + n)

# -- decoy: flaky tests, every root cause different, no runbook ------------------------------------ #
for n, cause in enumerate(["a timing race", "fixture data", "a slow runner", "a DNS blip"]):
    add(f"slack:T1:thread:CHELP:f{n}", "thread", "slack", f"test_login is flaky on CI again ({n})",
        f"looked into it: {cause}", people=[(person(20 + n), ["author"]), (DHRUV, ["participant"])],
        container="slack:T1:channel:CHELP", days=2 + n)

# -- noise: a PagerDuty burst, one wiki project filed three times and marked Duplicate, a status thread #
add("slack:T1:thread:CALERT:0", "thread", "slack", "TRIGGERED disk usage > 85%", "PagerDuty: TRIGGERED disk usage > 85%",
    people=[(DHRUV, ["participant"])], meta={"bots": ["PagerDuty"]}, container="slack:T1:channel:CALERT", days=1)
for n in range(3):
    add(f"jira:WIKI-{n}", "issue", "jira", "Move the wiki to Notion", "One migration, see WIKI-0",
        people=[(person(30 + n, "jira"), ["reporter"])],
        refs=[{"type": "duplicates", "target": "jira:WIKI-0", "via": "field"}] if n else [], container="jira:WIKI",
        days=3 + n)

add("slack:T1:thread:CHELP:status", "thread", "slack", "status on the access tickets?",
    "where are we on these?", people=[(HAMILTON, ["author"])], container="slack:T1:channel:CHELP", days=1,
    refs=[{"type": "references", "target": f"jira:OPS-{n}", "via": "key"} for n in range(3)])

groups = {"group:slack:T1:channel:CESC:members": {"id": "group:slack:T1:channel:CESC:members",
                                                  "members": ["user:hamilton@tess.dev", "user:karthik@tess.dev"]}}
for p in (HAMILTON, KARTHIK, OUTSIDER):     # people the records know, so internal items are theirs
    add(f"slack:T1:thread:CSOC:{p['handle']}", "thread", "slack", f"{p['handle']} says hi", "", people=[(p, ["author"])], days=1)
state = SimpleNamespace(items=items, groups=groups, person_of={}, clusters={}, extractions={}, as_of=AS_OF.isoformat())

ops = opportunities(state, AS_OF, as_email="hamilton@tess.dev")
by = {o.label: o for o in ops}
print("ranked:", [(o.label, o.frequency, o.score) for o in ops])
access = next((o for o in ops if "access" in o.label), None)
keys = next((o for o in ops if "rotate" in o.label), None)
flaky = next((o for o in ops if "flaky" in o.label), None)

print("an ask and its ticket are one ask")
check("P1 counts 7 asks (6 public + 1 private), not 10 items", access and access.frequency == 7,
      access and access.evidence)
check("the evidence says why there are more items than asks", access and "3 filed from or duplicating another ask" in access.evidence)
check("requesters count once across Slack and Jira", access and access.requesters == 7, access and access.requesters)

print("approver from ownership")
names = {tuple(a["people"]): a for a in access.approvers} if access else {}
check("Grafana asks route to Dhruv, from the handbook table", ("Dhruv Pendharkar",) in names
      and names[("Dhruv Pendharkar",)]["systems"].get("Grafana") == 3, names)
check("the wrong approver (Karthik) is not Grafana's approver", all("Grafana" not in a["systems"] or a["people"] == ["Dhruv Pendharkar"]
                                                                   for a in access.approvers))
check("the replica needs Dhruv and Hamilton", names.get(("Dhruv Pendharkar", "Hamilton Park"), {}).get("systems")
      == {"prod read replica": 2}, names)
check("auth-service routes to Ishan, from CODEOWNERS", names.get(("Ishan Sinha",), {}).get("sources", [{}])[0].get("how")
      == "CODEOWNERS", names)
check("each approver links where ownership is written", all(s["url"] for a in access.approvers for s in a["sources"]))

exports = next((o for o in ops if "export" in o.label), None)
check("P2 names no system, so its approver is the one its runbook names", exports and exports.approvers
      and exports.approvers[0]["people"] == ["Hamilton Park"] and exports.approvers[0]["sources"][0]["how"] == "named in runbook",
      exports and exports.approvers)
check("a descriptive owners cell still names its system", "Grafana" in names.get(("Dhruv Pendharkar",), {}).get("systems", {}))

print("steps from the linked runbook")
check("P1's runbook is the Access Policy, linked by a GitHub file URL", access.runbook and access.runbook["title"] == "access-policy.md"
      and access.steps[0].startswith("Find the system's owner"), access.runbook)
check("P3 names no system, so it routes to the runbook's owner", keys and keys.approvers
      and keys.approvers[0]["people"] == ["Dhruv Pendharkar"] and keys.approvers[0]["sources"][0]["how"] == "runbook owner",
      keys and keys.approvers)
check("P3's runbook is the Drive doc, checklist steps read", keys and keys.runbook["title"] == "Runbook - Rotate Staging Keys"
      and len(keys.steps) == 4 and keys.steps_from == "runbook", keys and keys.steps)

print("decoys and noise")
check("flaky tests rank below both patterns with a reason", flaky and ops.index(flaky) > ops.index(keys)
      and flaky.why[0] == "ranks low: steps vary between instances", flaky and flaky.why)
check("the PagerDuty burst is not an ask", all("TRIGGERED" not in (i["title"] or "") for o in ops for i in o.items))
check("one wiki project filed three times, marked Duplicate, is one ask, so no pattern", not any("wiki" in o.label for o in ops))

print("as the viewer can see it")
out = opportunities(state, AS_OF, as_email="sam@tess.dev")
a2 = next(o for o in out if "access" in o.label)
check("outside the private channel: 6 asks, not 7", a2.frequency == 6, a2.frequency)
check("and the private ask is not in the evidence", all("CESC" not in i["id"] for i in a2.items))
check("the report says whose view it is", "as sam@tess.dev can see it" in markdown(out, viewer_email="sam@tess.dev"))
print(markdown(ops, viewer_email="hamilton@tess.dev"))

print("\nALL PASS" if ok else "\nFAILURES")
sys.exit(0 if ok else 1)
