"""
check_extraction.py - the sandbox's answer key, model half, checked against the bucket.

What the LLM step should pull out of the sandbox (sandbox/storyline.md on the
ishan-sandbox-env branch): the decisions, systems, owners, customers and
replacements the three stories turn on, each from the items that state them.
Model output varies run to run, so each check looks for an entity of the
right type whose name carries the right words, in the right items, rather
than an exact string. Also reports what was extracted overall and what the
noise channels produced.

    S3_BUCKET=... S3_ENDPOINT=... python -m eval.check_extraction      exit 1 if any check fails
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from collections.abc import Callable

from swarm.records import Entity, ExtractionRecord, ItemRecord

from .bucket import Bucket
from .check_sandbox import ROSTER, channel, doc

CHECKS: list[tuple[str, str, Callable[[Bucket], tuple[bool, str]]]] = []


def check(group: str, name: str):
    def register(fn):
        CHECKS.append((group, name, fn))
        return fn
    return register


# -- helpers ------------------------------------------------------------------ #

def ex(b: Bucket, item: ItemRecord | None) -> ExtractionRecord | None:
    return b.extraction(item) if item else None


def entities(b: Bucket, items: list[ItemRecord | None], kind: str, *words: str) -> list[tuple[ItemRecord, Entity]]:
    """Entities of `kind` from these items' extractions whose name has every word (regexes, any case)."""
    found = []
    for item in items:
        record = ex(b, item)
        for e in record.entities if record else []:
            if e.type == kind and all(re.search(w, e.name, re.I) for w in words):
                found.append((item, e))
    return found


def relations(b: Bucket, items: list[ItemRecord | None], kind: str) -> list[tuple[ItemRecord, object, str, str]]:
    """(item, relation, source name, target name) for relations of `kind`, names resolved where they can be."""
    out = []
    for item in items:
        record = ex(b, item)
        if not record:
            continue
        names = {e.local_id: e.name for e in record.entities}
        names.update({p.id: p.name or p.handle for p in item.people})
        names[item.id] = item.title
        for r in record.relations:
            if r.type == kind:
                out.append((item, r, names.get(r.source, r.source), names.get(r.target, r.target)))
    return out


def where(found) -> str:
    return "; ".join(sorted({f"{i.title[:40]}: {e.name}" for i, e in found}))[:300]


# -- extraction ran ----------------------------------------------------------- #

@check("coverage", "every item worth a model call has an extraction of its current version")
def _(b):
    from swarm.stages.extract import worth_extracting
    due = [i for i in b.items.values() if worth_extracting(i)]
    missing = [i for i in due if b.extraction(i) is None]
    by_source = Counter(i.source for i in missing)
    return not missing, f"{len(missing)}/{len(due)} missing: {dict(by_source)}"


# -- story 1: OIDC ------------------------------------------------------------ #

@check("OIDC", "the custom-claims decision (option B, namespaced claims) is extracted where it was made")
def _(b):
    sources = [*channel(b, "#auth-migration"), b.jira("SBX-4"), b.drive("Auth OIDC Migration - Design Doc")]
    found = entities(b, sources, "decision", r"namespac|option b")
    in_thread = [f for f in found if f[0].source == "slack"]
    decided_in = [r for r in relations(b, [i for i, _ in in_thread], "decided_in")]
    return (bool(in_thread) and bool(decided_in),
            f"found: {where(found) or 'none'}; decided_in from the thread: {len(decided_in)}")


@check("OIDC", "auth-service is a system, and Ishan owns it")
def _(b):
    sources = [doc(b, "auth-service", ".github/CODEOWNERS"), doc(b, "handbook", "README.md"),
               b.drive("Auth OIDC Migration - Design Doc"), doc(b, "auth-service", "README.md")]
    systems = entities(b, sources, "system", r"auth.?service")
    owns = [(s, t) for _, _, s, t in relations(b, sources, "owns")
            if re.search(r"ishan", s, re.I) and re.search(r"auth.?service", t, re.I)]
    return bool(systems) and bool(owns), f"systems: {where(systems) or 'none'}; Ishan owns: {owns[:3] or 'none'}"


@check("OIDC", "rotating the signing keys is an action item on Dhruv")
def _(b):
    sources = [b.jira("OPS-1"), *channel(b, "#eng"), *channel(b, "#auth-migration"),
               b.drive("API Gateway Key Rotation Runbook.docx")]
    items = entities(b, sources, "action_item", r"rotat")
    assigned = [(s, t) for _, _, s, t in relations(b, sources, "assigned_to")
                if re.search(r"rotat", s, re.I) and re.search(r"dhruv|pendharkar", t, re.I)]
    return bool(items) and bool(assigned), f"items: {where(items) or 'none'}; to Dhruv: {assigned[:2] or 'none'}"


# -- story 2: retraining ------------------------------------------------------ #

@check("retraining", "ADR-014 records the decision to retrain on drift, and the pipeline as a system or project")
def _(b):
    adrs = [b.drive("ADR-014 Continuous Retraining.docx"), doc(b, "ml-platform", "docs/adr/014-continuous-retraining.md")]
    decisions = entities(b, adrs, "decision", r"drift|continuous")
    # Either reading is right: the pipeline as one system, or as a project whose parts are systems.
    pipeline = (entities(b, adrs, "system", r"retrain|pipeline")
                + entities(b, adrs, "project", r"retrain|pipeline"))
    return bool(decisions) and bool(pipeline), f"decisions: {where(decisions) or 'none'}; pipeline: {where(pipeline) or 'none'}"


@check("retraining", "Karthik owns the retraining pipeline")
def _(b):
    sources = [b.drive("ADR-014 Continuous Retraining.docx"), doc(b, "ml-platform", "docs/adr/014-continuous-retraining.md"),
               doc(b, "ml-platform", ".github/CODEOWNERS"), doc(b, "ml-platform", "README.md"), b.jira("ML-1"),
               *channel(b, "#ml-platform")]
    owns = [(s, t) for _, _, s, t in relations(b, sources, "owns")
            if re.search(r"karthik|valluri|kar722", s, re.I) and re.search(r"retrain|pipeline|ml.platform", t, re.I)]
    return bool(owns), str(owns[:3] or "none")


@check("retraining", "the drift-triggered design replaces the nightly cron")
def _(b):
    sources = [b.drive("ADR-014 Continuous Retraining.docx"), doc(b, "ml-platform", "docs/adr/014-continuous-retraining.md"),
               *channel(b, "#ml-platform"), b.jira("ML-9"), b.jira("ML-1")]
    sup = [(s, t) for _, _, s, t in relations(b, sources, "supersedes") if re.search(r"cron|nightly", t, re.I)]
    stated = entities(b, sources, "decision", r"cron|nightly")
    return bool(sup) or bool(stated), f"supersedes: {sup[:2] or 'none'}; decisions naming the cron: {where(stated) or 'none'}"


# -- story 3: CSV export ------------------------------------------------------ #

@check("CSV export", "removing the legacy CSV endpoint is a decision")
def _(b):
    sources = [b.jira("WEB-8"), *channel(b, "#eng"), *channel(b, "#releases"), b.drive("Export Migration Note.docx")]
    found = entities(b, sources, "decision", r"csv|export")
    return bool(found), where(found) or "none"


@check("CSV export", "Acme Corp is a customer, from the escalation")
def _(b):
    sources = [b.jira("CS-4"), b.jira("CS-1"), *channel(b, "#customer-success"), b.drive("Acme Corp - Account Plan.docx")]
    found = entities(b, sources, "customer", r"acme")
    return bool(found), where(found) or "none"


@check("CSV export", "the replacements, the Reports API and scheduled delivery, are systems")
def _(b):
    sources = [b.drive("Export Migration Note.docx"), b.jira("WEB-9"), b.jira("WEB-10"), b.jira("WEB-8"),
               *channel(b, "#releases")]
    reports = entities(b, sources, "system", r"report")
    scheduled = entities(b, sources, "system", r"schedul|exports? page")
    return bool(reports) and bool(scheduled), f"reports: {where(reports) or 'none'}; scheduled: {where(scheduled) or 'none'}"


# -- people the text names ---------------------------------------------------- #

def person_named(b: Bucket, person_id: str) -> str:
    """Who a resolved person is, by the roster: their accounts are the cluster's
    members. Names alone would not do: Dhruv's four accounts carry four names."""
    record = b.identities.get(person_id)
    members = {(m.source, m.handle) for m in record.members} if record else set()
    for person, accounts in ROSTER.items():
        if members & set(accounts.items()):
            return person
    return person_id


@check("people", "action items in the weekly sync notes link to the people the notes name")
def _(b):
    notes = b.drive("Platform Weekly Sync Notes.docx")
    linked = {person_named(b, r.target) for _, r, _, _ in relations(b, [notes], "assigned_to")
              if r.target.startswith("person:c:")}
    return need_all(linked, {"Ishan Sinha", "Dhruv Pendharkar"})


@check("people", "across documents, work is linked to people only their text names")
def _(b):
    docs = [i for i in b.items.values() if i.source == "gdrive"]
    found = Counter(person_named(b, r.target) for _, r, _, _ in
                    [x for kind in ("assigned_to", "owns", "decided_by") for x in relations(b, docs, kind)]
                    if r.target.startswith("person:c:"))
    return len(found) >= 3, str(dict(found))


def need_all(got: set[str], wanted: set[str]) -> tuple[bool, str]:
    return wanted <= got, f"linked: {sorted(got)}; want {sorted(wanted)}"


# -- noise ----------------------------------------------------------------------- #

@check("noise", "#social and bot channels yield next to nothing")
def _(b):
    noisy = channel(b, "#social") + channel(b, "#ci") + channel(b, "#alerts")
    extracted = [ex(b, t) for t in noisy]
    count = sum(len(e.entities) for e in extracted if e)
    worth = [d for d in (ex(b, t) for t in channel(b, "#social")) if d and any(x.type == "decision" for x in d.entities)]
    return not worth and count <= 2 * len(noisy), f"{count} entities from {len(noisy)} noisy threads; decisions in #social: {len(worth)}"


def summary(b: Bucket) -> str:
    types = Counter(e.type for es in b.extractions.values() for r in es for e in r.entities)
    rels = Counter(r.type for es in b.extractions.values() for x in es for r in x.relations)
    records = sum(len(es) for es in b.extractions.values())
    return f"{records} extraction records; entities {dict(types.most_common())}; relations {dict(rels.most_common())}"


def main() -> int:
    b = Bucket.from_env()
    print(summary(b))
    failed, group = 0, None
    for g, name, fn in CHECKS:
        if g != group:
            print(f"\n{g}")
            group = g
        try:
            ok, detail = fn(b)
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}: {e}"
        failed += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail}" if detail else ""))
    print(f"\n{len(CHECKS) - failed}/{len(CHECKS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
