"""The swarm reader and builder: Karthik's sample run end to end, and the reading rules and access
cases it does not exercise, on hand-made runs. No database needed.

    python -m graph.test_build_swarm
"""
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.stdout.reconfigure(encoding="utf-8")

from graph.build_swarm import build  # noqa: E402
from graph.contract import EdgeProposal, NodeProposal  # noqa: E402
from graph.resolve import resolve_people  # noqa: E402
from graph.swarm_records import LocalStore, load  # noqa: E402

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


def has(t, s, d):
    return any(e.type == t and e.src_id == s and e.dst_id == d for e in edges)


def one(t, s=None, d=None):
    return next(e for e in edges if e.type == t and s in (None, e.src_id) and d in (None, e.dst_id))


check("runs applied in order, identity last", state.runs[-1].endswith("-identity"), state.runs)
check("ten live items (six from GitHub and Slack, four from Jira)", len(state.items) == 11, len(state.items))
check("tombstone and acl_update for never-ingested ids are no-ops (rule 4)",
      state.counts["tombstone for an unknown id (no-op)"] == 1 and state.counts["acl_update for an unknown id (no-op)"] == 1)
check("nothing the door would refuse", not any(k.startswith("door would refuse") for k in report["dropped"]),
      dict(report["dropped"]))
pr, issue = "github:repo:7001:pr:412", "github:repo:7001:issue:398"
thread = "slack:T01ACME:thread:C42AUTH:1760450400.000100"
check("PR closes the issue", has("CLOSES", pr, issue))
check("commit implements the issue",
      has("IMPLEMENTS", "github:repo:7001:commit:3f9c2e1a7b4d5f60718293a4b5c6d7e8f9012345", issue))
check("the thread's ref to the PR resolves across sources", has("REFERENCES", thread, pr))
check("ref to a Drive file not ingested is kept as pending",
      "references>gdrive:file:1AbCdEfGh" in nodes[pr].props.get("pendingRefs", []))
check("items sit in their containers", has("IN", pr, "github:repo:7001")
      and has("IN", thread, "slack:T01ACME:channel:C42AUTH"))
check("the source's own update time is sourceUpdatedAt", "sourceUpdatedAt" in nodes[pr].props
      and "updatedAt" not in nodes[pr].props)
check("Priya reviewed the PR", has("REVIEWED", "person:github:priya-r", pr))
check("Sam's Slack and GitHub accounts are one person",
      has("HAS_ACCOUNT", "person:c:7f3a91", "person:slack:U123SAM") and has("HAS_ACCOUNT", "person:c:7f3a91", "person:github:slee"))
check("Priya's GitHub account follows her merged cluster to the survivor (rule 6)",
      has("HAS_ACCOUNT", "person:c:b20c44", "person:github:priya-r") and "person:c:e81d20" not in nodes)
check("the LLM-matched cluster keeps its 0.82",
      one("HAS_ACCOUNT", d="person:github:priya-r").provenance.confidence == 0.82)
check("people carry isBot", nodes["person:c:7f3a91"].props.get("isBot") is False)
check("restricted items carry their group", nodes[pr].visibility == "restricted"
      and nodes[pr].acl_groups == ["group:github:repo:7001:readers"])
check("group members become MEMBER_OF from the person", has("MEMBER_OF", "person:c:7f3a91",
      "group:slack:T01ACME:channel:C42AUTH:members"))

check("the re-extraction under the newer prompt is the one used (rule 9)",
      one("DECIDED_IN").props["extractor"] == "hand-written:sample-v1", one("DECIDED_IN").props["extractor"])
decision = next(i for i in nodes if i.startswith("entity:decision:use-oidc-instead-of-saml-for-staff-login@"))
project = next(i for i in nodes if i.startswith("entity:project:auth-migration@"))
check("the decision is a node, decided in the thread, by Sam",
      has("DECIDED_IN", decision, thread) and has("DECIDED_BY", decision, "person:slack:U123SAM"))
check("the decision is part of the project", has("PART_OF", decision, project))
check("a resolved person id is the Person, followed through merged_into, not a new Account",
      has("WORKS_ON", "person:c:b20c44", project)
      and not any(i.startswith("person:c:") and n.type == "Account" for i, n in nodes.items())
      and not any(i.startswith("person:ours:") and "person:c:" in i for i in nodes),
      [i for i in nodes if "person:" in i])
check("an extracted entity is only as visible as its source", nodes[decision].visibility == "restricted"
      and nodes[decision].acl_groups == ["group:slack:T01ACME:channel:C42AUTH:members"])
check("no stored mention count on entities", "mentionedIn" not in nodes[decision].props)
check("extracted edges carry their source's access",
      one("DECIDED_BY").props.get("visibility") == "restricted"
      and one("DECIDED_BY").props.get("aclGroups") == ["group:slack:T01ACME:channel:C42AUTH:members"])
check("every evidence quote was at its offsets", report["dropped"]["evidence quote not at its offsets"] == 0)
check("extracted confidence is the model's own score, not scaled by source",
      one("DECIDED_BY").provenance.confidence == 0.8 and one("DECIDED_BY").props["sourceAuthority"] == 0.7,
      one("DECIDED_BY").provenance.confidence)
author = one("AUTHORED", d=pr)
check("role edges carry the role's weight, not faded by age", author.props["weight"] == 1.0, author.props)


# -- reading rules and access cases the sample does not reach ---------------------------------- #
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


def extraction(iid, version, text, name, attributes, decided_by=None):
    quote_at = text.index(name)
    ev = [{"start": quote_at, "end": quote_at + len(name), "quote": name}]
    rels = [{"type": "decided_by", "source": "e1", "target": decided_by, "confidence": 0.9, "evidence": ev}] \
        if decided_by else []
    return {"record_type": "extraction", "item_id": iid, "item_version": version, "source": "gdrive",
            "extractor": {"model": "m", "prompt_version": "p"}, "relations": rels,
            "entities": [{"local_id": "e1", "type": "decision", "name": name, "attributes": attributes,
                          "evidence": ev}]}


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

print("access")
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    public_text, private_text = "We chose Kafka for events.", "We chose Kafka for events. Acme pays for it."
    private = ("restricted", ["group:g:inner"])
    run(root, "gdrive", "2026-10-20T03:00:00Z-gdrive", [
        item("pub", "2026-10-20T01:00:00Z", "2026-10-20T03:01:00Z", public_text, ("public", [])),
        item("priv", "2026-10-20T01:00:00Z", "2026-10-20T03:01:00Z", private_text, private),
        extraction("pub", "2026-10-20T01:00:00Z", public_text, "Kafka", {}),
        extraction("priv", "2026-10-20T01:00:00Z", private_text, "Kafka", {"customer": "Acme"},
                   decided_by="person:gdrive:boss"),
        {"record_type": "group", "id": "group:g:outer", "source": "gdrive", "name": "outer",
         "members": ["group:g:inner"], "observed_at": "2026-10-20T03:00:00Z"},
        {"record_type": "group", "id": "group:g:inner", "source": "gdrive", "name": "inner",
         "members": ["user:a@x.com"], "observed_at": "2026-10-20T03:00:00Z"}])
    s = load(LocalStore(root))
    props, _ = build(s, "kg:test")
    n = {p.id: p for p in props if isinstance(p, NodeProposal)}
    e = [p for p in props if isinstance(p, EdgeProposal)]
    kafka = sorted(i for i in n if i.startswith("entity:decision:kafka@"))
    pub_node = n.get("entity:decision:kafka@public")
    check("one entity node per access set", len(kafka) == 2, kafka)
    check("the public node carries nothing from the private item",
          pub_node is not None and pub_node.visibility == "public" and "Acme" not in json.dumps(pub_node.props),
          pub_node and pub_node.props)
    dby = next(x for x in e if x.type == "DECIDED_BY")
    check("DECIDED_BY read from the private item is private, on the private node",
          dby.props["visibility"] == "restricted" and dby.src_id != "entity:decision:kafka@public")
    check("a group inside a group is MEMBER_OF its parent",
          any(x.type == "MEMBER_OF" and x.src_id == "group:g:inner" and x.dst_id == "group:g:outer" for x in e))

print("people")
people, links, review = resolve_people([
    {"source": "slack", "handle": "u1", "name": "John Smith", "email": "john@a.com"},
    {"source": "jira", "handle": "j9", "name": "John Smith", "email": None},
    {"source": "github", "handle": "jsmith", "name": "John Smith", "email": "john@a.com"},
    {"source": "github", "handle": "dependabot[bot]", "name": "dependabot[bot]", "email": None}])
check("same email merges", len({link["personId"] for link in links if link["account"]["email"] == "john@a.com"}) == 1)
check("same name alone does not merge; it becomes a review item",
      len(people) == 3 and any(r["rule"] == "same-normalised-name" for r in review), (people, review))
check("bots are flagged", any(p["isBot"] for p in people) and sum(p["isBot"] for p in people) == 1)

print("\nALL PASS" if ok else "\nFAILURES")
sys.exit(0 if ok else 1)
