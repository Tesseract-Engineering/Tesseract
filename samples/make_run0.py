"""
make_run0.py - build samples/run-0, the hand-written sample run for the graph
engineer.

The records below are written by hand, but every file is produced by the
real write and manifest stages, so the content hashes, part names and
manifests are exactly what a pipeline would emit. All data is fictional
(Acme, acme.com). Fetched source data never enters the repo.

    python samples/make_run0.py      rewrite samples/run-0/runs/
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import datetime
from pathlib import Path

from swarm import ids
from swarm.records import (
    Acl,
    AclUpdateRecord,
    Entity,
    ExtractionRecord,
    Extractor,
    GroupRecord,
    IdentityMember,
    IdentityRecord,
    ItemRecord,
    Person,
    Ref,
    Relation,
    Span,
    TombstoneRecord,
    content_hash,
)
from swarm.stages.manifest import finish_run
from swarm.stages.redact import redact
from swarm.stages.write import RunWriter
from swarm.store import LocalStore

OUT = Path(__file__).resolve().parent / "run-0"

# A fake token in GitHub's format, assembled so the literal never sits in the
# repo for secret scanners to flag. Redaction must strip it.
PLANTED_TOKEN = "ghp" + "_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def at(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def item(run_id: str, **fields) -> ItemRecord:
    fields.setdefault("people", [])
    fields.setdefault("refs", [])
    fields.setdefault("meta", {})
    fields["content_hash"] = content_hash(title=fields["title"], text=fields["text"],
                                          container=fields["container"], people=fields["people"],
                                          refs=fields["refs"], meta=fields["meta"])
    return ItemRecord(run_id=run_id, **fields)


def span(text: str, quote: str) -> Span:
    start = text.index(quote)
    return Span(start=start, end=start + len(quote), quote=quote)


# --------------------------------------------------------------------------- #
# GitHub: one repo, an issue, the PR that closes it, and the merge commit
# --------------------------------------------------------------------------- #

REPO_ID = 7001
REPO = ids.github_repo(REPO_ID)
READERS = f"group:{REPO}:readers"
ORG = Acl(visibility="restricted", principals=[READERS])

sam_gh = Person(id=ids.person("github", "slee"), source="github", handle="slee", name="Sam Lee", roles=["author"])
priya_gh = Person(id=ids.person("github", "priya-r"), source="github", handle="priya-r", name="Priya Rao",
                  roles=["reviewer"])


def github_run(store: LocalStore) -> None:
    run_id = "2026-10-20T03:00:00Z-github"
    fetched = at("2026-10-20T03:02:11Z")
    w = RunWriter(store, "github", run_id)

    w.add_item(item(
        run_id, id=REPO, aliases=["acme/api"], kind="repo", source="github", container=None,
        title="acme/api", text="Public API and auth service for Acme.",
        url="https://github.com/acme/api", created_at=at("2024-03-01T10:00:00Z"),
        updated_at=at("2026-10-19T18:40:00Z"), version="2026-10-19T18:40:00Z",
        meta={"default_branch": "main", "language": "Python", "visibility": "private"},
        acl=ORG, fetched_at=fetched))

    issue_text = ("SAML login breaks for customers behind the new IdP proxy. "
                  "We should evaluate OIDC before the Q4 auth migration.")
    w.add_item(item(
        run_id, id=ids.github_issue(REPO_ID, 398), aliases=["acme/api#398"], kind="issue", source="github",
        container=REPO, title="SAML login fails behind IdP proxy", text=issue_text,
        url="https://github.com/acme/api/issues/398", created_at=at("2026-09-30T09:12:00Z"),
        updated_at=at("2026-10-14T16:05:00Z"), version="2026-10-14T16:05:00Z",
        people=[Person(id=ids.person("github", "priya-r"), source="github", handle="priya-r",
                       name="Priya Rao", roles=["author"])],
        meta={"state": "closed", "labels": ["auth", "bug"]}, acl=ORG, fetched_at=fetched))

    # Review comments fold into the PR's text. The pasted token shows what
    # redaction leaves behind.
    raw_pr = ("Replaces SAML with OIDC for staff login. Fixes #398.\n\n"
              "Rollout plan in https://docs.google.com/document/d/1AbCdEfGh/edit\n\n"
              "--- review: priya-r ---\n"
              "Looks good. Don't forget to rotate the test client: " + PLANTED_TOKEN)
    pr_text, pr_redactions = redact(raw_pr)
    w.add_item(item(
        run_id, id=ids.github_pr(REPO_ID, 412), aliases=["acme/api#412"], kind="pull_request", source="github",
        container=REPO, title="Switch staff login to OIDC", text=pr_text,
        url="https://github.com/acme/api/pull/412", created_at=at("2026-10-12T11:30:00Z"),
        updated_at=at("2026-10-14T16:05:00Z"), version="2026-10-14T16:05:00Z",
        people=[sam_gh, priya_gh],
        refs=[Ref(type="closes", target=ids.github_issue(REPO_ID, 398), via="closing_keyword"),
              Ref(type="references", target=ids.gdrive_file("1AbCdEfGh"), via="url")],
        meta={"state": "merged", "base": "main", "head": "slee/oidc", "merged_at": "2026-10-14T16:05:00Z",
              "additions": 412, "deletions": 188},
        acl=ORG, redactions=pr_redactions, fetched_at=fetched))

    sha = "3f9c2e1a7b4d5f60718293a4b5c6d7e8f9012345"
    w.add_item(item(
        run_id, id=ids.github_commit(REPO_ID, sha), aliases=[sha[:7]], kind="commit", source="github",
        container=REPO, title="Switch staff login to OIDC (#412)",
        text="Switch staff login to OIDC (#412)\n\nFixes #398.",
        url=f"https://github.com/acme/api/commit/{sha}", created_at=at("2026-10-14T16:05:00Z"),
        updated_at=at("2026-10-14T16:05:00Z"), version=sha,
        people=[Person(id=ids.person("github", "slee"), source="github", handle="slee", name="Sam Lee",
                       email="sam@acme.com", roles=["author"])],
        refs=[Ref(type="implements", target=ids.github_issue(REPO_ID, 398), via="closing_keyword"),
              Ref(type="references", target=ids.github_pr(REPO_ID, 412), via="key")],
        meta={"files": ["api/auth/oidc.py", "api/auth/saml.py"], "branch": "main"},
        acl=ORG, fetched_at=fetched))

    # Who READERS is: the repo's collaborators with a known email. One has none.
    w.add(GroupRecord(run_id=run_id, id=READERS, source="github", name="acme/api readers",
                      members=["user:sam@acme.com", "user:priya@acme.com"], unresolved=1,
                      observed_at=at("2026-10-20T03:00:40Z")))

    finish_run(w, started_at=at("2026-10-20T03:00:00Z"), finished_at=at("2026-10-20T03:04:37Z"))


# --------------------------------------------------------------------------- #
# Slack: a channel, a decision thread, one extraction, and two change records
# --------------------------------------------------------------------------- #

TEAM = "T01ACME"
CHANNEL = ids.slack_channel(TEAM, "C42AUTH")
MEMBERS_GROUP = f"group:{CHANNEL}:members"
MEMBERS = Acl(visibility="restricted", principals=[MEMBERS_GROUP])


def slack_run(store: LocalStore) -> None:
    run_id = "2026-10-20T03:00:00Z-slack"
    fetched = at("2026-10-20T03:01:40Z")
    w = RunWriter(store, "slack", run_id)

    w.add_item(item(
        run_id, id=CHANNEL, aliases=["#eng-auth"], kind="channel", source="slack", container=None,
        title="#eng-auth", text="Auth migration: OIDC rollout, IdP proxy issues.",
        url="https://acme.slack.com/archives/C42AUTH", created_at=at("2025-01-08T15:00:00Z"),
        updated_at=at("2026-10-15T10:20:00Z"), version="2026-10-15T10:20:00Z",
        meta={"is_private": True, "member_count": 9}, acl=MEMBERS, fetched_at=fetched))

    thread_ts = "1760450400.000100"
    text = ("Sam Lee: SAML keeps breaking behind the IdP proxy. Proposal: we use OIDC instead of SAML for staff login.\n"
            "Priya Rao: Agreed. acme/api#412 already has the change.\n"
            "Sam Lee: Decided then. I'll draft the rollout plan by Friday.")
    sam = Person(id=ids.person("slack", "U123SAM"), source="slack", handle="U123SAM", name="Sam Lee",
                 email="sam@acme.com", roles=["author", "participant"])
    priya = Person(id=ids.person("slack", "U456PRI"), source="slack", handle="U456PRI", name="Priya Rao",
                   email="priya@acme.com", roles=["participant"])
    thread = item(
        run_id, id=ids.slack_thread(TEAM, "C42AUTH", thread_ts), aliases=[
            "https://acme.slack.com/archives/C42AUTH/p1760450400000100"],
        kind="thread", source="slack", container=CHANNEL, title="SAML keeps breaking behind the IdP proxy",
        text=text, url="https://acme.slack.com/archives/C42AUTH/p1760450400000100",
        created_at=at("2026-10-14T14:00:00Z"), updated_at=at("2026-10-14T14:09:30Z"),
        version="1760450970.000300", people=[sam, priya],
        refs=[Ref(type="references", target=ids.github_pr(REPO_ID, 412), via="key")],
        meta={"message_count": 3, "reply_count": 2, "grouping": "thread"},
        acl=MEMBERS, fetched_at=fetched)
    w.add_item(thread)

    decision = "we use OIDC instead of SAML for staff login"
    action = "I'll draft the rollout plan by Friday"
    w.add_extraction(ExtractionRecord(
        run_id=run_id, item_id=thread.id, item_version=thread.version, source="slack",
        extractor=Extractor(model="hand-written", prompt_version="sample-v0"),
        entities=[
            Entity(local_id="e1", type="decision", name="Use OIDC instead of SAML for staff login",
                   evidence=[span(text, decision)]),
            Entity(local_id="e2", type="action_item", name="Sam to draft the OIDC rollout plan",
                   attributes={"due": "Friday"}, evidence=[span(text, action)]),
            Entity(local_id="e3", type="project", name="Auth migration",
                   evidence=[span(text, "SAML keeps breaking behind the IdP proxy")]),
        ],
        relations=[
            Relation(type="decided_in", source="e1", target=thread.id, confidence=0.95,
                     evidence=[span(text, "Decided then.")]),
            Relation(type="decided_by", source="e1", target=sam.id, confidence=0.8,
                     evidence=[span(text, "Sam Lee: Decided then.")]),
            Relation(type="assigned_to", source="e2", target=sam.id, confidence=0.9,
                     evidence=[span(text, action)]),
            Relation(type="part_of", source="e1", target="e3", confidence=0.6,
                     evidence=[span(text, decision)]),
        ],
        extracted_at=at("2026-10-20T03:03:05Z")))

    w.add(GroupRecord(run_id=run_id, id=MEMBERS_GROUP, source="slack", name="#eng-auth members",
                      members=["user:sam@acme.com", "user:priya@acme.com"],
                      observed_at=at("2026-10-20T03:00:20Z")))
    ops_group = f"group:{ids.slack_channel(TEAM, 'C77OPS')}:members"
    w.add(GroupRecord(run_id=run_id, id=ops_group, source="slack", name="#ops members",
                      members=["user:sam@acme.com"], observed_at=at("2026-10-20T03:00:21Z")))
    w.add(TombstoneRecord(run_id=run_id, id=ids.slack_thread(TEAM, "C42AUTH", "1729450000.123400"),
                          source="slack", reason="deleted", detected_at=at("2026-10-20T03:03:40Z")))
    w.add(AclUpdateRecord(run_id=run_id, id=ids.slack_channel(TEAM, "C77OPS"), source="slack",
                          acl=Acl(visibility="restricted", principals=[ops_group]),
                          observed_at=at("2026-10-20T03:03:41Z")))

    finish_run(w, started_at=at("2026-10-20T03:00:00Z"), finished_at=at("2026-10-20T03:03:52Z"),
               extractor_versions=["sample-v0"])


# --------------------------------------------------------------------------- #
# Identity: one email match and one LLM match
# --------------------------------------------------------------------------- #

def identity_run(store: LocalStore) -> None:
    run_id = "2026-10-20T04:00:00Z-identity"
    w = RunWriter(store, "identity", run_id)
    w.add(IdentityRecord(
        run_id=run_id, person_id=ids.person_cluster("7f3a91"),
        members=[IdentityMember(id=ids.person("slack", "U123SAM"), source="slack", handle="U123SAM", email="sam@acme.com", name="Sam Lee"),
                 IdentityMember(id=ids.person("github", "slee"), source="github", handle="slee", email="sam@acme.com", name="Sam Lee")],
        method="email", confidence=1.0, evidence="Slack profile email equals the commit email"))
    w.add(IdentityRecord(
        run_id=run_id, person_id=ids.person_cluster("b20c44"),
        members=[IdentityMember(id=ids.person("slack", "U456PRI"), source="slack", handle="U456PRI", email="priya@acme.com", name="Priya Rao"),
                 IdentityMember(id=ids.person("github", "priya-r"), source="github", handle="priya-r", email=None, name="Priya Rao")],
        method="llm", confidence=0.82,
        evidence="GitHub profile name matches the Slack display name; both review the same PR"))
    finish_run(w, started_at=at("2026-10-20T04:00:00Z"), finished_at=at("2026-10-20T04:00:09Z"))


def build(out: Path = OUT) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store = LocalStore(tmp)
        github_run(store)
        slack_run(store)
        identity_run(store)
        target = out / "runs"
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(Path(tmp) / "runs", target)


if __name__ == "__main__":
    build()
    print(f"wrote {OUT / 'runs'}")
