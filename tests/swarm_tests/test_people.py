"""Finding the people a text names, and offering them to the extractor."""

from datetime import datetime, timezone

from swarm.people import Directory
from swarm.records import Acl, ItemRecord, Person, content_hash
from swarm.stages.extract import build_record, known_ids, message

ISHAN, DHRUV, SAM_A, SAM_B = "person:c:aaaaaa", "person:c:bbbbbb", "person:c:cccccc", "person:c:dddddd"
PEOPLE = Directory({
    ISHAN: {"names": ["Ishan Sinha", "ishan.tesseract"], "members": ["person:slack:U1", "person:gdrive:ishan@x.dev"]},
    DHRUV: {"names": ["Dhruv Pendharkar", "D. Pendharkar"], "members": ["person:slack:U2"]},
    SAM_A: {"names": ["Sam Lee"], "members": ["person:slack:U3"]},
    SAM_B: {"names": ["Sam Ortiz", "sam@x.dev"], "members": ["person:slack:U4"]},
})


def test_full_names_any_case_and_initials_with_surnames():
    assert PEOPLE.named_in("ping ishan sinha and D. Pendharkar") == [(ISHAN, "ishan sinha"), (DHRUV, "D. Pendharkar")]
    assert PEOPLE.named_in("cc D Pendharkar") == [(DHRUV, "D Pendharkar")]


def test_a_first_name_alone_counts_only_when_one_person_has_it():
    assert PEOPLE.named_in("Ishan to write up the options.") == [(ISHAN, "Ishan")]
    assert PEOPLE.named_in("Sam will take it") == []                     # two Sams: not guessed
    assert PEOPLE.named_in("Sam Ortiz will take it") == [(SAM_B, "Sam Ortiz")]


def test_lowercase_words_emails_and_handles_are_not_names():
    assert PEOPLE.named_in("mail ishan.tesseract or sam@x.dev; the dhruv fork") == []
    assert PEOPLE.person_of("person:gdrive:ishan@x.dev") == ISHAN


def item(text, people=()):
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    meta, refs, people = {}, [], list(people)
    return ItemRecord(id="gdrive:file:1SYNC", kind="document", source="gdrive", container="gdrive:file:1ROOT",
                      title="Platform Weekly Sync Notes", text=text, url="https://drive.google.com/file/d/1SYNC",
                      created_at=now, updated_at=now, version="1", people=people, refs=refs, meta=meta,
                      acl=Acl(visibility="public"), fetched_at=now, run_id="r",
                      content_hash=content_hash(title="Platform Weekly Sync Notes", text=text,
                                                container="gdrive:file:1ROOT", people=people, refs=refs, meta=meta))


OWNER = Person(id="person:gdrive:ishan@x.dev", source="gdrive", handle="ishan@x.dev", name="Ishan Sinha",
               roles=["owner"])
NOTES = "Okta strips the claims; Ishan to write up options. Dhruv Pendharkar owns the postmortem."


def test_people_the_text_names_are_offered_by_their_resolved_id():
    known = known_ids(item(NOTES), PEOPLE)
    assert known[ISHAN] == "person: Ishan (named in the text)"
    assert known[DHRUV] == "person: Dhruv Pendharkar (named in the text)"
    assert f"- {DHRUV}  person: Dhruv Pendharkar (named in the text)" in message(item(NOTES), PEOPLE)
    assert DHRUV not in known_ids(item(NOTES))                           # no directory before the first identity pass


def test_someone_already_on_the_item_is_not_offered_twice():
    known = known_ids(item(NOTES, [OWNER]), PEOPLE)
    assert OWNER.id in known and ISHAN not in known and DHRUV in known


def test_an_action_item_links_to_a_person_only_the_text_names():
    answer = {"entities": [{"id": "e1", "type": "action_item", "name": "Write up the claims options",
                            "attributes": [], "evidence": ["Ishan to write up options"]}],
              "relations": [{"type": "assigned_to", "source": "e1", "target": ISHAN, "confidence": 0.9,
                             "evidence": ["Ishan to write up options"]}]}
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)
    record, dropped = build_record(item(NOTES), answer, model="m", prompt_version="document-v4", run_id="r",
                                   now=now, people=PEOPLE)
    assert [(r.type, r.target) for r in record.relations] == [("assigned_to", ISHAN)] and not dropped
    without, dropped = build_record(item(NOTES), answer, model="m", prompt_version="document-v4", run_id="r", now=now)
    assert without.relations == [] and dropped["relation_endpoint"] == 1   # an id it was not given
