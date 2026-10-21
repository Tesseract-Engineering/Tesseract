"""Record schema v2: what the contract admits and what it refuses."""

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from swarm import ids, ontology
from swarm.records import (
    Acl,
    AclUpdateRecord,
    Entity,
    ExtractionRecord,
    Extractor,
    IdentityMember,
    IdentityRecord,
    ItemRecord,
    Manifest,
    Person,
    Ref,
    Relation,
    Span,
    content_hash,
    dump_line,
    parse_line,
)

NOW = datetime(2026, 10, 20, 3, 0, tzinfo=timezone.utc)
REPO = ids.github_repo(7001)
ACL = Acl(visibility="restricted", principals=["group:github:acme:repo:7001:readers"])


def make_item(**overrides) -> ItemRecord:
    fields = dict(id=ids.github_issue(7001, 1), kind="issue", source="github", container=REPO,
                  title="t", text="body", url="https://github.com/acme/api/issues/1",
                  created_at=NOW, updated_at=NOW, version="v1", people=[], refs=[], meta={},
                  acl=ACL, fetched_at=NOW, run_id="r1")
    fields.update(overrides)
    fields.setdefault("content_hash", content_hash(
        title=fields["title"], text=fields["text"], container=fields["container"],
        people=fields["people"], refs=fields["refs"], meta=fields["meta"]))
    return ItemRecord(**fields)


def test_ontology_matches_the_design_doc():
    onto = ontology.load()
    assert onto.version == 1
    assert len(onto.entities) == 15 and len(onto.relations) == 16
    assert onto.item_backed == {"repo", "commit", "pull_request", "issue", "thread", "channel", "document", "folder"}
    assert onto.llm_entities == {"team", "project", "decision", "system", "action_item", "customer"}
    assert onto.relations - onto.deterministic_relations == {"owns", "works_on", "decided_by", "decided_in",
                                                             "supersedes"}


def test_item_round_trips_through_a_jsonl_line():
    item = make_item(refs=[Ref(type="closes", target=ids.github_issue(7001, 2), via="closing_keyword")])
    assert parse_line(dump_line(item)) == item


def test_item_without_acl_is_refused():
    with pytest.raises(ValidationError, match="acl"):
        make_item(acl=None)


def test_restricted_acl_must_name_a_principal():
    with pytest.raises(ValidationError, match="at least one principal"):
        Acl(visibility="restricted")
    with pytest.raises(ValidationError):
        Acl(visibility="restricted", principals=["sam@acme.com"])  # no user:/group:/domain: prefix


def test_content_hash_must_match_and_ignores_acl():
    item = make_item()
    with pytest.raises(ValidationError, match="content_hash"):
        make_item(content_hash="sha256:" + "0" * 64)
    assert make_item(acl=Acl(visibility="internal")).content_hash == item.content_hash
    assert make_item(text="changed").content_hash != item.content_hash


def test_ids_must_match_the_formats_and_the_source():
    with pytest.raises(ValidationError, match="ID format"):
        make_item(id="github:acme/api#1")
    with pytest.raises(ValidationError, match="does not belong"):
        make_item(id=ids.jira_issue("acme", 10001))
    with pytest.raises(ValidationError, match="ref target"):
        Ref(type="references", target="NEXP-318", via="key")


def test_refs_are_deterministic_relations_only():
    with pytest.raises(ValidationError, match="LLM-only"):
        Ref(type="owns", target=REPO, via="field")
    with pytest.raises(ValidationError, match="not in ontology"):
        Ref(type="relates_to", target=REPO, via="field")


def test_person_roles_and_ids():
    with pytest.raises(ValidationError, match="unknown person roles"):
        Person(id="person:github:slee", source="github", handle="slee", roles=["boss"])
    with pytest.raises(ValidationError, match="person:"):
        Person(id="person:c:7f3a91", source="github", handle="slee", roles=["author"])


def test_extraction_vocabulary_is_closed():
    span = Span(start=0, end=4, quote="body")
    with pytest.raises(ValidationError, match="never from the LLM"):
        Entity(local_id="e1", type="pull_request", name="x", evidence=[span])
    with pytest.raises(ValidationError, match="not in ontology"):
        Entity(local_id="e1", type="meeting", name="x", evidence=[span])
    with pytest.raises(ValidationError, match="not an LLM relation"):
        Relation(type="closes", source="e1", target="e2", confidence=0.5, evidence=[span])


def test_extraction_endpoints_resolve():
    span = Span(start=0, end=4, quote="body")
    common = dict(run_id="r1", item_id=ids.github_issue(7001, 1), item_version="v1", source="github",
                  extractor=Extractor(model="m", prompt_version="p1"), extracted_at=NOW,
                  entities=[Entity(local_id="e1", type="decision", name="d", evidence=[span])])
    ExtractionRecord(**common, relations=[
        Relation(type="decided_in", source="e1", target=ids.github_issue(7001, 1), confidence=0.9, evidence=[span])])
    with pytest.raises(ValidationError, match="neither a local_id"):
        ExtractionRecord(**common, relations=[
            Relation(type="decided_in", source="e1", target="e9", confidence=0.9, evidence=[span])])


def test_span_length_matches_quote():
    with pytest.raises(ValidationError, match="length"):
        Span(start=0, end=10, quote="body")


def test_identity_and_change_records():
    IdentityRecord(run_id="r", person_id="person:c:7f3a91", method="email", confidence=1.0,
                   members=[IdentityMember(source="slack", handle="U1")])
    with pytest.raises(ValidationError, match="person:c:"):
        IdentityRecord(run_id="r", person_id="person:slack:U1", method="email", confidence=1.0,
                       members=[IdentityMember(source="slack", handle="U1")])
    line = '{"schema_version": "2", "record_type": "acl_update", "id": "gdrive:file:1AbC", "source": "gdrive", ' \
           '"acl": {"visibility": "restricted", "principals": ["user:sam@acme.com"]}, ' \
           '"observed_at": "2026-10-25T03:12:00Z", "run_id": "x"}'
    assert isinstance(parse_line(line), AclUpdateRecord)


def test_unknown_fields_and_versions_are_refused():
    with pytest.raises(ValidationError):
        parse_line('{"schema_version": "1", "record_type": "tombstone", "id": "gdrive:file:1", "source": "gdrive", '
                   '"reason": "deleted", "detected_at": "2026-10-25T03:10:00Z", "run_id": "x"}')
    with pytest.raises(ValidationError):
        parse_line('{"schema_version": "2", "record_type": "tombstone", "id": "gdrive:file:1", "source": "gdrive", '
                   '"reason": "deleted", "detected_at": "2026-10-25T03:10:00Z", "run_id": "x", "extra": 1}')


def test_manifest_times_are_ordered():
    with pytest.raises(ValidationError, match="before"):
        Manifest(source="github", run_id="r", started_at=NOW, finished_at=NOW.replace(hour=2),
                 counts={"items": 0}, files=[])
