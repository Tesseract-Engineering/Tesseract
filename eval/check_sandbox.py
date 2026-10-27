"""
check_sandbox.py - the sandbox's answer key, deterministic half, checked against the bucket.

The sandbox (branch ishan-sandbox-env of swarm-beta, sandbox/storyline.md) is a
fake company seeded into real GitHub, Jira, Slack and Drive accounts, with a
known story, deliberate mess and planted traps. This checks what the pipelines
must get right without a model: coverage, scope, redaction, ACLs, and the links
and facts the storyline says live across sources. No model, no network beyond
the bucket.

    S3_BUCKET=... S3_ENDPOINT=... python -m eval.check_sandbox      exit 1 if any check fails
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from collections.abc import Callable

from swarm.records import ItemRecord

from .bucket import Bucket

ORG = "tesseract-sandbox"
REPOS = ("auth-service", "infra", "handbook", "ml-platform")
SEEDED_PROJECTS = ("SBX", "OPS", "WEB", "ML", "CS")
CANARY = re.compile(r"CANARY-[A-Z]+-\d+")
# The planted fake secrets, as fragments so this file holds none of them whole.
PLANTED = ["AKIAIOSFODNN" + "7EXAMPLE", "wJalrXUtnFEMI" + "/K7MDENG", "hunter2" + "-not-real",
           "ghp_FakeSandbox" + "Token"]
HIDDEN_FILES = ("Token Leak Postmortem", "Acme Corp - Contract Addendum.pdf", "Northwind - MSA (signed).pdf",
                "Hiring Plan 2027.xlsx", "1-1 notes with Hamilton.docx", "scratch notes.docx",
                "Infrastructure Engineer - JD (draft).docx")

# sandbox/roster.yaml: each person's id in every tool. Hamilton and Dhruv have no GitHub account;
# Dhruv's commits carry his email, so his GitHub identity is that email.
ROSTER = {
    "Ishan Sinha": {"slack": "U0CC07LPSSU", "jira": "712020:0ec78558-16b2-4e6e-bc2b-7d5782baaf00",
                    "github": "ishan-sinha-ucb", "gdrive": "ishan.tesseract@gmail.com"},
    "Karthik Valluri": {"slack": "U0CB2DPLNSH", "jira": "712020:d4aa3945-2d8c-4288-a377-6e8a36d4977c",
                        "github": "kar722", "gdrive": "karthik.tesseract@gmail.com"},
    "Hamilton Park": {"slack": "U0CB7H3PNNN", "jira": "712020:e59b6a4d-a938-4cd6-8a3b-b44313a139a7",
                      "gdrive": "hamilton.tesseract@gmail.com"},
    "Dhruv Pendharkar": {"slack": "U0CB5Q14Q3Y", "jira": "712020:91dc33d6-2cc7-42b9-aa55-37cf262223d2",
                         "github": "dhruv.tesseract1@gmail.com", "gdrive": "dhruv.tesseract1@gmail.com"},
}

CHECKS: list[tuple[str, str, Callable[[Bucket], tuple[bool, str]]]] = []


def check(group: str, name: str):
    def register(fn):
        CHECKS.append((group, name, fn))
        return fn
    return register


# -- helpers ------------------------------------------------------------------ #

def gh(b: Bucket, repo: str, number: int) -> ItemRecord | None:
    return b.github(f"{ORG}/{repo}#{number}")


def doc(b: Bucket, repo: str, path: str) -> ItemRecord | None:
    return b.github(f"{ORG}/{repo}/{path}")


def links(a: ItemRecord | None, b: ItemRecord | None, kind: str | None = None) -> bool:
    return bool(a and b) and any(r.target == b.id and (kind is None or r.type == kind) for r in a.refs)


def either(a, b, kind=None) -> bool:
    return links(a, b, kind) or links(b, a, kind)


def need(*pairs: tuple[str, bool]) -> tuple[bool, str]:
    missing = [label for label, ok in pairs if not ok]
    return (not missing, "missing: " + "; ".join(missing) if missing else "")


def channel(b: Bucket, name: str) -> list[ItemRecord]:
    ids = {i.id for i in b.of("slack", "channel") if i.title == name}
    return [i for i in b.of("slack", "thread") if i.container in ids]


def everything(b: Bucket) -> str:
    """Every record ever written, raw payloads included, not just the current view."""
    if not hasattr(b, "_everything"):
        b._everything = "\n".join(b.every_object())
    return b._everything


# -- coverage ----------------------------------------------------------------- #

@check("coverage", "GitHub: 4 visible repos, 18 PRs, 6 issues, 7 docs")
def _(b):
    kinds = {k: len(b.of("github", k)) for k in ("repo", "pull_request", "issue", "document")}
    return kinds == {"repo": 4, "pull_request": 18, "issue": 6, "document": 7}, str(kinds)


@check("coverage", "Jira: all 79 seeded tickets across SBX, OPS, WEB, ML, CS")
def _(b):
    n = len([i for i in b.of("jira") if i.meta.get("project_key") in SEEDED_PROJECTS])
    return n == 79, f"{n} tickets"


@check("coverage", "Slack: 320 messages (330 seeded, minus #leadership's 10)")
def _(b):
    n = sum(i.meta.get("message_count", 0) for i in b.of("slack", "thread"))
    return n == 320, f"{n} messages"


@check("coverage", "Drive: all 36 readable files, and the 5 folders")
def _(b):
    files, folders = len(b.of("gdrive", "document")), len(b.of("gdrive", "folder"))
    return (files, folders) == (36, 5), f"{files} files, {folders} folders"


# -- scope and safety --------------------------------------------------------- #

@check("scope", "no canary from #leadership, web-app, restricted/ or customer-contracts/")
def _(b):
    found = sorted(set(CANARY.findall(everything(b))))
    return not found, ", ".join(found)


@check("scope", "nothing from the web-app repo, the #leadership channel or unshared Drive files")
def _(b):
    bad = [i.aliases[0] for i in b.of("github") if i.aliases and i.aliases[0].startswith(f"{ORG}/web-app")]
    bad += [i.title for i in b.of("slack", "channel") if i.title == "#leadership"]
    bad += [i.title for i in b.of("gdrive") if i.title in HIDDEN_FILES]
    known = {i.id.split(":")[2] for i in b.of("github", "repo")}
    bad += [r.target for i in b.items.values() for r in i.refs
            if r.target.startswith("github:repo:") and r.target.split(":")[2] not in known]
    return not bad, ", ".join(bad)


@check("scope", "no planted secret survives, anywhere")
def _(b):
    text = everything(b)
    leaked = [s[:6] + "..." for s in PLANTED if s in text]
    return not leaked, ", ".join(leaked)


@check("scope", "each planted secret is recorded as redacted where it was planted")
def _(b):
    def red(item, kind):
        return bool(item) and any(r.type == kind for r in item.redactions)
    infra_commits = [i for i in b.of("github", "commit") if i.container == (b.github(f"{ORG}/infra") or i).id]
    design = b.drive("Auth OIDC Migration - Design Doc")
    return need(("auth-service PR #4: GitHub token", red(gh(b, "auth-service", 4), "GitHub Token")),
                ("an infra commit: AWS access key", any(red(c, "AWS Access Key") for c in infra_commits)),
                ("#auth-migration: GitHub token", any(red(t, "GitHub Token") for t in channel(b, "#auth-migration"))),
                ("SBX-3: AWS secret key", red(b.jira("SBX-3"), "AWS Secret Access Key")),
                ("design doc: database URL", red(design, "Basic Auth Credentials")))


@check("scope", "every item has an ACL, and every group it names was written")
def _(b):
    missing = sorted({p for i in b.items.values() for p in i.acl.principals
                      if p.startswith("group:") and p not in b.groups})
    return not missing, ", ".join(missing[:5])


@check("scope", "private places are restricted, public ones are not")
def _(b):
    auth_mig = channel(b, "#auth-migration")
    eng = channel(b, "#eng")
    return need(("#auth-migration threads restricted", bool(auth_mig) and all(t.acl.visibility == "restricted" for t in auth_mig)),
                ("#eng threads internal", bool(eng) and all(t.acl.visibility == "internal" for t in eng)),
                ("handbook public", (h := b.github(f"{ORG}/handbook")) is not None and h.acl.visibility == "public"),
                ("auth-service restricted", (a := b.github(f"{ORG}/auth-service")) is not None
                 and a.acl.visibility == "restricted"))


# -- story 1: auth-service moves to OIDC --------------------------------------- #

@check("OIDC", "Ishan owns auth-service: CODEOWNERS and the handbook name him")
def _(b):
    owners = doc(b, "auth-service", ".github/CODEOWNERS")
    handbook = doc(b, "handbook", "README.md")
    return need(("CODEOWNERS mentions ishan-sinha-ucb", bool(owners) and any(p.handle == "ishan-sinha-ucb" for p in owners.people)),
                ("handbook lists auth-service with Ishan", bool(handbook) and "auth-service" in handbook.text
                 and ("Ishan" in handbook.text or "ishan-sinha-ucb" in handbook.text)))


@check("OIDC", "SBX-1 links the design doc, and the doc cites SBX-1")
def _(b):
    epic, design = b.jira("SBX-1"), b.drive("Auth OIDC Migration - Design Doc")
    return need(("SBX-1 -> doc", links(epic, design)), ("doc -> SBX-1", links(design, epic)))


@check("OIDC", "PR #3 shipped the discovery endpoint for SBX-2, linked both ways")
def _(b):
    pr, story = gh(b, "auth-service", 3), b.jira("SBX-2")
    return need(("PR #3 -> SBX-2", links(pr, story)), ("SBX-2 -> PR #3", links(story, pr)),
                ("PR #3 merged", bool(pr) and pr.meta.get("state") == "merged"),
                ("SBX-2 Done", bool(story) and story.meta.get("status") == "Done"))


@check("OIDC", "issue #1 and SBX-7 (Okta strips claims) link each other")
def _(b):
    issue, bug = gh(b, "auth-service", 1), b.jira("SBX-7")
    return need(("issue #1 -> SBX-7", links(issue, bug)), ("SBX-7 -> issue #1", links(bug, issue)))


@check("OIDC", "PR #4 fixes issue #1 and cites SBX-4; the decision is in #auth-migration")
def _(b):
    pr, decision = gh(b, "auth-service", 4), b.jira("SBX-4")
    thread = [t for t in channel(b, "#auth-migration") if links(t, decision)]
    return need(("PR #4 closes issue #1", links(pr, gh(b, "auth-service", 1), "closes")),
                ("PR #4 -> SBX-4", links(pr, decision)),
                ("an #auth-migration thread -> SBX-4", bool(thread)),
                ("SBX-4 -> PR #4", links(decision, pr)))


@check("OIDC", "SBX-5 is blocked by SBX-4, OPS-1 and WEB-1, and tied to issue #2 and open PR #5")
def _(b):
    sbx5 = b.jira("SBX-5")
    pr5 = gh(b, "auth-service", 5)
    return need(("SBX-4 blocks SBX-5", links(b.jira("SBX-4"), sbx5, "blocks")),
                ("OPS-1 blocks SBX-5", links(b.jira("OPS-1"), sbx5, "blocks")),
                ("WEB-1 blocks SBX-5", links(b.jira("WEB-1"), sbx5, "blocks")),
                ("issue #2 -> SBX-5", links(gh(b, "auth-service", 2), sbx5)),
                ("PR #5 -> SBX-5, open", links(pr5, sbx5) and pr5.meta.get("state") == "open"))


@check("OIDC", "OPS-1 (rotate signing keys) is assigned to infra's Dhruv")
def _(b):
    ops1 = b.jira("OPS-1")
    return bool(ops1) and any(p.name == "D. Pendharkar" and "assignee" in p.roles for p in ops1.people), ""


@check("OIDC", "the gateway ticket moved SBX-8 -> OPS-2, and infra issue #1's old key still links it")
def _(b):
    ops2 = b.jira("OPS-2")
    return need(("OPS-2 keeps SBX-8 as an alias", bool(ops2) and "SBX-8" in ops2.aliases),
                ("SBX-8 finds the same ticket", b.jira("SBX-8") is ops2),
                ("infra issue #1 -> OPS-2", links(gh(b, "infra", 1), ops2)))


@check("OIDC", "SOC2 is a forcing function: the auditor PDF is read, #eng discusses it")
def _(b):
    pdf = b.drive("SOC2 Auditor Questions - Authentication.pdf")
    return need(("PDF text extracted", bool(pdf) and pdf.meta.get("text_status") == "extracted"),
                ("#eng mentions SOC2", any("SOC2" in t.text for t in channel(b, "#eng"))))


# -- story 2: do we already have a retraining pipeline? ------------------------ #

@check("retraining", "ML-1 is a Done epic with 7 Done children and ML-8, the open gap")
def _(b):
    epic = b.jira("ML-1")
    kids = {i.meta["key"]: i.meta.get("status") for i in b.of("jira") if links(i, epic, "part_of")}
    expected = {f"ML-{n}" for n in (2, 3, 4, 5, 6, 7, 8, 12)}
    done = [k for k, s in kids.items() if s == "Done"]
    return need(("ML-1 Epic, Done", bool(epic) and epic.meta.get("issue_type") == "Epic" and epic.meta.get("status") == "Done"),
                (f"children {sorted(expected)} (got {sorted(kids)})", set(kids) == expected),
                ("all Done but ML-8", sorted(done) == sorted(expected - {"ML-8"})))


@check("retraining", "ML-8 is blocked by ML-10 and duplicated by ML-13")
def _(b):
    ml8 = b.jira("ML-8")
    ml13 = b.jira("ML-13")
    return need(("ML-10 blocks ML-8", links(b.jira("ML-10"), ml8, "blocks")),
                ("ML-13 -> ML-8, as a duplicate", links(ml13, ml8)
                 and {"type": "duplicates", "key": "ML-8"} in ml13.meta.get("links", [])))


@check("retraining", "ADR-014 is in Drive and in the repo, and says 48h")
def _(b):
    adr = b.drive("ADR-014 Continuous Retraining.docx")
    repo_adr = doc(b, "ml-platform", "docs/adr/014-continuous-retraining.md")
    return need(("Drive ADR read, Accepted, 48 hours", bool(adr) and "Accepted" in adr.text and "48" in adr.text),
                ("Drive ADR -> ML-1", links(adr, b.jira("ML-1"))),
                ("repo ADR -> ML-1", links(repo_adr, b.jira("ML-1"))))


@check("retraining", "the merged ml-platform PRs link ML-2, ML-3, ML-4, ML-5, ML-7 and ML-12")
def _(b):
    merged = [i for i in b.of("github", "pull_request")
              if i.aliases and i.aliases[0].startswith(f"{ORG}/ml-platform#") and i.meta.get("state") == "merged"]
    return need(*[(f"a merged PR -> {k}", any(links(pr, b.jira(k)) for pr in merged))
                  for k in ("ML-2", "ML-3", "ML-4", "ML-5", "ML-7", "ML-12")])


@check("retraining", "the gap shows in code too: the draft backfill PR and the empty-training-set issue link ML-8")
def _(b):
    ml8 = b.jira("ML-8")
    drafts = [i for i in b.of("github", "pull_request") if i.meta.get("draft") and i.meta.get("state") == "open"
              and i.aliases[0].startswith(f"{ORG}/ml-platform#")]
    return need(("open draft PR -> ML-8", any(links(pr, ml8) for pr in drafts)),
                ("issue #10 -> ML-8", links(gh(b, "ml-platform", 10), ml8)))


@check("retraining", "the traps are there to be avoided: DRAFT design, July copy, ML-9, Feast PR closed")
def _(b):
    feast = [i for i in b.of("github", "pull_request") if "Feast" in i.title]
    return need(("ML Retraining - Design (DRAFT) read", bool(b.drive("ML Retraining - Design (DRAFT).docx"))),
                ("Copy of ADR-014 read", bool(b.drive("Copy of ADR-014 Continuous Retraining.docx"))),
                ("ML-9 nightly cron present", bool(b.jira("ML-9")) and "cron" in b.jira("ML-9").title.lower()),
                ("Feast PR closed, not merged", bool(feast) and all(p.meta.get("state") == "closed" for p in feast)))


# -- story 3: can customers still use the CSV export? -------------------------- #

@check("CSV export", "WEB-8 removed the endpoint; WEB-9, WEB-10, WEB-11 are tied to it")
def _(b):
    web8 = b.jira("WEB-8")
    return need(("WEB-8 Done", bool(web8) and web8.meta.get("status") == "Done"),
                *[(f"{k} <-> WEB-8", either(b.jira(k), web8)) for k in ("WEB-9", "WEB-10", "WEB-11")])


@check("CSV export", "Acme's escalation CS-4 is tied to WEB-8, and CS-1 duplicates it")
def _(b):
    cs4 = b.jira("CS-4")
    return need(("CS-4 <-> WEB-8", either(cs4, b.jira("WEB-8"))),
                ("CS-1 -> CS-4, as a duplicate", links(b.jira("CS-1"), cs4)))


@check("CSV export", "the migration note says 410; the stale FAQ and 2025 overview are there to be avoided")
def _(b):
    note, faq = b.drive("Export Migration Note.docx"), b.drive("Customer FAQ - Data Exports.docx")
    return need(("Export Migration Note read, says 410", bool(note) and "410" in note.text),
                ("Customer FAQ read (stale)", bool(faq) and bool(faq.text)),
                ("Architecture Overview (2025) read", bool(b.drive("Architecture Overview (2025)"))))


@check("CSV export", "#releases announces v4.6.0 and links WEB-8")
def _(b):
    rel = [t for t in channel(b, "#releases") if "4.6.0" in t.text]
    return need(("a #releases thread -> WEB-8", any(links(t, b.jira("WEB-8")) for t in rel)))


# -- the deliberate mess --------------------------------------------------------- #

@check("mess", "duplicate pairs are linked as duplicates")
def _(b):
    pairs = [("CS-1", "CS-4"), ("ML-13", "ML-8"), ("WEB-7", "WEB-5"), ("SBX-19", "SBX-14")]
    return need(*[(f"{a} -> {c}", links(b.jira(a), b.jira(c))
                   and {"type": "duplicates", "key": c} in b.jira(a).meta.get("links", [])) for a, c in pairs])


@check("mess", "misfiled, vague and badly named items are all ingested")
def _(b):
    return need(("WEB-6 (an ML problem in WEB) <-> ML-1", either(b.jira("WEB-6"), b.jira("ML-1"))),
                ("SBX-17 and OPS-16", bool(b.jira("SBX-17")) and bool(b.jira("OPS-16"))),
                ("Untitled document.docx", bool(b.drive("Untitled document.docx"))),
                ("Platform Weekly Sync Notes v2 FINAL (1).docx", bool(b.drive("Platform Weekly Sync Notes v2 FINAL (1).docx"))))


@check("mess", "bot noise is kept as text, and bots are never people")
def _(b):
    noisy = channel(b, "#ci") + channel(b, "#alerts")
    names = {p.name for t in noisy for p in t.people}
    return need(("#ci and #alerts ingested", bool(noisy)),
                ("no bot among their people", not names & {"GitHub Actions", "PagerDuty"}))


@check("mess", "Dhruv is 'D. Pendharkar' in Jira and 'Dhruv Pendharkar' elsewhere")
def _(b):
    jira_names = {p.name for i in b.of("jira") for p in i.people}
    slack_names = {p.name for i in b.of("slack") for p in i.people}
    return need(("Jira: D. Pendharkar", "D. Pendharkar" in jira_names),
                ("Slack: Dhruv Pendharkar", "Dhruv Pendharkar" in slack_names))


# -- identity -------------------------------------------------------------------- #

def clusters(b: Bucket) -> dict[str, set[tuple[str, str]]]:
    return {r.person_id: {(m.source, m.handle) for m in r.members}
            for r in b.identities.values() if r.merged_into is None}


@check("identity", "each person is one cluster holding their account in every tool")
def _(b):
    found = clusters(b)
    out = []
    for person, accounts in ROSTER.items():
        wanted = set(accounts.items())
        holding = [pid for pid, members in found.items() if members & wanted]
        if len(holding) != 1 or not wanted <= found[holding[0]]:
            split = [sorted(src for src, _ in found[pid] & wanted) for pid in holding]
            out.append((f"{person} (split across {split})", False))
    return need(*out) if out else (bool(found), "" if found else "no identity records")


@check("identity", "no cluster holds two people (no false merges)")
def _(b):
    merged = []
    for members in clusters(b).values():
        people = [p for p, accounts in ROSTER.items() if members & set(accounts.items())]
        if len(people) > 1:
            merged.append(" + ".join(people))
    return not merged, ", ".join(merged)


@check("identity", "every person named on an item is in exactly one cluster")
def _(b):
    named = {p.id for i in b.items.values() for p in i.people}
    placed = Counter(m.id for r in b.identities.values() if r.merged_into is None for m in r.members)
    missing = sorted(named - set(placed))
    twice = sorted(k for k, n in placed.items() if n > 1)
    return need((f"not in any cluster: {missing[:3]}", not missing), (f"in two: {twice[:3]}", not twice))


# -- link hygiene ---------------------------------------------------------------- #

@check("links", "every Jira key resolves, except keys of projects nobody granted")
def _(b):
    left = sorted({k for i in b.items.values() for k in i.meta.get("jira_keys", [])})
    return not left, ", ".join(left)


@check("links", "every GitHub link into the granted repos resolves")
def _(b):
    left = sorted({link for i in b.items.values() for link in i.meta.get("github_links", [])
                   if any(f"{ORG}/{r}" in link for r in REPOS)})
    return not left, ", ".join(left[:5])


@check("links", "every ref to a GitHub or Jira item points at an item in the bucket")
def _(b):
    dangling = sorted({r.target for i in b.items.values() for r in i.refs
                       if r.target.split(":")[0] in ("github", "jira") and ":commit:" not in r.target
                       and r.target not in b.items})
    return not dangling, ", ".join(dangling[:5])


def main() -> int:
    b = Bucket.from_env()
    failed = 0
    group = None
    for g, name, fn in CHECKS:
        if g != group:
            print(f"\n{g}")
            group = g
        try:
            ok, detail = fn(b)
        except Exception as e:                       # a check that crashes is a failed check, not a dead run
            ok, detail = False, f"{type(e).__name__}: {e}"
        failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail}" if detail and not ok else ""))
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
