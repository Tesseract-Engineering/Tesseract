"""The planted-token corpus: every fake secret is caught, by the right
detector, and look-alike text survives."""

import pytest
import yaml

from swarm.stages.redact import redact, redact_payload, redactions_in

from .github_fixture import CORPUS

DATA = yaml.safe_load(CORPUS.read_text(encoding="utf-8"))
POSITIVES = {name: ("".join(spec["parts"]), spec["type"]) for name, spec in DATA["positives"].items()}


@pytest.mark.parametrize("name", sorted(POSITIVES))
def test_positive_is_redacted_by_its_detector(name):
    secret, kind = POSITIVES[name]
    for text in (secret, f"before {secret} after", f'{{"key": "{secret}"}}', f"line1\n  {secret}\nline3"):
        clean, found = redact(text)
        assert secret not in clean, f"{name} leaked in {text!r}"
        assert [(r.type, r.count) for r in found] == [(kind, 1)]


@pytest.mark.parametrize("text", DATA["negatives"])
def test_negative_survives(text):
    assert redact(text) == (text, [])


def test_payload_redaction_reaches_nested_strings_and_leaves_the_rest():
    secret = POSITIVES["slack_bot"][0]
    payload = {"id": 7, "ok": True, "body": f"token {secret}", "comments": [{"body": secret}, {"n": None}],
               "nested": {"deeper": [f"x {secret}"]}}
    clean, count = redact_payload(payload)
    assert count == 3 and secret not in repr(clean)
    assert clean["id"] == 7 and clean["ok"] is True and clean["comments"][1] == {"n": None}


def test_redactions_are_read_back_from_markers():
    clean, _ = redact(f"a {POSITIVES['aws_access_key'][0]} b {POSITIVES['aws_access_key'][0]}")
    assert [(r.field, r.type, r.count) for r in redactions_in({"title": "ok", "text": clean})] == [
        ("text", "AWS Access Key", 2)]
