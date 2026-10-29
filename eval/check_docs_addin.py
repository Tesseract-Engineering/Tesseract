"""
check_docs_addin.py - the Docs add-in's answer key: the SOC2 auditor questions, answered from the
sandbox graph as two people who can read different things.

The sandbox's planning folder holds "SOC2 Auditor Questions - Authentication" (Legal, FY2025
follow-up). Its five questions are what the add-in demo answers. The key, from what the sandbox
holds (docs_addin/README.md has the table):

    1 OIDC          in progress: discovery shipped, ID tokens underway, cutover blocked
    2 rotation      the runbook's 24h overlap procedure; no schedule; only a staging dry run on record
    3 claims        tenant_id and roles, namespaced (option B, SBX-4)
    4 deprecation   /oauth/token (SBX-5), no calendar date; the CSV export is not an auth endpoint
    5 key access    KMS via Terraform and little else: partial, and it should say so

Ishan reads everything. Hamilton is not a reader of the auth-service repo, so for him the GitHub
PRs must be locked: never cited, counted, with Ishan named as who can share them. Neither may cite
the questionnaire itself, the June DRAFT design, a "Copy of" or "FINAL (1)" duplicate, or the 2025
architecture overview.

Answers vary run to run, so checks look for the right sources and words, not exact text.

    NEO4J_PASSWORD=... ADDIN_MODEL_REPLAY=data/replay-addin python -m eval.check_docs_addin
    (the graph built into ADDIN_NS, default kg:sandbox, from the sandbox bucket; exit 1 on a failure)
"""

from __future__ import annotations

import os
import re
import sys

from docs_addin.answer import answer, model_from_env
from docs_addin.retrieve import directory, gather
from graph.store import Neo4jStore

TODAY = "2026-10-28"          # fixed, so recorded answers replay
ISHAN = "ishan.tesseract@gmail.com"
HAMILTON = "hamilton.tesseract@gmail.com"
VIEWERS = (ISHAN, HAMILTON)

QUESTIONS = {
    "oidc": "Does the authentication service use an industry-standard protocol (for example OpenID Connect) "
            "for federated login?",
    "rotation": "How are token signing keys rotated, and how often? Provide evidence of the last rotation.",
    "claims": "Which custom claims are issued in access tokens, and how are they validated downstream?",
    "deprecation": "Are deprecated authentication endpoints inventoried, with a removal date?",
    "access": "How is access to token signing material restricted?",
}

NEVER_CITED = [r"SOC2 Auditor Questions", r"\(DRAFT\)", r"^Copy of", r"FINAL \(1\)", r"Architecture Overview \(2025\)"]

CHECKS = []


def check(name: str, viewers=VIEWERS):
    def register(fn):
        CHECKS.append((name, viewers, fn))
        return fn
    return register


def cites(out: dict, *patterns: str) -> bool:
    """Some cited source's label or title matches one of the patterns."""
    return any(re.search(p, s["label"]) or re.search(p, s["title"]) for s in out["sources"] for p in patterns)


def says(out: dict, *patterns: str) -> bool:
    return all(re.search(p, out["answer"], re.I) for p in patterns)


def gaps(out: dict, pattern: str) -> bool:
    return any(re.search(pattern, g, re.I) for g in out["gaps"])


def cited(out: dict) -> str:
    return "; ".join(s["label"][:50] for s in out["sources"]) or "nothing"


# -- per question --------------------------------------------------------------- #

@check("1 oidc: answers from the migration's own records")
def _(a, v):
    out = a["oidc"]
    ok = out["status"] != "none" and cites(out, r"SBX-1\b", r"Auth OIDC Migration - Design Doc", r"OIDC Rollout Plan") \
        and says(out, r"OIDC|OpenID")
    return ok, f"{out['status']}; cited {cited(out)}"


@check("1 oidc: does not claim the migration is finished")
def _(a, v):
    out = a["oidc"]
    ok = says(out, r"progress|not yet|blocked|underway|remain|until|pending|partial")
    return ok, out["answer"][:200]


@check("2 rotation: the runbook's procedure, with its 24h overlap")
def _(a, v):
    out = a["rotation"]
    ok = cites(out, r"Key Rotation Runbook", r"OPS-1\b") and says(out, r"24")
    return ok, f"cited {cited(out)}"


@check("2 rotation: partial, and says no schedule is defined")
def _(a, v):
    out = a["rotation"]
    ok = out["status"] == "partial" and gaps(out, r"schedule|frequen|how often|cadence|interval")
    return ok, f"{out['status']}; gaps {out['gaps']}"


@check("3 claims: tenant_id and roles, namespaced, from the SBX-4 decision")
def _(a, v):
    out = a["claims"]
    ok = says(out, r"tenant_id", r"roles", r"namespac") and cites(
        out, r"SBX-4\b", r"Auth OIDC Migration - Design Doc", r"custom claims", r"OIDC Rollout Plan")
    return ok, f"cited {cited(out)}"


@check("4 deprecation: /oauth/token from SBX-5, and no date on record")
def _(a, v):
    out = a["deprecation"]
    ok = says(out, r"/oauth/token") and cites(out, r"SBX-5\b") and out["status"] == "partial" \
        and gaps(out, r"date|deadline|when")
    return ok, f"{out['status']}; cited {cited(out)}; gaps {out['gaps']}"


@check("4 deprecation: the CSV export is out of scope")
def _(a, v):
    out = a["deprecation"]
    return not says(out, r"csv"), out["answer"][:200]


@check("5 key access: partial or none, and says what is missing")
def _(a, v):
    out = a["access"]
    ok = out["status"] in ("partial", "none") and bool(out["gaps"] or out["status"] == "none")
    return ok, f"{out['status']}; gaps {out['gaps']}"


# -- every answer --------------------------------------------------------------- #

@check("no answer cites the questionnaire, a draft, a copy or the 2025 overview")
def _(a, v):
    bad = [(k, s["label"]) for k, out in a.items() for s in out["sources"]
           if any(re.search(p, s["title"]) for p in NEVER_CITED)]
    return not bad, str(bad)[:300]


@check("every answer that is not 'none' cites something, and every marker names a cited source")
def _(a, v):
    bad = []
    for k, out in a.items():
        marks = {int(m) for m in re.findall(r"\[(\d+)\]", out["answer"])}
        if out["status"] != "none" and not out["sources"]:
            bad.append(f"{k}: no sources")
        if marks - set(range(1, len(out["sources"]) + 1)):
            bad.append(f"{k}: markers {sorted(marks)} for {len(out['sources'])} sources")
    return not bad, "; ".join(bad)


@check("no he/she for people", VIEWERS)
def _(a, v):
    bad = [k for k, out in a.items() if re.search(r"\b(he|she|his|her|him)\b", out["answer"] + " "
                                                   + ((out["ask"] or {}).get("reason") or ""), re.I)]
    return not bad, ", ".join(bad)


# -- access --------------------------------------------------------------------- #

@check("Ishan: nothing locked, and GitHub evidence is cited", (ISHAN,))
def _(a, v):
    locked = {k: out["locked"]["count"] for k, out in a.items() if out["locked"]["count"]}
    github = [k for k, out in a.items() if any(s["source"] == "github" for s in out["sources"])]
    return not locked and bool(github), f"locked {locked}; GitHub cited in {github}"


@check("Hamilton: no GitHub source cited (not a reader of the repos)", (HAMILTON,))
def _(a, v):
    bad = [(k, s["label"]) for k, out in a.items() for s in out["sources"] if s["source"] == "github"]
    return not bad, str(bad)[:300]


@check("Hamilton: the linked PRs are locked, with Ishan named to share them", (HAMILTON,))
def _(a, v):
    out = a["claims"]
    lk = out["locked"]
    ok = lk["count"] > 0 and set(lk["by_source"]) == {"github"} and any(o["name"] == "Ishan Sinha" for o in lk["owners"])
    return ok, str(lk)


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ns = os.environ.get("ADDIN_NS", "kg:sandbox")
    store = Neo4jStore()
    model = model_from_env()
    people = directory(store, ns)
    answers = {}
    tokens = [0, 0]
    for v in VIEWERS:
        answers[v] = {}
        for key, q in QUESTIONS.items():
            out = answer(model, q, gather(store, ns, v, q), people, today=TODAY)
            answers[v][key] = out
            tokens[0] += out["tokens"]["input"]
            tokens[1] += out["tokens"]["output"]
    store.close()

    for v in VIEWERS:
        print(f"\n{v}")
        for key, out in answers[v].items():
            print(f"  {key:12} {out['status']:8} {len(out['sources']):>2}/{out['read']:<2} cited, "
                  f"{out['locked']['count']} locked, ask {(out['ask'] or {}).get('name', '-')}")
    print(f"\n{model.name}, {tokens[0]:,} input and {tokens[1]:,} output tokens (zero when replayed)")

    failed = 0
    total = 0
    for name, viewers, fn in CHECKS:
        for v in viewers:
            total += 1
            try:
                ok, detail = fn(answers[v], v)
            except Exception as e:
                ok, detail = False, f"{type(e).__name__}: {e}"
            failed += not ok
            who = v.split(".")[0]
            print(f"  {'PASS' if ok else 'FAIL'}  [{who}] {name}" + (f"\n        {detail}" if detail and not ok else ""))
    print(f"\n{total - failed}/{total} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
