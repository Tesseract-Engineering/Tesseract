"""
check_docs_addin.py - the Docs add-in's answer key, in two parts.

The design doc (docs_addin/demo_doc.json, the demo): "Design: billing-service verifies OIDC tokens
directly", each paragraph checked as Ishan would check it. The paragraphs that walk into the
sandbox's history must get the note that history calls for:

    consumers   "the only consumer" of the billing audience: Hooli's webhooks broke too (CS-2)
    jwks        fetched once at startup: the OPS-8 staging SEV-2 (WEB-3 refreshes every 10 minutes);
                and calling auth-service directly, when the JWKS is routed through api-gateway
    claims      top-level tenant_id and roles: SBX-4 namespaced them
    lookup      300 calls a minute on one key: rate limiter v2 allows 100 by default (Globex, CS-3)
    s2s         a new /oauth/token client: SBX-5 is deprecating it
    infra       Terraform applied from a laptop: the conventions say CI only
    rollout     production the week of Dec 14: the auth cutover (Dec 15), the rotation, the shim's users
and the ones that do not must stay quiet (the header, a rejected alternative, the review note);
nothing that agrees with a decision may be called a conflict (rejecting a permanent shim, keeping
the audit trail in auth-service as SBX-20 does), and the two open questions are answered with
citations. Other paragraphs may get notes or not: a fair reviewer comment is not noise.

The SOC2 auditor questions (the first demo), answered as two people who can read different things:

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

import json
import os
import re
import sys
from pathlib import Path

from docs_addin.answer import answer, context, model_from_env
from docs_addin.retrieve import directory, gather, paragraph_query
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


# -- the design doc, paragraph by paragraph ------------------------------------------------------ #

DOC = json.loads((Path(__file__).parent.parent / "docs_addin" / "demo_doc.json").read_text(encoding="utf-8"))
CONTEXT_CHECKS = []


def ccheck(name: str):
    def register(fn):
        CONTEXT_CHECKS.append((name, fn))
        return fn
    return register


def paragraphs() -> dict[str, tuple[str | None, str]]:
    """key -> (section, text) for the doc's keyed paragraphs, in order."""
    out, section = {}, None
    for b in DOC["blocks"]:
        if b["style"] in ("title", "h2", "h3"):     # in Docs the title is a Heading 1, so a section
            section = b["text"]
        elif b.get("key"):
            out[b["key"]] = (section, b["text"])
    return out


def notes(out: dict, *kinds: str) -> list[dict]:
    return [n for n in out["notes"] if not kinds or n["kind"] in kinds]


def note_says(out: dict, kinds: tuple[str, ...], *patterns: str) -> bool:
    """Some note of these kinds mentions every pattern (in its title or text)."""
    return any(all(re.search(p, n["title"] + " " + n["text"], re.I) for p in patterns) for n in notes(out, *kinds))


def shown(out: dict) -> str:
    return " | ".join(f"{n['kind']}: {n['title']}" for n in out["notes"]) or "no notes"


@ccheck("consumers: 'the only consumer' misses Hooli, whose webhooks broke on the same change (CS-2)")
def _(c):
    out = c["consumers"]
    ok = note_says(out, ("conflict", "incident"), r"Hooli|CS-2") and cites(out, r"CS-2\b", r"Hooli")
    return ok, f"{shown(out)}; cited {cited(out)}"


@ccheck("jwks: fetching once at startup is the OPS-8 incident; refresh every 10 minutes")
def _(c):
    out = c["jwks"]
    ok = note_says(out, ("incident", "conflict"), r"refresh|10 min|rotat") and cites(
        out, r"Postmortem", r"OPS-8\b", r"SEV-2", r"WEB-3\b", r"JWKS for 10 minutes", r"#incidents")
    return ok, f"{shown(out)}; cited {cited(out)}"


@ccheck("jwks: going around api-gateway contradicts routing the JWKS through it")
def _(c):
    out = c["jwks"]
    return note_says(out, (), r"gateway"), shown(out)


@ccheck("claims: top-level tenant_id and roles contradict SBX-4's namespaced claims")
def _(c):
    out = c["claims"]
    ok = note_says(out, ("conflict", "decided"), r"namespac") and cites(
        out, r"SBX-4\b", r"Auth OIDC Migration - Design Doc", r"Namespace custom claims", r"custom claims")
    return ok, f"{shown(out)}; cited {cited(out)}"


@ccheck("lookup: 300 calls a minute on one key runs into rate limiter v2 (100 a minute by default)")
def _(c):
    out = c["lookup"]
    ok = note_says(out, ("conflict", "incident"), r"rate|429|100") and cites(
        out, r"Rate Limiting", r"SBX-9\b", r"CS-3\b", r"429")
    return ok, f"{shown(out)}; cited {cited(out)}"


@ccheck("s2s: a new /oauth/token client conflicts with SBX-5's deprecation")
def _(c):
    out = c["s2s"]
    ok = note_says(out, ("conflict",), r"deprecat|sunset|remov") and cites(out, r"SBX-5\b", r"Deprecate /oauth/token")
    return ok, f"{shown(out)}; cited {cited(out)}"


@ccheck("infra: applying Terraform from a laptop breaks the apply-from-CI convention")
def _(c):
    out = c["infra"]
    ok = note_says(out, ("conflict", "decided"), r"\bCI\b|laptop") and cites(out, r"Terraform Conventions")
    return ok, f"{shown(out)}; cited {cited(out)}"


@ccheck("security: keeping the audit trail in auth-service, where SBX-20 puts it, is not a conflict")
def _(c):
    out = c["security"]
    return not notes(out, "conflict"), shown(out)


@ccheck("rollout: the week of Dec 14 runs into the cutover, the rotation, or the shim's other users")
def _(c):
    out = c["rollout"]
    return note_says(out, (), r"Dec(ember)? 15|cutover|rotat|Hooli|payments"), shown(out)


@ccheck("quiet: the doc's header, a rejected alternative and the review note get no notes")
def _(c):
    loud = {k: shown(c[k]) for k in ("meta", "alt_payload", "neutral") if c[k]["notes"]}
    return not loud, str(loud)


@ccheck("agree: rejecting a permanent shim, as the OIDC design did, is not called a conflict")
def _(c):
    return not notes(c["alt_shim"], "conflict"), shown(c["alt_shim"])


@ccheck("every note cites a source, and none cites a draft, a copy or the 2025 overview")
def _(c):
    c = {k: out for k, out in c.items() if "notes" in out}
    bad = [f"{k}: {n['title']}" for k, out in c.items() for n in out["notes"] if not re.search(r"\[\d+\]", n["text"])]
    bad += [f"{k}: {s['label'][:40]}" for k, out in c.items() for s in out["sources"]
            if any(re.search(p, s["title"]) for p in NEVER_CITED)]
    return not bad, "; ".join(bad)


@ccheck("no he/she in notes")
def _(c):
    bad = [k for k, out in c.items() for n in out.get("notes", []) if re.search(r"\b(he|she|his|her|him)\b", n["text"], re.I)]
    return not bad, ", ".join(bad)


@ccheck("open question: an audit record points at the token-issuance audit log (SBX-20)")
def _(c):
    out = c["q_audit"]
    return out["status"] != "none" and cites(out, r"SBX-20\b", r"Audit Log for Token Issuance", r"audit log"), \
        f"{out['status']}; cited {cited(out)}"


@ccheck("open question: billing-service belongs to the payments team")
def _(c):
    out = c["q_owner"]
    return out["status"] != "none" and says(out, r"payments"), out["answer"][:200]


def run_checks(title: str, checks, results: dict, viewers) -> tuple[int, int]:
    print(f"\n{title}")
    failed = total = 0
    for name, *rest in checks:
        fn, who_list = (rest[1], rest[0]) if len(rest) == 2 else (rest[0], viewers)
        for v in who_list:
            total += 1
            try:
                ok, detail = fn(results[v], v) if len(rest) == 2 else fn(results[v])
            except Exception as e:
                ok, detail = False, f"{type(e).__name__}: {e}"
            failed += not ok
            who = v.split(".")[0]
            print(f"  {'PASS' if ok else 'FAIL'}  [{who}] {name}" + (f"\n        {detail}" if detail and not ok else ""))
    return failed, total


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    ns = os.environ.get("ADDIN_NS", "kg:sandbox")
    store = Neo4jStore()
    model = model_from_env()
    people = directory(store, ns)
    tokens = [0, 0]

    def tally(out):
        tokens[0] += out["tokens"]["input"]
        tokens[1] += out["tokens"]["output"]
        return out

    # The design doc, as Ishan writes it: notes for each paragraph, answers for the open questions.
    design: dict[str, dict] = {}
    for key, (section, text) in paragraphs().items():
        if key.startswith("q_"):
            design[key] = tally(answer(model, text, gather(store, ns, ISHAN, text), people, today=TODAY))
        else:
            ctx = gather(store, ns, ISHAN, paragraph_query(text, section))
            design[key] = tally(context(model, text, section, DOC["title"], ctx, people, today=TODAY))

    answers = {}
    for v in VIEWERS:
        answers[v] = {key: tally(answer(model, q, gather(store, ns, v, q), people, today=TODAY))
                      for key, q in QUESTIONS.items()}
    store.close()

    print("design doc, as ishan")
    for key, out in design.items():
        if "notes" in out:
            print(f"  {key:10} {shown(out)}")
        else:
            print(f"  {key:10} {out['status']}: {out['answer'][:90]}")
    for v in VIEWERS:
        print(f"\nSOC2, as {v}")
        for key, out in answers[v].items():
            print(f"  {key:12} {out['status']:8} {len(out['sources']):>2}/{out['read']:<2} cited, "
                  f"{out['locked']['count']} locked, ask {(out['ask'] or {}).get('name', '-')}")
    print(f"\n{model.name}, {tokens[0]:,} input and {tokens[1]:,} output tokens (zero when replayed)")

    f1, t1 = run_checks("design doc", CONTEXT_CHECKS, {ISHAN: design}, (ISHAN,))
    f2, t2 = run_checks("SOC2 questions", CHECKS, answers, VIEWERS)
    print(f"\n{t1 + t2 - f1 - f2}/{t1 + t2} passed")
    return 1 if f1 + f2 else 0


if __name__ == "__main__":
    sys.exit(main())
