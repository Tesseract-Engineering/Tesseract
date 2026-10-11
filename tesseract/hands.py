"""hands.py - the hands: from a grounded answer to drafts aimed at the right person.

The generic answer path used to end at words. This module turns an answer into
`proposed_actions` for the approval door (actions.py), and it finds WHO each
action goes to from the graph, never from a hardcoded name:

    owner of a document   (d)-[:CREDITS {role: owner|author}]->(p)   the header said so, quote kept
    assignee of a ticket  (d)-[:ASSIGNED_TO]->(p)
    author of a SharePoint file  (p)-[:HAS_ACCOUNT]->()-[:AUTHORED]->(d)
    someone who left      Person.status = 'alumni'   skipped, and the draft says so

Three situations produce actions, chosen from the question and the evidence:

    access      a restricted document matches (the viewer cannot read it), or the
                question asks how to get access   -> email + Slack draft to its owner
    precedent   "has anyone built / is there an existing / reuse"   -> intro to
                the team that already built it, cc their manager
    incident    stale / outage / OOM / root cause                   -> Slack heads-up
                to the owners of what the evidence points at

Nothing is sent. Each action is a draft the person approves (exact-payload
hash, actions.py); executing it writes the draft to the outbox. Drafts never
quote or title a restricted document: a sealed record is referred to by its
ACL group only.

How to reach someone comes from the graph too: the email and Slack handle on
the accounts identity resolution linked to them. When no account carries an
email, the company's email_domain (config.py) gives first.last@domain, and the
contact says it was a convention rather than a record.
"""

from __future__ import annotations

import re
from typing import Callable

from . import graph
from .config import settings

ACCESS_Q = re.compile(r"\b(access|permissions?|how (do|can) i get|who do i ask|get added|get approved)\b", re.I)
INCIDENT_Q = re.compile(r"\b(stale|outage|incident|oom\w*|down|failing|failures?|spiking|latency|errors?|"
                        r"crash\w*|root cause|frozen|not updating|broken)\b", re.I)
PRECEDENT_Q = re.compile(r"\b(has anyone|anyone (here )?(built|done|tried)|already (built|exists?|have|got)|"
                         r"existing|re-?use|precedent|prior art|instead of (building|writing)|"
                         r"i'?m (building|planning|writing)|we (plan|need)|planning|going to (build|write)|"
                         r"help (me|us) (architect|design|build|plan|set up|implement|create)|"
                         r"(want|need|would like|'d like) to (build|work on|create|set up|design|architect|implement)|"
                         r"how (should|would|do) (i|we) (build|design|architect|implement))\b", re.I)

Q_CREDITS = """
MATCH (d) WHERE d.ns = $ns AND d.id IN $ids
MATCH (d)-[c:CREDITS]->(p:Person)
RETURN d.id AS doc, p.name AS name, coalesce(p.status, 'employee') AS status, p.departedAt AS departed,
       c.role AS role, c.quote AS quote, coalesce(d.text, '') CONTAINS 'DRAFT' AS draft
"""
Q_ASSIGNED = """
MATCH (d) WHERE d.ns = $ns AND d.id IN $ids
MATCH (d)-[:ASSIGNED_TO]->(p:Person)
RETURN d.id AS doc, p.name AS name, coalesce(p.status, 'employee') AS status, p.departedAt AS departed,
       coalesce(d.state, '') AS state
"""
DONE_STATES = {"closed", "done", "resolved"}
Q_AUTHORED = """
MATCH (d) WHERE d.ns = $ns AND d.id IN $ids AND d.id STARTS WITH 'sharepoint:'
MATCH (p:Person)-[:HAS_ACCOUNT]->(:Account)-[:AUTHORED]->(d)
RETURN DISTINCT d.id AS doc, p.name AS name, coalesce(p.status, 'employee') AS status, p.departedAt AS departed
"""

Q_SPEAKERS = """
MATCH (d) WHERE d.ns = $ns AND d.id IN $ids AND (d.id STARTS WITH 'thread:')
MATCH (p:Person)-[:HAS_ACCOUNT]->(:Account)-[:AUTHORED]->(d)
RETURN DISTINCT d.id AS doc, p.name AS name, coalesce(p.status, 'employee') AS status, p.departedAt AS departed,
       split(coalesce(d.text, ''), '\n')[0] AS first_line
"""

Q_CONTACTS = """
MATCH (p:Person {ns: $ns}) WHERE p.name IN $names
OPTIONAL MATCH (p)-[:HAS_ACCOUNT]->(a:Account)
WITH p, collect(a) AS accounts
RETURN p.name AS name, coalesce(p.title, '') AS title,
       [a IN accounts WHERE a.email IS NOT NULL | a.email] AS emails,
       [a IN accounts WHERE a.source = 'slack' AND a.handle IS NOT NULL | a.handle] AS handles
"""

Run = Callable[..., list[dict]]


# --------------------------------------------------------------------------- #
# people
# --------------------------------------------------------------------------- #

def _slug(name: str) -> str:
    return ".".join(re.sub(r"[^a-z\s'-]", "", name.lower()).replace("'", "").split())


def contact(name: str, row: dict | None = None, domain: str | None = None) -> dict:
    """How to reach `name`, from their accounts (`row`, a Q_CONTACTS row) when
    one carries an address, else the company convention first.last@domain.
    `email_basis` says which: account, convention, or unknown."""
    row = row or {}
    domain = settings().company.email_domain if domain is None else domain
    emails, handles = row.get("emails") or [], row.get("handles") or []
    if emails:
        email, basis = emails[0], "account"
    elif domain:
        email, basis = f"{_slug(name)}@{domain}", "convention"
    else:
        email, basis = "", "unknown"
    slack = handles[0] if handles else _slug(name)
    return {"name": name, "email": email, "slack": "@" + slack.lstrip("@"),
            "title": row.get("title") or "", "email_basis": basis}


def contacts(run: Run, names: list[str]) -> dict[str, dict]:
    """name -> contact(), one query for all of them."""
    rows = {r["name"]: r for r in run(Q_CONTACTS, names=names)} if names else {}
    return {n: contact(n, rows.get(n)) for n in names}


def resolve_owners(run: Run, doc_ids: list[str], limit: int = 3,
                   exclude: set[str] | None = None,
                   built_only: bool = False) -> tuple[list[dict], list[dict]]:
    """Who to contact about these records, and who is named but has left.

    Weights: header owner 3, header author 2, ticket assignee 2, SharePoint
    author 1, someone who spoke in a Slack thread 1. Alumni are returned
    separately and never contacted, and `exclude` (the asker) never appears."""
    if not doc_ids:
        return [], []
    people: dict[str, dict] = {}
    alumni: dict[str, dict] = {}

    def add(row: dict, basis: str, weight: int) -> None:
        name = row.get("name")
        if not name or name in (exclude or ()):
            return
        if row.get("status") == "alumni":
            alumni.setdefault(name, {"name": name, "departed": str(row.get("departed") or "")[:10],
                                     "doc": row["doc"]})
            return
        p = people.setdefault(name, {"name": name, "score": 0, "bases": [], "docs": [], "quote": ""})
        p["score"] += weight
        if basis not in p["bases"]:
            p["bases"].append(basis)
        if row["doc"] not in p["docs"]:
            p["docs"].append(row["doc"])
        if basis.startswith("header") and row.get("quote") and not p["quote"]:
            p["quote"] = row["quote"]

    for r in run(Q_CREDITS, ids=doc_ids):
        # A draft plan's author is planning the thing, not someone who built it.
        if built_only and r.get("draft"):
            continue
        add(r, f"header {r.get('role') or 'credit'}", 3 if r.get("role") == "owner" else 2)
    for r in run(Q_ASSIGNED, ids=doc_ids):
        # For "who already built this", an open ticket's assignee is someone
        # planning the same thing, not someone who built it.
        if built_only and (r.get("state") or "").lower() not in DONE_STATES:
            continue
        add(r, "ticket assignee", 2)
    for r in run(Q_AUTHORED, ids=doc_ids):
        add(r, "document author", 1)
    for r in run(Q_SPEAKERS, ids=doc_ids):
        # The first line of a thread is the person who asked; the others answered.
        if (r.get("first_line") or "").startswith(f"{r.get('name')}:"):
            continue
        add(r, "answered in thread", 1)
    ranked = sorted(people.values(), key=lambda p: (-p["score"], p["name"]))[:limit]
    return ranked, list(alumni.values())


# --------------------------------------------------------------------------- #
# wording
# --------------------------------------------------------------------------- #

def _topic(question: str, limit: int = 90) -> str:
    t = " ".join(question.strip().rstrip("?.! ").split())
    return t if len(t) <= limit else t[: limit - 1].rstrip() + "…"


def _names(owners: list[dict]) -> str:
    firsts = [o["name"].split()[0] for o in owners]
    return firsts[0] if len(firsts) == 1 else ", ".join(firsts[:-1]) + " and " + firsts[-1]


def _site(groups: list[str]) -> str:
    out = []
    for g in groups:
        out.append("the " + g.split(":", 1)[1].replace("-", " ").title() + " SharePoint site"
                   if g.startswith("sharepoint:") else g)
    return " and ".join(dict.fromkeys(out)) or "a restricted location"


def _alumni_note(alumni: list[dict]) -> str:
    if not alumni:
        return ""
    bits = [f"{a['name']}" + (f" (left {a['departed']})" if a["departed"] else " (has left)") for a in alumni]
    return f"Note: {', '.join(bits)} appear on this work but are no longer at the company, so I am asking you instead."


def _sign(asker: str | None) -> str:
    return f"Thanks,\n{asker}" if asker else "Thanks"


def _readable(claims: list[dict], n: int = 4) -> list[dict]:
    return [c for c in claims if c.get("node_id") and not c.get("restricted")][:n]


def _email(to: list[dict], cc: list[dict], subject: str, body: str, cites: list[str], aid: str) -> dict:
    emails = [t["email"] or t["name"] for t in to]
    return {"id": aid, "type": "email.draft", "to": emails[0],
            "payload": {"to": emails, "to_names": [t["name"] for t in to],
                        "cc": [c["email"] or c["name"] for c in cc], "subject": subject,
                        "body": body, "citations": cites}}


def _slack(to: list[dict], text: str, cites: list[str], aid: str) -> dict:
    handles = [t["slack"] for t in to]
    return {"id": aid, "type": "slack.draft", "to": handles[0],
            "payload": {"to": handles, "to_names": [t["name"] for t in to], "text": text, "citations": cites}}


# --------------------------------------------------------------------------- #
# the three situations
# --------------------------------------------------------------------------- #

def _access(question, res, owners, alumni, asker, restricted: bool) -> list[dict]:
    topic = _topic(question)
    seen = _readable(res.get("claims") or [])
    seen_lines = "".join(f"\n- {c['title']} ({c['source']})" for c in seen)
    cites = [c["node_id"] for c in seen]
    to = owners
    where = _site(res.get("restricted_groups") or [])
    if restricted:
        why = (f"The documents that cover it are restricted to {where}, and I can't read them with my "
               f"current access. You are listed as an owner there.")
        ask = ("Could you either grant me read access to the relevant document, or point me to the right "
               "approver and the request I should file?")
    else:
        why = "From the docs I read, getting this needs your approval."
        ask = "Could you tell me what you need from me, and approve it if that is right?"
    body = (f"Hi {_names(owners)},\n\nI'm trying to find out: \"{question.strip()}\"\n\n{why}\n{ask}\n"
            + (f"\nWhat I can already see on this topic:{seen_lines}\n" if seen_lines else "")
            + (f"\n{_alumni_note(alumni)}\n" if alumni else "") + f"\n{_sign(asker)}")
    text = (f"Hi {_names(owners)}, quick one. I'm trying to find out: \"{topic}\". "
            + (f"It lives in {where}, which I can't open. " if restricted else "")
            + "Could you grant access or point me to the right approver?")
    return [_email(to, [], f"Access request: {topic}", body, cites, "a1"),
            _slack(to, text, cites, "a2")]


def _precedent(question, res, owners, alumni, asker) -> list[dict]:
    topic = _topic(question)
    seen = _readable(res.get("claims") or [], 3)
    items = "".join(f"\n- {c['title']} ({c['source']}, {c.get('date') or 'undated'})"
                    + (f": \"{(c.get('quote') or '').strip()[:200]}\"" if c.get("quote") else "") for c in seen)
    cites = [c["node_id"] for c in seen]
    to = owners
    body = (f"Hi {_names(owners)},\n\nI'm working on this: \"{question.strip()}\"\n\n"
            f"While searching I found that your team has already done related work:{items}\n\n"
            f"Rather than build something parallel, could we get 30 minutes so I can understand how it works, "
            f"what went wrong along the way, and what it would take to build on it?\n"
            + (f"\n{_alumni_note(alumni)}\n" if alumni else "") + f"\n{_sign(asker)}")
    lead = seen[0]["title"] if seen else "your existing work"
    text = (f"Hi {_names(owners)}, I'm working on this: \"{topic}\". I found {lead}. "
            f"Could I grab 30 minutes to see how to reuse it instead of building a second one?")
    subject = f"Reusing your work: {lead}" if seen else f"Reusing existing work: {topic}"
    repo = _repo_of([c["node_id"] for c in seen])
    talk = [_email(to, [], subject[:110], body, cites, "a2" if repo else "a1"),
            _slack(to, text, cites, "a3" if repo else "a2")]
    return ([_agent(question, seen, owners, repo, "a1")] if repo else []) + talk


# Where code lives, read off a record's path: a service or package inside a
# monorepo (".../services/billing/"), or a repository folder (".../ml-platform-repo/").
REPO_PATH = (re.compile(r"^(.*?/?(?:services|packages|apps)/[^/]+)/"),
             re.compile(r"^(.*?[^/]+-repo)/"))


def _repo_of(node_ids: list[str]) -> str:
    """The code home of the precedent, or "" when no evidence lives in code."""
    for nid in node_ids:
        path = nid.split(":", 1)[-1]
        for pattern in REPO_PATH:
            m = pattern.match(path)
            if m:
                return m.group(1)
    return ""


def _agent(question, seen, owners, repo, aid) -> dict:
    """A coding agent brief: build on the existing system, in its repo, as a draft
    for its owners to review. The UI shows the run; nothing merges or deploys."""
    lead = seen[0]["title"] if seen else "the existing system"
    reviewers = [o["name"] for o in owners]
    steps = [f"Reading {c['title']} ({c['source']})" for c in seen[:3]]
    steps += [f"Checking out {repo} on a new branch",
              f"Drafting the changes to build on {lead}, following its onboarding docs",
              "Running the repo's tests and dry-run checks",
              f"Opening a draft PR for review by {', '.join(reviewers)}"]
    return {"id": aid, "type": "agent.launch", "to": repo,
            "payload": {"title": f"Build on {lead}", "goal": question.strip(), "repo": repo,
                        "reuse": lead, "read_first": [c["node_id"] for c in seen[:3]],
                        "reviewers": reviewers, "steps": steps,
                        "will_not": ["merge", "deploy", "touch production"],
                        "citations": [c["node_id"] for c in seen]}}


def _incident(question, res, owners, alumni, asker) -> list[dict]:
    topic = _topic(question)
    seen = _readable(res.get("claims") or [], 4)
    cites = [c["node_id"] for c in seen]
    to = owners
    heads = " ".join((res.get("headline") or "").split())[:320]
    if re.match(r"\d+ (grounded )?records?\b", heads) and seen:      # the keyword path's placeholder headline
        heads = "Closest record: \"" + " ".join((seen[0].get("quote") or seen[0]["title"]).split())[:260] + "\"."
    text = (f"Heads-up from Tesseract on \"{topic}\": {heads} "
            f"Evidence: " + "; ".join(c["title"] for c in seen) + ". "
            "Can one of you confirm and own the fix? " + _alumni_note(alumni)).strip()
    return [_slack(to, text, cites, "a1")]


def attach(res: dict, question: str, ctx: dict, asker: str | None = None,
           run: Run = graph.run) -> dict:
    """Add owners and proposed_actions to an answer when the situation calls for
    it; otherwise return it untouched. `ctx` is the keyword path's context
    (restricted_ids). `run` executes Cypher; tests pass a fake."""
    if res.get("proposed_actions"):
        return res
    claims = res.get("claims") or []
    restricted_ids = list(ctx.get("restricted_ids") or []) if res.get("restricted_matches") else []
    readable_ids = [c["node_id"] for c in _readable(claims)]

    if restricted_ids:
        intent, ids = "access", restricted_ids
    elif res.get("verdict") == "INFO" and readable_ids:
        if ACCESS_Q.search(question):
            intent, ids = "access-process", readable_ids
        elif INCIDENT_Q.search(question):
            intent, ids = "incident", readable_ids
        elif PRECEDENT_Q.search(question):
            intent, ids = "precedent", readable_ids
        else:
            return res
    else:
        return res

    owners, alumni = resolve_owners(run, ids, exclude={asker} if asker else None,
                                    built_only=intent == "precedent")
    if not owners:
        return {**res, "hands": {"intent": intent, "owners": [], "skipped_alumni": alumni,
                                 "note": "no current owner found in the graph for this evidence"}}
    reach = contacts(run, [o["name"] for o in owners])
    for o in owners:
        o.update({k: v for k, v in reach[o["name"]].items() if k != "name"})

    if intent == "access":
        actions = _access(question, res, owners, alumni, asker, True)
    elif intent == "access-process":
        actions = _access(question, res, owners, alumni, asker, False)
    elif intent == "precedent":
        actions = _precedent(question, res, owners, alumni, asker)
    else:
        actions = _incident(question, res, owners, alumni, asker)

    hidden = intent == "access"       # a sealed record's text and title never leave the graph
    shown = [{"name": o["name"], "email": o["email"], "slack": o["slack"], "basis": o["bases"],
              **({} if hidden else {"quote": o["quote"]})} for o in owners]
    return {**res,
            "owners": [{"role": "owner", "name": o["name"],
                        "source": "" if hidden else (o["docs"][0] if o["docs"] else "")} for o in owners],
            "approval_required": [{"who": f"{asker or 'You'} (sender)",
                                   "why": "Nothing is sent. Approving saves the draft for you to review and send.",
                                   "source": "graph"}],
            "proposed_actions": actions,
            "hands": {"intent": intent, "owners": shown, "skipped_alumni": alumni}}
