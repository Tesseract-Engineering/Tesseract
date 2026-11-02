"""
build_swarm.py - build the company graph from the swarm's runs (record schema v2), through the door.

    swarm_records.load()   reading rules 1-8 -> the current state: live items, ACLs, groups,
                           identity clusters, extractions
    skeleton               items, containers, accounts; role edges, refs, IN - exact, from fields
    people                 the swarm's identity clusters; for identities no cluster lists, our
                           resolver merges on exact email only and writes anything weaker down as
                           a ReviewItem (a wrong merge would hand one person the other's access)
    meaning                extracted entities merged across items by type, name and access set,
                           with quotes; extracted relations with the model's own confidence, each
                           edge carrying the access of the items it was read from
    the door               namespaced, validated, receipted writes (contract.py, ingest.py)

What is parsed and what is inferred stays apart, as everywhere else: a role or a ref is a field
(producer swarm:field / swarm:ref:<via>), an extracted relation is a model's reading of prose
(producer swarm:llm:<model>:<prompt>) and carries its quotes. Weights come from weights.py.

Usage (from the repo root):
    python -m graph.build_swarm --root samples/run-0 --dry-run
    ... -m graph.build_swarm --root <folder holding runs/>      # writes namespace kg:swarm
    ... -m graph.build_swarm --root s3 --env .env               # the bucket itself
    docker compose -f swarm/compose.yaml run --rm graph         # the same, inside the stack
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from . import weights
from .contract import EDGE_TYPES, EdgeProposal, NodeProposal, Provenance, validate
from .resolve import looks_like_bot, resolve_people
from .swarm_records import State, current_extractions, load, open_store, resolve_person

TYPE_OF_KIND = {"repo": "Container", "channel": "Container", "folder": "Container", "project": "Container",
                "pull_request": "Issue", "issue": "Issue", "commit": "Commit",
                "thread": "Document", "document": "Document"}
ENTITY_LABEL = {"decision": "Decision", "project": "Project", "system": "System", "team": "Team",
                "action_item": "ActionItem", "customer": "Customer"}
REF_EDGE = {"closes": "CLOSES", "implements": "IMPLEMENTS", "references": "REFERENCES",
            "blocks": "BLOCKS", "depends_on": "DEPENDS_ON", "part_of": "PART_OF", "duplicates": "DUPLICATES"}
REL_EDGE = {"decided_in": "DECIDED_IN", "decided_by": "DECIDED_BY", "assigned_to": "ASSIGNED_TO",
            "part_of": "PART_OF", "works_on": "WORKS_ON", "owns": "OWNS", "member_of": "MEMBER_OF",
            "mentions": "MENTIONS", "supersedes": "SUPERSEDES", "depends_on": "DEPENDS_ON",
            "blocks": "BLOCKS"}
# Person role -> (edge type, direction). "out" is account -> item, "in" is item -> account.
ROLE_EDGE = {"author": ("AUTHORED", "out"), "reporter": ("AUTHORED", "out"),
             "reviewer": ("REVIEWED", "out"), "assignee": ("ASSIGNED_TO", "in"),
             "commenter": ("PARTICIPATED_IN", "out"), "participant": ("PARTICIPATED_IN", "out"),
             "mentioned": ("MENTIONS", "in"), "owner": ("OWNS", "out")}


def allowed(edge: str, src: str, dst: str) -> bool:
    srcs, dsts = EDGE_TYPES[edge]
    return src in srcs and dst in dsts


def when(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def clean(props: dict) -> dict:
    """Neo4j stores scalars and lists of scalars; anything else becomes JSON text."""
    out = {}
    for k, v in props.items():
        if v is None or v == [] or v == "":
            continue
        if isinstance(v, (str, int, float, bool, datetime)):
            out[k] = v
        elif isinstance(v, list) and all(isinstance(x, (str, int, float, bool)) for x in v):
            out[k] = v
        else:
            out[k] = json.dumps(v, sort_keys=True, default=str)
    return out


def item_props(item: dict) -> dict:
    meta = item.get("meta") or {}
    label = TYPE_OF_KIND[item["kind"]]
    props = {"title": item.get("title") or item["id"], "kind": item["kind"], "source": item["source"],
             "url": item.get("url"), "aliases": item.get("aliases"), "createdAt": when(item.get("created_at")),
             "sourceUpdatedAt": when(item.get("updated_at")), "version": item.get("version"),
             "contentHash": item.get("content_hash"), "runId": item.get("run_id"), "meta": meta}
    text_key = "body" if label in ("Issue", "Commit") else "text"   # what the codeSearch index reads
    props[text_key] = item.get("text")
    # Every source-specific fact becomes a property of its own (Jira status, PR state, message counts...):
    # snake_case to camelCase, *_at / *_date as real dates, anything nested kept as JSON text by clean().
    for key, value in meta.items():
        name = re.sub(r"_(\w)", lambda m: m.group(1).upper(), key)
        if name in props:
            continue
        props[name] = when(value) if key.endswith(("_at", "_date")) and isinstance(value, str) else value
    return clean(props)


def acl(item: dict) -> tuple[str, list[str]]:
    a = item.get("acl") or {}
    vis = a.get("visibility", "restricted")
    return vis, sorted(a.get("principals") or []) if vis == "restricted" else []


def acl_key(item: dict) -> str:
    """One short name per access set: 'public', 'internal', or a digest of the principals."""
    vis, principals = acl(item)
    if vis != "restricted":
        return vis
    return "r" + hashlib.sha256("|".join(principals).encode("utf-8")).hexdigest()[:10]


def build(state: State, ns: str) -> tuple[list, dict]:
    """State -> door proposals, plus a report of everything worth a human's attention."""
    report: dict = {"counts": Counter(), "dropped": Counter(), "pending_refs": []}
    proposals: list = []
    label_of = {iid: TYPE_OF_KIND[it["kind"]] for iid, it in state.items.items()}

    def prov(source: str, source_id: str, producer: str, confidence: float = 1.0, quote: str = "",
             observed: str | None = None) -> Provenance:
        return Provenance(source=source, source_id=source_id, producer=producer,
                          confidence=round(confidence, 4), quote=quote, observed_at=observed)

    def edge(etype, src, dst, stype, dtype, props, provenance):
        if not allowed(etype, stype, dtype):
            report["dropped"][f"{etype} {stype}->{dtype} not in the contract"] += 1
            return
        proposals.append(EdgeProposal(ns=ns, type=etype, src_id=src, dst_id=dst, src_type=stype,
                                      dst_type=dtype, props=clean(props), provenance=provenance))
        report["counts"][f"edge {etype}"] += 1

    # ---- accounts: every source identity anything mentions ------------------------------ #
    accounts: dict[str, dict] = {}

    def account(pid: str, **known) -> str:
        entry = accounts.setdefault(pid, {"id": pid, "source": pid.split(":")[1] if pid.count(":") >= 2 else "",
                                          "handle": pid.split(":", 2)[-1]})
        for k, v in known.items():
            if v and not entry.get(k):
                entry[k] = v
        return pid

    for it in state.items.values():
        for p in it.get("people", []):
            account(p["id"], name=p.get("name"), email=(p.get("email") or "").lower() or None)
    for cluster in state.clusters.values():
        for m in cluster.get("members", []):
            account(m["id"], name=m.get("name"), email=(m.get("email") or "").lower() or None)
    extractions, stale = current_extractions(state)     # rule 9
    for ex in extractions:                               # people named only in extracted relations
        for rel in ex.get("relations", []):
            for ref in (rel["source"], rel["target"]):
                if ref.startswith("person:") and not ref.startswith("person:c:"):
                    account(ref)

    # ---- items: nodes, containers, refs, roles ------------------------------------------ #
    pending: dict[str, list[str]] = defaultdict(list)
    item_nodes: dict[str, NodeProposal] = {}
    for iid, it in state.items.items():
        label = label_of[iid]
        vis, principals = acl(it)
        props = item_props(it)
        container = it.get("container")
        if container and container not in state.items:
            pending[iid].append(f"in>{container}")
        item_nodes[iid] = NodeProposal(ns=ns, type=label, id=iid, props=props, visibility=vis,
                                       acl_groups=principals,
                                       provenance=prov(it["source"], iid, f"swarm:{it['kind']}",
                                                       observed=it.get("updated_at")))
        proposals.append(item_nodes[iid])
        report["counts"][f"node {label} ({it['kind']})"] += 1
        if container and container in state.items:
            edge("IN", iid, container, label, label_of[container], {},
                 prov(it["source"], iid, "swarm:field:container", observed=it.get("updated_at")))
        for ref in it.get("refs", []):
            target, rtype = ref["target"], ref["type"]
            if target not in state.items:
                pending[iid].append(f"{rtype}>{target}")
                report["pending_refs"].append((iid, rtype, target))
                continue
            etype = REF_EDGE.get(rtype, "REFERENCES")
            if not allowed(etype, label, label_of[target]):
                etype = "REFERENCES"
            edge(etype, iid, target, label, label_of[target], {"relation": rtype, "via": ref.get("via")},
                 prov(it["source"], iid, f"swarm:ref:{ref.get('via')}", weights.ref_confidence(ref.get("via")),
                      observed=it.get("updated_at")))
        for p in it.get("people", []):
            for role in p.get("roles", []):
                etype, direction = ROLE_EDGE.get(role, ("PARTICIPATED_IN", "out"))
                src, dst, stype, dtype = ((p["id"], iid, "Account", label) if direction == "out"
                                          else (iid, p["id"], label, "Account"))
                if not allowed(etype, stype, dtype):     # e.g. an owner of a thread
                    etype, src, dst, stype, dtype = "PARTICIPATED_IN", p["id"], iid, "Account", label
                edge(etype, src, dst, stype, dtype, {"role": role, "weight": weights.role_weight(role)},
                     prov(it["source"], iid, "swarm:field:people", observed=it.get("updated_at")))
    for iid, refs in pending.items():   # rule 4: kept, resolved when the target arrives
        item_nodes[iid].props["pendingRefs"] = refs
    report["counts"]["pending refs"] = sum(len(v) for v in pending.values())

    # ---- people: identity clusters, then our resolver for identities no cluster lists -- #
    # rule 6: an identity listed only by a cluster that later merged away belongs to the survivor
    person_of = {aid: resolve_person(state, pid) for aid, pid in state.person_of.items()}
    person_of = {aid: pid for aid, pid in person_of.items() if pid in state.clusters}
    persons: dict[str, dict] = {}
    for pid, cluster in state.clusters.items():
        names = Counter(m.get("name") for m in cluster["members"] if m.get("name"))
        persons[pid] = {"name": names.most_common(1)[0][0] if names else pid,
                        "method": cluster.get("method"), "confidence": float(cluster.get("confidence", 1.0)),
                        "evidence": cluster.get("evidence"),
                        "isBot": any(looks_like_bot(m.get("handle") or "", m.get("name") or "")
                                     for m in cluster["members"])}
    loose = [a for a in accounts.values() if a["id"] not in person_of]
    if loose:
        ours = {(a["source"], a["handle"].lower()): a["id"] for a in loose}
        people, links, review = resolve_people([{"source": a["source"], "handle": a["handle"],
                                                 "name": a.get("name"), "email": a.get("email")} for a in loose])
        for row in people:
            persons[f"person:ours:{row['id']}"] = {"name": row["name"], "method": "graph-resolver",
                                                   "confidence": 1.0, "evidence": None, "isBot": row["isBot"]}
        for link in links:
            src = ours.get((link["account"]["source"], link["account"]["handle"]))
            if src:
                person_of[src] = f"person:ours:{link['personId']}"
        for item in review:     # too weak to merge: written down for a person to settle
            proposals.append(NodeProposal(ns=ns, type="ReviewItem", id=item["id"], props=clean(item),
                                          provenance=prov("swarm", item["id"], "graph-resolver",
                                                          item["score"])))
        report["counts"]["identities resolved by our resolver (no swarm cluster)"] = len(loose)
        report["counts"]["identity matches left for review"] = len(review)
    for pid, p in persons.items():
        proposals.append(NodeProposal(ns=ns, type="Person", id=pid, props=clean({"name": p["name"],
                                      "resolvedBy": p["method"], "isBot": p["isBot"]}),
                                      provenance=prov("swarm", pid, f"identity:{p['method']}", p["confidence"])))
    for aid, a in accounts.items():
        proposals.append(NodeProposal(ns=ns, type="Account", id=aid,
                                      props=clean({"handle": a["handle"], "source": a["source"],
                                                   "name": a.get("name"), "email": a.get("email")}),
                                      provenance=prov(a["source"], aid, "swarm:people")))
        if aid in person_of:
            p = persons[person_of[aid]]
            edge("HAS_ACCOUNT", person_of[aid], aid, "Person", "Account",
                 {"method": p["method"], "evidence": p["evidence"]},
                 prov("swarm", person_of[aid], f"identity:{p['method']}", p["confidence"]))
    report["counts"]["persons"] = len(persons)
    report["counts"]["accounts"] = len(accounts)

    # ---- groups: who may read restricted items ------------------------------------------- #
    by_email = defaultdict(set)
    for aid, a in accounts.items():
        if a.get("email") and aid in person_of:
            by_email[a["email"]].add(person_of[aid])
    for gid, g in state.groups.items():
        proposals.append(NodeProposal(ns=ns, type="Group", id=gid,
                                      props=clean({"name": g.get("name") or gid, "source": g.get("source"),
                                                   "members": g.get("members"), "unresolved": g.get("unresolved")}),
                                      provenance=prov(g.get("source", "swarm"), gid, "swarm:group",
                                                      observed=g.get("observed_at"))))
        for m in g.get("members", []):
            if m in state.groups:      # a group inside a group: readers follow MEMBER_OF transitively
                edge("MEMBER_OF", m, gid, "Group", "Group", {"as": m},
                     prov(g.get("source", "swarm"), gid, "swarm:group", observed=g.get("observed_at")))
                continue
            members = (by_email.get(m[5:].lower(), set()) if m.startswith("user:")
                       else {person_of[m]} if m in person_of else set())     # rule 7
            if not members:
                report["counts"]["group members with no person in the graph"] += 1
            for person in members:
                edge("MEMBER_OF", person, gid, "Person", "Group", {"as": m},
                     prov(g.get("source", "swarm"), gid, "swarm:group", observed=g.get("observed_at")))

    # ---- meaning: extracted entities and relations ----------------------------------------- #
    report["counts"]["extractions applied"] = len(extractions)
    report["counts"]["extractions from an older item version"] = stale
    entities: dict[str, dict] = {}
    relations: dict[tuple, dict] = {}
    for ex in extractions:
        item = state.items[ex["item_id"]]
        text = item.get("text") or ""
        extractor = f"{ex['extractor'].get('model')}:{ex['extractor'].get('prompt_version')}"

        def checked(evidence: list[dict], text: str = text) -> list[dict]:
            good = [e for e in evidence or [] if text[e["start"]:e["end"]] == e["quote"]]   # rule 8
            report["dropped"]["evidence quote not at its offsets"] += len(evidence or []) - len(good)
            return good

        local: dict[str, tuple[str, str]] = {}
        for ent in ex.get("entities", []):
            label = ENTITY_LABEL.get(ent["type"])
            if label is None:
                report["dropped"][f"entity type {ent['type']} (item-backed or unknown)"] += 1
                continue
            evidence = checked(ent.get("evidence"))
            if not evidence:
                report["dropped"]["entity with no checkable evidence"] += 1
                continue
            # One node per thing AND access set. A node shared by a public and a private item
            # would carry the private item's wording and attributes to everyone who can see it.
            eid = f"entity:{ent['type']}:{norm(ent['name']).replace(' ', '-')}@{acl_key(item)}"
            local[ent["local_id"]] = (eid, label)
            e = entities.setdefault(eid, {"label": label, "names": Counter(), "items": {}, "attributes": {},
                                          "acl": acl(item)})
            e["names"][ent["name"]] += 1
            e["attributes"].update(ent.get("attributes") or {})
            e["items"][item["id"]] = (evidence, extractor)

        for rel in ex.get("relations", []):
            def endpoint(ref: str, local: dict = local):
                if ref in local:
                    return local[ref]
                if ref in state.items:
                    return ref, label_of[ref]
                if ref.startswith("person:c:"):           # a resolved person the text names
                    pid = resolve_person(state, ref)
                    return (pid, "Person") if pid in state.clusters else None
                if ref.startswith("person:"):             # a source identity on the item
                    return account(ref), "Account"
                return None
            src, dst = endpoint(rel["source"]), endpoint(rel["target"])
            etype = REL_EDGE.get(rel["type"])
            if not (src and dst and etype):
                report["dropped"][f"relation {rel['type']} with an endpoint we do not have"] += 1
                continue
            evidence = checked(rel.get("evidence"))
            if not evidence:
                report["dropped"]["relation with no checkable evidence"] += 1
                continue
            r = relations.setdefault((etype, src, dst, acl_key(item)), {"seen": [], "rel": rel["type"]})
            r["seen"].append((item, float(rel.get("confidence", 0.5)), evidence, extractor))

    # Extracted edges carry the access of the items they were read from (visibility, aclGroups),
    # so a reader checks the edge, not only its endpoints: "decided by" from a private thread stays
    # private even when the decision itself is also public elsewhere.
    def edge_acl(item: dict) -> dict:
        vis, principals = acl(item)
        return {"visibility": vis, "aclGroups": principals}

    for eid, e in entities.items():
        vis, principals = e["acl"]
        name = e["names"].most_common(1)[0][0]
        # Counts of mentions are not stored: readers count the MENTIONS edges they can see.
        proposals.append(NodeProposal(ns=ns, type=e["label"], id=eid, visibility=vis, acl_groups=principals,
                                      props=clean({"name": name, "aliases": sorted(e["names"]),
                                                   "attributes": e["attributes"]}),
                                      provenance=prov("swarm", eid, "swarm:llm:entity")))
        report["counts"][f"node {e['label']} (extracted)"] += 1
        for iid, (evidence, extractor) in e["items"].items():
            it = state.items[iid]
            edge("MENTIONS", iid, eid, label_of[iid], e["label"],
                 {"quote": evidence[0]["quote"], "start": evidence[0]["start"], "end": evidence[0]["end"],
                  "extractor": extractor, "sourceAuthority": weights.source_authority(it["source"]),
                  **edge_acl(it)},
                 prov(it["source"], iid, f"swarm:llm:{extractor}", weights.MENTION_CONFIDENCE,
                      evidence[0]["quote"], it.get("updated_at")))

    for (etype, (sid, stype), (did, dtype), _key), r in relations.items():
        it, conf, evidence, extractor = r["seen"][0]
        edge(etype, sid, did, stype, dtype,
             {"relation": r["rel"], "modelConfidence": conf, "corroboratedBy": len(r["seen"]),
              "evidenceItems": [x[0]["id"] for x in r["seen"]], "quote": evidence[0]["quote"],
              "extractor": extractor,
              "sourceAuthority": max(weights.source_authority(x[0]["source"]) for x in r["seen"]),
              **edge_acl(it)},
             prov(it["source"], it["id"], f"swarm:llm:{extractor}",
                  weights.corroborate([weights.extraction_confidence(c) for _, c, _, _ in r["seen"]]),
                  evidence[0]["quote"], it.get("updated_at")))

    bad = [(p, validate(p)) for p in proposals if validate(p)]
    for _, reason in bad:
        report["dropped"][f"door would refuse: {reason}"] += 1
    return proposals, report


def main(argv: list[str] | None = None) -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Build the company graph from swarm runs (schema v2)")
    ap.add_argument("--root", required=True, help="a folder holding runs/, or 's3' for the bucket")
    ap.add_argument("--env", type=Path, help="file with S3_ENDPOINT, S3_BUCKET, STORAGE_ACCESS_KEY, STORAGE_SECRET_KEY "
                    "(the swarm's .env); the environment is used when absent")
    ap.add_argument("--ns", default="kg:swarm")
    ap.add_argument("--dry-run", action="store_true", help="read, map and check; write nothing")
    ap.add_argument("--keep", action="store_true", help="do not wipe the namespace first")
    args = ap.parse_args(argv)

    env = dict(os.environ)
    if args.env:
        for line in args.env.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"')
    state = load(open_store(args.root, env))
    print(f"Runs applied ({len(state.runs)}): {', '.join(state.runs)}")
    if state.skipped_runs:
        print(f"Runs skipped, manifest not complete: {', '.join(state.skipped_runs)}")
    print(f"Live items {len(state.items)}, groups {len(state.groups)}, identity clusters {len(state.clusters)}, "
          f"extractions {len(state.extractions)}, as of {state.as_of}")
    for k, v in sorted(state.counts.items()):
        print(f"  {v:>5}  {k}")
    for k, v in state.problems.items():
        print(f"  PROBLEM {v:>3}  {k}")

    proposals, report = build(state, args.ns)
    print("\nProposals")
    for k, v in sorted(report["counts"].items()):
        print(f"  {v:>5}  {k}")
    if report["pending_refs"]:
        print("Refs to items not ingested yet (kept on the node as pendingRefs):")
        for src, rtype, target in report["pending_refs"]:
            print(f"         {src} -{rtype}-> {target}")
    if report["dropped"]:
        print("Dropped")
        for k, v in sorted(report["dropped"].items()):
            print(f"  {v:>5}  {k}")

    if args.dry_run:
        print(f"\nDRY RUN - nothing written. {len(proposals)} proposals.")
        return
    from . import load as loader_mod
    print(f"\nWriting to namespace {args.ns!r} ...")
    with loader_mod.Loader(args.ns) as loader:
        if not args.keep:
            loader.wipe()
        receipt = loader.ingest(proposals)
        print(f"  {receipt}")
        for reason, n in receipt.reasons.most_common(8):
            print(f"    {n:>5}  {reason}")
        for key, n in sorted(loader.store.counts(args.ns).items(), key=lambda kv: -kv[1]):
            print(f"  {key:<22} {n:>6}")


if __name__ == "__main__":
    main()
