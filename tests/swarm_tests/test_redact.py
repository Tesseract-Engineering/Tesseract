"""Redaction strips planted secrets and leaves ordinary prose alone. The full
planted-token corpus is Phase 1 work; these are the shapes that matter first.
Tokens are assembled at runtime so the repo holds no scanner-shaped literal."""

import pytest

from swarm.stages.redact import redact, redact_fields

PLANTED = {
    "GitHub Token": "ghp" + "_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8",
    "Slack Token": "xoxb" + "-123456789012-1234567890123-abcdefghijklmnopqrstuvwx",
    "AWS Access Key": "AKIA" + "IOSFODNN7EXAMPLE",
    "Anthropic API Key": "sk-ant" + "-api03-abcdefghijklmnopqrstuvwxyz0123",
}


@pytest.mark.parametrize(("kind", "secret"), PLANTED.items())
def test_planted_secret_is_removed(kind, secret):
    clean, found = redact(f"before {secret} after")
    assert secret not in clean
    assert clean == f"before [REDACTED:{kind}] after"
    assert [(r.type, r.count) for r in found] == [(kind, 1)]


def test_private_key_block_is_removed_whole():
    pem = "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEow\nabc\n-----END RSA PRIVATE KEY-----"
    clean, found = redact(f"key:\n{pem}\ndone")
    assert "MIIEow" not in clean and clean == "key:\n[REDACTED:Private Key]\ndone"
    assert found[0].type == "Private Key"


def test_prose_shas_and_urls_survive():
    text = ("Merged 3f9c2e1a7b4d5f60718293a4b5c6d7e8f9012345 into main; see "
            "https://github.com/acme/api/pull/412 and NEXP-318.")
    assert redact(text) == (text, [])


def test_redact_fields_records_which_field():
    clean, found = redact_fields({"title": "ok", "text": "t " + PLANTED["GitHub Token"]})
    assert clean["title"] == "ok"
    assert [(r.field, r.type) for r in found] == [("text", "GitHub Token")]


def test_an_unquoted_aws_secret_keeps_its_label():
    # The value comes from the corpus, joined at runtime, so this file holds no scanner-shaped literal.
    import yaml

    from .github_fixture import CORPUS
    parts = yaml.safe_load(CORPUS.read_text(encoding="utf-8"))["positives"]["aws_secret_unquoted"]["parts"]
    value = "".join(parts[2:])
    # Quoted, detect-secrets' own AWS pattern already catches it; bare, as in .env files, it did not.
    clean, found = redact(f"Staging creds:\nAWS_SECRET_ACCESS_KEY={value}\naws.secret-key : {value} (rotate)")
    assert clean == ("Staging creds:\nAWS_SECRET_ACCESS_KEY=[REDACTED:AWS Secret Access Key]\n"
                     "aws.secret-key : [REDACTED:AWS Secret Access Key] (rotate)")
    assert [(r.type, r.count) for r in found] == [("AWS Secret Access Key", 2)]
