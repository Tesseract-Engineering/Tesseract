"""The swarm reader and builder: Karthik's sample run end to end, and the reading rules it does not
exercise, on hand-made runs. No database needed.

    python graph/test_build_swarm.py
"""
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.stdout.reconfigure(encoding="utf-8")

from build_swarm import build
from contract import EdgeProposal, NodeProposal
from swarm_records import LocalStore, load

ok = True


def check(name, condition, detail=""):
    global ok
    ok = ok and condition
    print(f"  {'PASS' if condition else 'FAIL'}  {name}{('  ' + str(detail)) if detail else ''}")


# -- Karthik's samples/run-0 ------------------------------------------------------------------ #
print("samples/run-0")
state = load(LocalStore(HERE.parent / "samples" / "run-0"))
proposals, report = build(state, "kg:test")
nodes = {p.id: p for p in proposals if isinstance(p, NodeProposal)}
edges = [p for p in proposals if isinstance(p, EdgeProposal)]
has = lambda t, s, d: any(e.type == t and e.src_id == s and e.dst_id == d for e in edges)

check("three runs applied, identity last", state.runs[-1].endswith("-identity"), state.runs)
check("ten live items (six from GitHub and Slack, four from Jira)", len(state.items) == 10, len(state.items))
check("tombstone and acl_update for never-ingested ids are no-ops (rule 4)",
      state.counts["tombstone for an unknown id (no-op)"] == 1 and state.counts["acl_update for an unknown id (no-op)"] == 1)
check("nothing the door would refuse", not any(k.startswith("door would refuse") for k in report["dropped"]),
      dict(report["dropped"]))
pr, issue = "github:repo:7001:pr:412", "github:repo:7001:issue:398"
check("PR closes the issue", has("CLOSES", pr, issue))
check("commit implements the issue",
      has("IMPLEMENTS", "github:repo:7001:commit:3f9c2e1a7b4d5f60718293a4b5c6d7e8f9012345", issue))
check("the thread's ref to the PR resolves across sources",
      has("REFERENCES", "slack:T01ACME:thread:C42AUTH:1760450400.000100", pr))
check("ref to a Drive file not ingested is kept as pending",
      "references>gdrive:file:1AbCdEfGh" in nodes[pr].props.get("pendingRefs", []))
check("items sit in their containers", has("IN", pr, "github:repo:7001")
      and has("IN", "slack:T01ACME:thread:C42AUTH:1760450400.000100", "slack:T01ACME:channel:C42AUTH"))
check("Priya reviewed the PR", has("REVIEWED", "person:github:priya-r", pr))
check("Sam's Slack and GitHub accounts are one person",
      has("HAS_ACCOUNT", "person:c:7f3a91", "person:slack:U123SAM") and has("HAS_ACCOUNT", "person:c:7f3a91", "person:github:slee"))
check("the LLM-matched cluster keeps its 0.82", next(e.provenance.confidence for e in edges
      if e.type == "HAS_ACCOUNT" and e.dst_id == "person:github:priya-r") == 0.82)
check("restricted items carry their group", nodes[pr].visibility == "restricted"
      and nodes[pr].acl_groups == ["group:github:repo:7001:readers"])
check("group members become MEMBER_OF from the person", has("MEMBER_OF", "person:c:7f3a91",
      "group:slack:T01ACME:channel:C42AUTH:members"))
decision = "entity:decision:use-oidc-instead-of-saml-for-staff-login"
check("the decision is a node, decided in the thread, by Sam",
      decision in nodes and has("DECIDED_IN", decision, "slack:T01ACME:thread:C42AUTH:1760450400.000100")
      and has("DECIDED_BY", decision, "person:slack:U123SAM"))
check("the decision is part of the project", has("PART_OF", decision, "entity:project:auth-migration"))
check("an extracted entity is only as visible as its source", nodes[decision].visibility == "restricted"
      and nodes[decision].acl_groups == ["group:slack:T01ACME:channel:C42AUTH:members"])
check("every evidence quote was at its offsets", report["dropped"]["evidence quote not at its offsets"] == 0)
dec_by = next(e for e in edges if e.type == "DECIDED_BY")
check("extracted confidence = model 0.8 x slack authority 0.7", dec_by.provenance.confidence == 0.56,
      dec_by.provenance.confidence)
author = next(e for e in edges if e.type == "AUTHORED" and e.dst_id == pr)
check("role edges carry strength and a decayed weight", author.props["strength"] == 1.0
      and 0 < author.props["weight"] <= 1.0, author.props)


# -- reading rules the sample does not reach --------------------------------------------------- #
def run(root: Path, source: str, rid: str, records: list[dict], complete: bool = True) -> None:
    folder = root / "runs" / source / rid.replace(":", "-")
    (folder / "items").mkdir(parents=True)
    (folder / "items" / "part-00001.jsonl").write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    manifest = {"schema_version": "2", "source": source, "run_id": rid, "files": ["items/part-00001.jsonl"],
                "status": "complete" if complete else "running", "finished_at": rid[:20]}
    (folder / "_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def item(iid, updated, fetched, text="t", acl=("internal", [])):
    return {"schema_version": "2", "record_type": "item", "id": iid, "kind": "document", "source": "gdrive",
            "container": None, "title": iid, "text": text, "updated_at": updated, "version": updated,
            "fetched_at": fetched, "people": [], "refs": [], "meta": {},
            "acl": {"visibility": acl[0], "principals": acl[1]}}


print("reading rules")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    run(root, "gdrive", "2026-10-21T03:00:00Z-gdrive", [item("d1", "2026-10-21T01:00:00Z", "2026-10-21T03:01:00Z", "new")])
    run(root, "gdrive", "2026-10-20T03:00:00Z-gdrive", [item("d1", "2026-10-19T01:00:00Z", "2026-10-20T03:01:00Z", "old")])
    run(root, "gdrive", "2026-10-22T03:00:00Z-gdrive", [
        {"record_type": "acl_update", "id": "d1", "source": "gdrive", "observed_at": "2026-10-22T03:02:00Z",
         "acl": {"visibility": "restricted", "principals": ["user:a@x.com"]}}])
    run(root, "gdrive", "2026-10-23T03:00:00Z-gdrive", [item("d2", "2026-10-23T01:00:00Z", "2026-10-23T03:01:00Z")],
        complete=False)
    run(root, "gdrive", "2026-10-24T03:00:00Z-gdrive", [
        item("d3", "2026-10-24T01:00:00Z", "2026-10-24T03:01:00Z"),
        {"record_type": "extraction", "item_id": "d3", "item_version": "2026-10-24T01:00:00Z", "source": "gdrive",
         "extractor": {"model": "m", "prompt_version": "p"}, "entities": [], "relations": []},
        {"record_type": "tombstone", "id": "d3", "source": "gdrive", "reason": "deleted",
         "detected_at": "2026-10-24T03:05:00Z"}])
    s = load(LocalStore(root))
    check("newest content wins, whatever order the runs arrive in (rule 2)", s.items["d1"]["text"] == "new")
    check("a later acl_update replaces the item's ACL (rule 3)", s.items["d1"]["acl"]["visibility"] == "restricted")
    check("a run without a complete manifest is not read (rule 1)", "d2" not in s.items and len(s.skipped_runs) == 1)
    check("a tombstone retires the item and its extraction (rule 5)", "d3" not in s.items and "d3" not in s.extractions)

print("\nALL PASS" if ok else "\nFAILURES")
sys.exit(0 if ok else 1)
