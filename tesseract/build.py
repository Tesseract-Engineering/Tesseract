"""
build.py - build the company graph from any company's exports, via an adapter.

    adapter (adapters/<name>.py) ->  Bundle: services, teams, roster, records
    corpus statistics            ->  boilerplate masked, common tokens refused
    identity resolution          ->  one Person per human across every system
    declared fields              ->  ABOUT / OWNS / ASSIGNED_TO / ..., parsed
    extraction (matching.py)     ->  MENTIONS / CREDITS, inferred, with a quote
    the door                     ->  namespaced, hashed, receipted writes

Nothing in this file knows which company it is building. Where a company
stores things, and what its fields are called, is the adapter's job; see
bundle.py for the contract between the two.

WHAT IS PARSED AND WHAT IS INFERRED, kept apart as everywhere else:

  ABOUT, OWNS, MEMBER_OF, ASSIGNED_TO, COMMENTED, FOR_CUSTOMER, REFERENCES
              a field in the source said so. Confidence 1.0, producer names
              the field, no quote needed - the field IS the evidence.
  MENTIONS    an extractor found a name in prose. Carries the quote, the
              surface form, how often, and whether it was in the title.
  CREDITS     a document header's labelled line. Parsed, with the line kept.

Usage:
    python -m tesseract.build --root <folder>              # TESSERACT_DATA if omitted
    python -m tesseract.build --root <folder> --dry-run

--dry-run reads, resolves and extracts but writes nothing: the first thing to
run on new data. It prints the checks below and the proposal counts.

CHECKS printed on every run, so new data that breaks something says so:
  coverage   files read vs. present, and the folders nobody read
  identity   people whose linked accounts carry different full names
  shape      problems with the adapter's Bundle (duplicate ids, dangling refs)
  --score    extraction measured against tickets' own declared service
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from . import adapters, corpus, load, matching, resolve
from .config import env
from .contract import EdgeProposal, NodeProposal, Provenance


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def declared(source: str, source_id: str, producer: str) -> Provenance:
    """Provenance for a fact the source states outright. No quote, because
    there is no prose to quote - the field IS the evidence."""
    return Provenance(source=source, source_id=source_id, producer=producer,
                      confidence=1.0, quote="", ingested_at=now())


# --------------------------------------------------------------------------- #
# identity hygiene, before anything is merged
# --------------------------------------------------------------------------- #

def untrustworthy_handles(records, roster_accounts) -> dict[tuple[str, str], set[str]]:
    """Handles observed under more than one person's name.

    Exports do not always identify users by their `user` field: one handle can
    post under five different people's names (shared logins, bots relaying
    messages, migrated workspaces). Resolution trusts "same handle, same
    human", so each such handle would weld five people into one Person and
    strip every ticket of its real assignee.

    A handle is only evidence of identity if every name seen with it agrees.
    Names that differ only in formatting ('priya.sharma' vs 'Priya Sharma')
    agree; different people do not.
    """
    everyone = list(roster_accounts)
    for record in records:
        everyone.extend(record.people)
        everyone.extend(record.meta.get("commenters") or [])

    # Every full name anyone appears under. A handle that spells one of these
    # IS a claim about identity, and it can contradict the display name beside
    # it: 'grace.hopper.ii' posting once as "Priya Sharma" looks consistent -
    # one handle, one name - but resolution joined it to Grace Hopper II by
    # the handle and to Priya by the name, and the account became a bridge
    # that welded the CEO into someone else. Short handles ('priya', 'maya.l')
    # spell nobody's full name, so they are left alone.
    full_names = {resolve.normalise(w["name"]) for w in everyone
                  if w.get("name") and " " in w["name"].strip()}

    seen: dict[tuple[str, str], set[str]] = defaultdict(set)
    for who in everyone:
        handle, name = (who.get("handle") or "").lower(), who.get("name") or ""
        if not handle:
            continue
        key = (who["source"], handle)
        handle_norm = resolve.normalise(handle)
        name_norm = resolve.normalise(name) if name else handle_norm
        seen[key].add(name_norm)
        if handle_norm in full_names and handle_norm != name_norm:
            seen[key].add(handle_norm)
    return {key: names for key, names in seen.items() if len(names) > 1}


def rekey_by_name(records, bad: dict) -> int:
    """Identify speakers on an untrustworthy handle by their display name."""
    changed = 0
    for record in records:
        for group in (record.people, record.meta.get("commenters") or []):
            for who in group:
                key = (who["source"], (who.get("handle") or "").lower())
                if key in bad and who.get("name"):
                    who["handle"] = who["name"].lower()
                    changed += 1
    return changed


def separate_collisions(records) -> tuple[set[str], list[dict]]:
    """Two different records that the source gave the same key.

    Real exports reuse keys: a support system that renumbered, two PRs from
    different repos with one number. Written under one id, the second silently
    overwrites the first. Neither is dropped: each gets
    its id qualified by its file name, keeps its original `key`, and the
    collision goes to the review queue. A link to the bare key is then
    ambiguous and is NOT resolved to either (see REFERENCES).
    """
    groups: dict[str, list] = defaultdict(list)
    for record in records:
        groups[record.id].append(record)
    collided, reviews = set(), []
    for rid, group in groups.items():
        if len(group) < 2:
            continue
        collided.add(rid)
        used: set[str] = set()
        for index, record in enumerate(group):
            stem = Path(record.meta.get("path") or "").stem or str(index)
            new_id = f"{rid}~{stem}"
            if new_id in used:
                new_id = f"{rid}~{stem}-{index}"
            used.add(new_id)
            record.id = new_id
        reviews.append({
            "id": f"review:key:{rid}", "kind": "key-collision", "a": rid,
            "b": ", ".join(sorted(used)), "aName": rid, "bName": f"{len(group)} records",
            "score": 0.0, "status": "pending",
            "reason": (f"the source uses {rid} for {len(group)} different records "
                       f"({', '.join(r.title[:50] for r in group)}); all are kept, "
                       f"under file-qualified ids, and links to the bare key are not "
                       f"resolved until a human says which one is meant"),
        })
    return collided, reviews


def suspect_merges(people, links) -> list[tuple[str, list[str]]]:
    """People whose linked accounts carry two or more different FULL names.

    Resolution may legitimately join 'priya.sharma' to "Priya Sharma"; it
    should never join "Priya Sharma" to "Grace Hopper II". A nonzero count
    here after new data is the first thing to look at.
    """
    names: dict[str, set[str]] = defaultdict(set)
    for link in links:
        name = (link["account"].get("name") or "").strip()
        if " " in name:
            names[link["personId"]].add(resolve.normalise(name))
    label = {p["id"]: p["name"] for p in people}
    return [(label.get(pid, pid), sorted(ns)) for pid, ns in names.items() if len(ns) > 1]


# --------------------------------------------------------------------------- #
# the build
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Build a company graph from its exports")
    parser.add_argument("--root", type=Path, default=Path(env("TESSERACT_DATA") or "."),
                        help="the company's export folder (default: TESSERACT_DATA)")
    parser.add_argument("--adapter", default="auto",
                        help=f"one of {', '.join(adapters.REGISTRY)}, or auto (default)")
    parser.add_argument("--ns", default=env("TESSERACT_NS") or None,
                        help="namespace (default: TESSERACT_NS, else tesseract:<adapter>)")
    parser.add_argument("--keep", action="store_true",
                        help="do not wipe the namespace before writing")
    parser.add_argument("--dry-run", action="store_true",
                        help="read, resolve and extract, print the checks, write nothing")
    parser.add_argument("--score", action="store_true",
                        help="score extracted service routing against the "
                             "declared service field on each ticket")
    args = parser.parse_args(argv)

    root: Path = args.root.resolve()
    if not root.is_dir():
        sys.exit(f"Data folder not found: {root}")
    adapter = adapters.detect(root) if args.adapter == "auto" else adapters.get(args.adapter)
    ns = args.ns or f"tesseract:{adapter.NAME}"
    print(f"Data: {root}\nAdapter: {adapter.NAME}   namespace: {ns}\n")

    bundle = adapter.load(root)
    records = bundle.records
    services = bundle.services
    people_rows = bundle.people

    # ---- what the adapter found ------------------------------------------- #
    kinds = Counter(s["props"].get("kind", "?") for s in services.values())
    alias_map = matching.service_alias_map(services)
    alias_re = matching.alias_pattern(alias_map) if alias_map else None
    print(f"Services: {len(services)}"
          + (" (" + ", ".join(f"{n} {k}" for k, n in kinds.most_common()) + ")"
             if services else ""))
    print(f"  {len(alias_map)} aliases generated from {len(services)} canonical names")
    statuses = Counter(p.get("status") or "listed" for p in people_rows)
    print(f"Org: {len(bundle.teams)} teams, {len(people_rows)} people"
          + (" (" + ", ".join(f"{n} {k}" for k, n in statuses.most_common()) + ")"
             if people_rows else ""))

    by_kind: dict[str, list] = defaultdict(list)
    for record in records:
        by_kind[record.kind].append(record)
    print("\nRecords: " + ", ".join(f"{k} {len(v)}" for k, v in sorted(by_kind.items())))
    by_source = Counter(r.source for r in records)
    print("  by source: " + ", ".join(f"{k} {v}" for k, v in sorted(by_source.items())))

    collided, collision_reviews = separate_collisions(records)
    if collided:
        print(f"\nKey collisions: {len(collided)} ids used by more than one record - all kept "
              f"under file-qualified ids, queued for review: {', '.join(sorted(collided)[:6])}")

    read, unread = bundle.coverage()
    print(f"\nCoverage: {read} files read, {sum(n for _, n in unread)} not read")
    for folder, n in unread[:12]:
        print(f"  {n:>5}  {folder}/")
    for problem in bundle.problems():
        print(f"  ADAPTER PROBLEM: {problem}")

    # ---- corpus statistics ------------------------------------------------- #
    stats = corpus.CorpusStats(records)
    print("\nCorpus statistics")
    for line in stats.report():
        print(line)

    # ---- identity ---------------------------------------------------------- #
    # Some sources sign with an employee ID ("emp-006") instead of a name.
    # That is the MOST reliable identifier there is - the roster's own key -
    # so it is translated to the roster name before resolution, rather than
    # becoming a dozen people called emp-0NN.
    name_of_emp = {p["empId"].lower(): p["name"] for p in people_rows if p.get("empId")}
    for record in records:
        for who in list(record.people) + list(record.meta.get("commenters") or []):
            emp = (who.get("handle") or "").lower()
            if emp in name_of_emp:
                who["name"] = name_of_emp[emp]
                who["handle"] = name_of_emp[emp].lower()

    bad_handles = untrustworthy_handles(records, bundle.accounts)
    rekeyed = rekey_by_name(records, bad_handles)
    if bad_handles:
        print(f"\nUntrustworthy handles: {len(bad_handles)} used under more than one "
              f"person's name - {rekeyed} appearances re-identified by display name")
        for (source, handle), names in sorted(bad_handles.items(),
                                               key=lambda kv: -len(kv[1]))[:5]:
            print(f"  {source}:{handle} -> {len(names)} different names")
    conflict_reviews = [{
        "id": f"review:handle:{source}:{handle}", "kind": "identity-conflict",
        "a": f"{source}:{handle}", "b": ", ".join(sorted(names)),
        "aName": handle, "bName": f"{len(names)} names", "score": 0.0,
        "status": "pending",
        "reason": (f"{source} handle '{handle}' appears under {len(names)} different "
                   f"people's names; treated as NOT identifying anyone until a human "
                   f"says which of them it belongs to"),
    } for (source, handle), names in bad_handles.items()]

    accounts = list(bundle.accounts)
    for record in records:
        accounts.extend(record.people)
    for record in by_kind.get("issue", []):
        name = record.meta.get("assigneeName")
        handle = record.meta.get("assigneeSlack")
        if name:
            accounts.append({"source": record.source, "handle": name.lower(),
                             "name": name, "email": ""})
        if handle:
            accounts.append({"source": "slack", "handle": handle.lower(),
                             "name": name or handle, "email": ""})
    # Everyone who commented, wrote an internal note or reviewed is an
    # identity too - otherwise COMMENTED would point at accounts that were
    # never created, and the door would drop the edges.
    for record in records:
        for who in record.meta.get("commenters") or []:
            accounts.append({k: who[k] for k in ("source", "handle", "name", "email")})

    people, links, review = resolve.resolve_people(accounts)
    print(f"\nIdentity resolution: {len(accounts)} raw identities -> {len(people)} people "
          f"({len(review)} queued for review)")
    suspects = suspect_merges(people, links)
    print(f"  suspect merges (one person, several different full names): {len(suspects)}")
    for label, names in suspects[:8]:
        print(f"    {label}: {', '.join(names)}")

    person_by_norm = {resolve.normalise(p["name"]): p["id"] for p in people}

    # ---- proposals ---------------------------------------------------------- #
    proposals: list = []

    for service in services.values():
        proposals.append(NodeProposal(
            ns=ns, type="Service", id=service["id"], props=dict(service["props"]),
            visibility="internal",
            provenance=declared(service["source"], service["id"], service["producer"]),
        ))

    # Teams, and every claim about who owns what. Claims that disagree are all
    # written, each with its `basis`: "who owns billing-service?" then returns
    # both answers, labelled - which is the truth when a company's sources
    # disagree.
    for team in bundle.teams:
        proposals.append(NodeProposal(
            ns=ns, type="Team", id=team["id"], props=dict(team["props"]),
            visibility="internal",
            provenance=declared(team["source"], team["source_id"], team["producer"]),
        ))
    for claim in bundle.ownership:
        service = services.get(claim["service"])
        if not service:
            continue
        proposals.append(EdgeProposal(
            ns=ns, type="OWNS", src_id=claim["team"], dst_id=service["id"],
            src_type="Team", dst_type="Service", props={"basis": claim["basis"]},
            provenance=Provenance(source=claim["source"], source_id=claim["source_id"],
                                  producer=claim["producer"], confidence=1.0,
                                  quote=claim.get("quote") or "", ingested_at=now()),
        ))

    # People, enriched from the roster where resolution found them.
    roster_by_norm = {resolve.normalise(p["name"]): p for p in people_rows}
    enriched = []
    for person in people:
        extra = roster_by_norm.get(resolve.normalise(person["name"]), {})
        enriched.append({**person,
                         **{k: extra[k] for k in load.PERSON_EXTRAS if extra.get(k)},
                         # Not "unknown": we know exactly what is true of
                         # these people - they appear in the records, and
                         # the roster does not list them.
                         "status": extra.get("status", "not-in-directory")})
    proposals += load.people(enriched, ns)
    proposals += load.accounts(links, ns)
    proposals += load.review_items(review + conflict_reviews + collision_reviews, ns)

    # Person -> Team, best-ranked source first; a person placed by a better
    # source is not placed again by a worse one.
    person_by_emp = {p["empId"]: person_by_norm.get(resolve.normalise(p["name"]))
                     for p in people_rows if p.get("empId")}
    member_edges: dict[tuple[str, str], dict] = {}
    placed: dict[str, int] = {}
    unplaced = []
    for membership in sorted(bundle.memberships, key=lambda m: m.get("rank", 0)):
        rank = membership.get("rank", 0)
        if membership.get("empId"):
            person_id = person_by_emp.get(membership["empId"])
        else:
            person_id = person_by_norm.get(resolve.normalise(membership.get("name") or ""))
        if not person_id or placed.get(person_id, rank) < rank:
            continue
        if not membership.get("team"):
            unplaced.append(f"{membership.get('name') or membership.get('empId')} "
                            f"({membership.get('teamLabel')})")
            continue
        member_edges[(person_id, membership["team"])] = membership
        placed[person_id] = min(placed.get(person_id, rank), rank)

    for (person_id, team_id), membership in member_edges.items():
        proposals.append(EdgeProposal(
            ns=ns, type="MEMBER_OF", src_id=person_id, dst_id=team_id,
            src_type="Person", dst_type="Team", props={"via": membership["via"]},
            provenance=declared(membership.get("source", "org"), person_id,
                                membership["producer"]),
        ))
    via = Counter(m["via"] for m in member_edges.values())
    print(f"\nTeams: {len(member_edges)} memberships"
          + (" (" + ", ".join(f"{n} from {k}" for k, n in via.most_common()) + ")"
             if via else ""))
    if unplaced:
        print("  listed on a team that matches nothing: " + ", ".join(unplaced))

    # Artifacts and authorship. EVERY participant authors a thread, weighted by
    # how many messages they posted - not just whoever spoke first.
    proposals += load.artifacts(records, ns)
    authored = 0
    for record in records:
        counts = record.meta.get("messageCounts") or {}
        seen_accounts = set()
        for person in record.people:
            handle = (person.get("handle") or person.get("email") or "").lower()
            account_id = f"account:{person['source']}:{handle}"
            if not handle or account_id in seen_accounts:
                continue
            seen_accounts.add(account_id)
            proposals.append(EdgeProposal(
                ns=ns, type="AUTHORED", src_id=account_id, dst_id=record.id,
                src_type="Account", dst_type=load.KIND_TYPE[record.kind],
                props={"messages": person.get("count") or counts.get(handle, 1)},
                provenance=Provenance(source=record.source, source_id=record.id,
                                      producer=f"connector:{record.source}",
                                      confidence=1.0, observed_at=record.created_at or None,
                                      ingested_at=now()),
            ))
            authored += 1

    assigned = 0
    unassignable = []
    for record in by_kind.get("issue", []):
        name = record.meta.get("assigneeName")
        if not name:
            continue
        person_id = person_by_norm.get(resolve.normalise(name))
        if not person_id:
            unassignable.append(name)
            continue
        proposals.append(EdgeProposal(
            ns=ns, type="ASSIGNED_TO", src_id=record.id, dst_id=person_id,
            src_type="Issue", dst_type="Person", props={},
            provenance=declared(record.source, record.id, "field:assignee"),
        ))
        assigned += 1
    print(f"Authorship: {authored} AUTHORED, {assigned} ASSIGNED_TO"
          + (f"  (assignees with no person: {', '.join(unassignable)})" if unassignable else ""))

    # Declared subject: ABOUT. Parsed, not guessed.
    declared_service: dict[str, str] = {}
    unknown_declared = Counter()
    for record in by_kind.get("issue", []):
        name = record.meta.get("declaredService")
        if not name:
            continue
        service = services.get(name)
        if not service:
            unknown_declared[name] += 1
            continue
        declared_service[record.id] = service["id"]
        field = record.meta.get("declaredServiceField") or "service"
        proposals.append(EdgeProposal(
            ns=ns, type="ABOUT", src_id=record.id, dst_id=service["id"],
            src_type="Issue", dst_type="Service", props={"basis": "declared-field"},
            provenance=declared(record.source, record.id, f"field:{field}"),
        ))

    # Who took part in each ticket's discussion - distinct from filing it or
    # being assigned it.
    commented = Counter()
    for record in by_kind.get("issue", []):
        for who in record.meta.get("commenters") or []:
            proposals.append(EdgeProposal(
                ns=ns, type="COMMENTED", src_id=f"account:{who['source']}:{who['handle']}",
                dst_id=record.id, src_type="Account", dst_type="Issue",
                props={"as": who["as"], "count": who["count"]},
                provenance=Provenance(source=record.source, source_id=record.id,
                                      producer=f"connector:{record.source}", confidence=1.0,
                                      observed_at=record.created_at or None,
                                      ingested_at=now()),
            ))
            commented[who["as"]] += 1

    # Customers, from the tickets that name them.
    customers: dict[str, dict] = {}
    for record in by_kind.get("issue", []):
        name = record.meta.get("customer")
        if not name:
            continue
        cid = f"customer:{resolve.normalise(name)}"
        entry = customers.setdefault(cid, {"id": cid, "name": name, "tickets": 0,
                                           "source": record.source,
                                           "tier": None, "arr": None, "merchantId": None})
        entry["tickets"] += 1
        entry["tier"] = entry["tier"] or record.meta.get("customerTier")
        entry["arr"] = entry["arr"] or record.meta.get("customerArr")
        entry["merchantId"] = entry["merchantId"] or record.meta.get("merchantId")
        proposals.append(EdgeProposal(
            ns=ns, type="FOR_CUSTOMER", src_id=record.id, dst_id=cid,
            src_type="Issue", dst_type="Customer", props={},
            provenance=declared(record.source, record.id, "field:customer_name"),
        ))
    # Possible duplicate customers ("ACME Corp Global" / "ACME Corporation")
    # share no identifier, so nothing in the data says whether they are one
    # account. Same rule as for people: similar names are a question for a
    # human, never a silent merge.
    ordered = sorted(customers.values(), key=lambda e: e["name"])
    customer_reviews: list[dict] = []
    for i, first in enumerate(ordered):
        for second in ordered[i + 1:]:
            a, b = resolve.normalise(first["name"]), resolve.normalise(second["name"])
            common = len(os.path.commonprefix([a, b]))
            if common >= 8:
                customer_reviews.append({
                    "id": f"review:customer:{first['id']}|{second['id']}",
                    "kind": "customer-duplicate", "a": first["id"], "b": second["id"],
                    "aName": first["name"], "bName": second["name"],
                    "score": round(common / max(len(a), len(b)), 3), "status": "pending",
                    "reason": (f"'{first['name']}' ({first['tickets']} tickets) and "
                               f"'{second['name']}' ({second['tickets']}) may be one customer; "
                               f"no shared merchant id, so they are kept apart"),
                })
    proposals += load.review_items(customer_reviews, ns)
    for entry in customers.values():
        proposals.append(NodeProposal(
            ns=ns, type="Customer", id=entry["id"],
            props={k: v for k, v in {"name": entry["name"], "tier": entry["tier"],
                                     "arr": entry["arr"], "merchantId": entry["merchantId"],
                                     "supportTickets": entry["tickets"]}.items() if v is not None},
            visibility="internal",
            provenance=declared(entry["source"], entry["id"], "field:customer_name"),
        ))

    # Tickets / PRs -> the tickets they say they relate to.
    ticket_ids = {r.id for r in by_kind.get("issue", [])}
    linked, dangling, ambiguous = 0, Counter(), Counter()
    for record in by_kind.get("issue", []):
        for key in record.refs.get("linkedIssues") or []:
            target = f"ticket:{key}"
            if target in collided:
                ambiguous[key] += 1
                continue
            if target not in ticket_ids:
                dangling[key] += 1
                continue
            proposals.append(EdgeProposal(
                ns=ns, type="REFERENCES", src_id=record.id, dst_id=target,
                src_type="Issue", dst_type="Issue", props={"via": "linked_issues"},
                provenance=declared(record.source, record.id, "field:linked_issues"),
            ))
            linked += 1
    print(f"Discussion: {sum(commented.values())} COMMENTED"
          + (" (" + ", ".join(f"{k} {v}" for k, v in commented.most_common()) + ")"
             if commented else ""))
    print(f"Customers: {len(customers)}   links: {linked} REFERENCES"
          + (f", {sum(dangling.values())} to tickets not in the export "
             f"(e.g. {', '.join(list(dangling)[:4])})" if dangling else "")
          + (f", {sum(ambiguous.values())} to a key two records share" if ambiguous else ""))

    # ---- inferred: MENTIONS, with the quote that justifies each --------------- #
    hits = []
    if alias_re is not None:
        for record in records:
            src_type = load.KIND_TYPE[record.kind]
            for hit in matching.service_mentions(record, alias_map, alias_re, stats):
                hit.update(srcId=record.id, dstId=hit["targetId"],
                           srcLabel=src_type, dstLabel="Service", extractedAt=now())
                hits.append(hit)

    # People named in prose, resolved to the SAME Person node identity
    # resolution produced. The vocabulary comes from RESOLUTION, not the
    # roster: a roster lists a fraction of a company, and resolution already
    # knows the names, handles and addresses of everyone else.
    name_of = {p["id"]: p["name"] for p in people}
    vocabulary_rows = list(people_rows)
    for link in links:
        account = link["account"]
        name = name_of.get(link["personId"])
        if not name:
            continue
        vocabulary_rows.append({
            "name": name, "email": account.get("email") or "",
            "slack": account["handle"] if account["source"] == "slack" else "",
        })
    person_alias = matching.build_person_vocabulary(vocabulary_rows)
    person_re = matching.person_pattern(person_alias) if person_alias else None
    person_hits, unresolved = [], Counter()
    if person_re is not None:
        for record in records:
            src_type = load.KIND_TYPE[record.kind]
            for hit in matching.person_mentions(record, person_alias, person_re, stats,
                                                bundle.speaker_sources):
                person_id = person_by_norm.get(resolve.normalise(hit["personName"]))
                if not person_id:
                    unresolved[hit["personName"]] += 1
                    continue
                hit.update(srcId=record.id, dstId=person_id, targetId=person_id,
                           srcLabel=src_type, dstLabel="Person", extractedAt=now())
                person_hits.append(hit)

    # Customers named in prose. Full name, or the name run together as in file
    # names and handles ("megaretailer"), and nothing shorter: "Apex" or
    # "Nordic" alone are words.
    customer_alias: dict[str, tuple[str, float]] = {}
    for entry in customers.values():
        name = entry["name"]
        customer_alias[name.lower()] = (name, 1.0)
        compact = re.sub(r"[^a-z0-9]", "", name.lower())
        if " " in name and len(compact) >= 8:
            customer_alias.setdefault(compact, (name, 0.95))
    customer_hits = []
    if customer_alias:
        customer_re = matching.person_pattern(customer_alias)
        customer_id = {e["name"]: e["id"] for e in customers.values()}
        for record in records:
            src_type = load.KIND_TYPE[record.kind]
            for hit in matching.person_mentions(record, customer_alias, customer_re, stats,
                                                bundle.speaker_sources):
                # A ticket FOR a customer names it in every subject line;
                # that is already the FOR_CUSTOMER edge.
                if record.meta.get("customer") == hit["personName"]:
                    continue
                target = customer_id[hit["personName"]]
                hit.update(srcId=record.id, dstId=target, targetId=target,
                           srcLabel=src_type, dstLabel="Customer",
                           extractor="rule:customer-alias", extractedAt=now())
                customer_hits.append(hit)
    print(f"  {len(customer_hits)} customer MENTIONS")

    proposals += load.mentions(hits + person_hits + customer_hits, ns)

    # Who each document's header credits. Parsed from a labelled line, so it
    # is stated, not inferred; the line is kept as the quote.
    credits = Counter()
    for record in by_kind.get("document", []) if person_re is not None else []:
        for credit in matching.document_credits(record, person_alias, person_re,
                                                bundle.speaker_sources):
            person_id = person_by_norm.get(resolve.normalise(credit["personName"]))
            if not person_id:
                continue
            confidence = person_alias.get(credit["surface"].lower(), (None, 1.0))[1]
            proposals.append(EdgeProposal(
                ns=ns, type="CREDITS", src_id=record.id, dst_id=person_id,
                src_type="Document", dst_type="Person",
                props={"role": credit["role"], "surface": credit["surface"]},
                provenance=Provenance(source=record.source, source_id=record.id,
                                      producer="parser:doc-header", confidence=confidence,
                                      quote=credit["quote"],
                                      observed_at=record.created_at or None,
                                      ingested_at=now()),
            ))
            credits[credit["role"]] += 1
    print(f"Document credits: {sum(credits.values())}"
          + (" (" + ", ".join(f"{k} {v}" for k, v in credits.most_common()) + ")"
             if credits else ""))
    print(f"\nExtraction: {len(hits)} service MENTIONS, {len(person_hits)} person MENTIONS "
          f"across {len(records)} records, {len(declared_service)} declared ABOUT")
    print(f"  {len(person_alias)} person aliases (full name, email, @handle)")
    if unresolved:
        print("  named but never resolved to a person: "
              + ", ".join(f"{k} ({v})" for k, v in unresolved.most_common()))
    if unknown_declared:
        print("  declared services with no entity: "
              + ", ".join(f"{k} ({v})" for k, v in unknown_declared.most_common()))

    # ---- write -------------------------------------------------------------- #
    if args.dry_run:
        by_type = Counter(p.type for p in proposals)
        print(f"\nDRY RUN - nothing written. {len(proposals)} proposals:")
        for key, n in by_type.most_common():
            print(f"  {key:<22} {n:>6}")
    else:
        print(f"\nWriting to namespace {ns!r} ...")
        with load.Loader(ns) as loader:
            if not args.keep:
                loader.wipe()
            receipt = loader.ingest(proposals)
            print(f"  {receipt}")
            for reason, n in receipt.reasons.most_common(8):
                print(f"    {n:>5}  {reason}")

            print("\nGraph now holds, in this namespace")
            for key, n in sorted(loader.store.counts(ns).items(), key=lambda kv: -kv[1]):
                print(f"  {key:<22} {n:>6}")

    if args.score:
        score_routing(by_kind.get("issue", []), hits, declared_service, alias_map, alias_re)


def score_routing(tickets, hits, declared_service, alias_map, alias_re) -> None:
    """Score the extractor against the tickets' own declared service.

    Two buckets, because a raw accuracy figure would be a lie by omission:

      LEXICAL     some alias of the declared service appears in the prose.
                  A string matcher can get these, and if it does not, that
                  is a bug in the matcher.
      SEMANTIC    it does not appear at all ("renewing a wildcard SSL cert"
                  declared as api-gateway). Knowing those belong together is
                  knowledge about the company, not about strings, and no
                  lexical method reaches them at any threshold.

    The blended figure is printed too, because it is what a user experiences.
    """
    by_record: dict[str, list[dict]] = defaultdict(list)
    for hit in hits:
        by_record[hit["srcId"]].append(hit)

    scored = [t for t in tickets if t.id in declared_service]
    n = len(scored)
    print("\n" + "=" * 74)
    print(f"ROUTING SCORED AGAINST THE DECLARED SERVICE FIELD  (n={n} tickets)")
    print("=" * 74)
    if not n or alias_re is None:
        print("  nothing to score - this data declares no services on its tickets")
        return

    buckets: dict[str, list] = {"lexical": [], "semantic": []}
    for ticket in scored:
        truth = declared_service[ticket.id]
        blob = f"{ticket.title}\n{ticket.text}"
        nameable = any(alias_map.get(m.group(0).lower(), (None, 0))[0] == truth
                       for m in alias_re.finditer(blob))
        buckets["lexical" if nameable else "semantic"].append(ticket)

    overall = {"top1": 0, "any": 0}
    for bucket, tickets_in in (("lexical", buckets["lexical"]),
                               ("semantic", buckets["semantic"])):
        if not tickets_in:
            continue
        top1 = anyhit = 0
        misses = []
        spread = []
        for ticket in tickets_in:
            truth = declared_service[ticket.id]
            found = sorted(by_record.get(ticket.id, []),
                           key=lambda h: (-h["confidence"], -h["count"]))
            spread.append(len(found))
            ids = [h["targetId"] for h in found]
            if ids and ids[0] == truth:
                top1 += 1
            if truth in ids:
                anyhit += 1
            else:
                misses.append((ticket.meta.get("key") or ticket.id, truth.split(":", 1)[1],
                               [i.split(":", 1)[1] for i in ids[:3]]))
        overall["top1"] += top1
        overall["any"] += anyhit
        size = len(tickets_in)
        headline = ("declared service IS named in the text - a string matcher should get these"
                    if bucket == "lexical" else
                    "declared service is NOWHERE in the text - unreachable lexically")
        print(f"\n  {bucket.upper()}  ({size}/{n} tickets) - {headline}")
        print(f"    top-1 correct    {top1:>3}/{size}  {top1 / size:.0%}")
        print(f"    declared found   {anyhit:>3}/{size}  {anyhit / size:.0%}")
        print(f"    services named per ticket: mean {sum(spread) / size:.1f}")
        for key, truth, got in misses[:8]:
            print(f"      {key:<10} {truth:<22} -> {got or 'nothing'}")

    print("\n  BLENDED (what a user actually gets)")
    print(f"    top-1 correct    {overall['top1']:>3}/{n}  {overall['top1'] / n:.0%}")
    print(f"    declared found   {overall['any']:>3}/{n}  {overall['any'] / n:.0%}")
    lex = len(buckets["lexical"])
    print(f"\n  The ceiling for ANY lexical method on this corpus is {lex}/{n} "
          f"({lex / n:.0%}).")


if __name__ == "__main__":
    main()
