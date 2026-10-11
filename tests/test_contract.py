"""What the door must refuse, and what it must let through."""

from tesseract.contract import EdgeProposal, NodeProposal, Provenance, content_hash, is_external, validate

PROV = Provenance(source="jira", source_id="issue:1", producer="rule:test", confidence=0.9, quote="q")


def edge(type_="MENTIONS", src_type="Issue", dst_type="Service", props=None, provenance=PROV):
    return EdgeProposal(ns="t", type=type_, src_id="a", dst_id="b", src_type=src_type,
                        dst_type=dst_type, props=props or {}, provenance=provenance)


def test_known_node_and_edge_types_are_admitted():
    assert validate(NodeProposal(ns="t", type="Issue", id="issue:1", props={"title": "t"}, provenance=PROV)) is None
    assert validate(edge()) is None
    assert validate(edge("TOUCHED", "Commit", "Module")) is None


def test_external_types_can_be_linked_but_not_created():
    assert is_external("Module")
    refusal = validate(NodeProposal(ns="t", type="Module", id="m", props={"name": "m"}, provenance=PROV))
    assert refusal and "external" in refusal


def test_edge_without_provenance_is_refused():
    assert validate(edge(provenance=None)) == "edge without provenance is inadmissible"


def test_node_without_namespace_is_refused():
    assert validate(NodeProposal(ns="", type="Issue", id="i", props={"title": "t"})) == "missing namespace"


def test_wrong_endpoint_and_invented_label_are_refused():
    assert "cannot start at" in (validate(edge("TOUCHED", "Issue", "Module")) or "")
    assert validate(edge(dst_type="Sprocket")) is not None


def test_required_property_is_enforced():
    assert "requires ['name']" in (validate(NodeProposal(ns="t", type="Team", id="team:x", props={})) or "")


def test_confidence_outside_unit_interval_is_refused():
    hot = Provenance(source="x", source_id="y", producer="z", confidence=1.5)
    assert "outside [0,1]" in (validate(edge(provenance=hot)) or "")


def test_hash_ignores_provenance_tracks_meaning_and_includes_namespace():
    a = edge(props={"count": 2}, provenance=Provenance(source="github", source_id="1", producer="p"))
    b = edge(props={"count": 2}, provenance=Provenance(source="jira", source_id="2", producer="q", confidence=0.5))
    c = edge(props={"count": 3}, provenance=Provenance(source="github", source_id="1", producer="p"))
    assert content_hash(a) == content_hash(b)
    assert content_hash(a) != content_hash(c)
    d = NodeProposal(ns="other", type="Issue", id="i", props={"title": "t"})
    e = NodeProposal(ns="t", type="Issue", id="i", props={"title": "t"})
    assert content_hash(d) != content_hash(e)
