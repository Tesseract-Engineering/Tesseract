"""
redact.py - strip secrets in memory, before anything touches disk.

Uses detect-secrets' pattern detectors (AWS keys, GitHub, Slack and Stripe
tokens, JWTs, private keys and the rest), not its entropy or keyword
heuristics: those flag ordinary prose such as commit SHAs and would shred the
text the extractor needs. A few patterns detect-secrets lacks are added here.
A pattern with a group named `secret` replaces only that group, so the label
around a value ("AWS_SECRET_ACCESS_KEY=") stays readable.

What was stripped is recorded as Redaction entries (field, detector, count),
never the value.
"""

from __future__ import annotations

import re
from collections import Counter
from functools import lru_cache

from ..records import Redaction

# Whole PEM blocks, not just the BEGIN line detect-secrets matches.
_PEM = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)", re.S)

_EXTRA: dict[str, list[re.Pattern[str]]] = {
    "Anthropic API Key": [re.compile(r"\bsk-ant-[A-Za-z0-9_\-]{20,}")],
    "Google API Key": [re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")],
    "GitHub Fine-Grained Token": [re.compile(r"\bgithub_pat_[A-Za-z0-9_]{22,}")],
    # detect-secrets knows only the legacy sk-...T3BlbkFJ shape and .npmrc lines.
    "OpenAI Token": [re.compile(r"\bsk-(?:proj|svcacct|admin)-[A-Za-z0-9_\-]{20,}")],
    "NPM Token": [re.compile(r"\bnpm_[A-Za-z0-9]{36}\b")],
    # detect-secrets matches a secret key only in quotes; .env files and shell exports leave them bare.
    "AWS Secret Access Key": [re.compile(
        r"(?i)\baws[\w.-]{0,20}?(?:secret|key)[\w.-]{0,20}?\s*[=:]\s*['\"]?(?P<secret>[A-Za-z0-9/+]{40})(?![A-Za-z0-9/+])")],
}


@lru_cache(maxsize=1)
def _detectors() -> list[tuple[str, re.Pattern[str]]]:
    from detect_secrets.core.plugins.util import get_mapping_from_secret_type_to_class
    from detect_secrets.plugins.base import RegexBasedDetector

    found: list[tuple[str, re.Pattern[str]]] = [("Private Key", _PEM)]
    for name, cls in sorted(get_mapping_from_secret_type_to_class().items()):
        if issubclass(cls, RegexBasedDetector) and name != "Private Key":
            found.extend((name, rx) for rx in cls().denylist)
    for name, patterns in _EXTRA.items():
        found.extend((name, rx) for rx in patterns)
    return found


def redact(text: str, field: str = "text") -> tuple[str, list[Redaction]]:
    """Return text with every secret replaced by [REDACTED:<detector>], and
    what was replaced."""
    if not text:
        return text, []
    counts: Counter[str] = Counter()
    for name, pattern in _detectors():
        def mark(match: re.Match[str], name: str = name) -> str:
            if match.group(0).startswith("[REDACTED:"):
                return match.group(0)
            counts[name] += 1
            if "secret" in match.re.groupindex:
                start, end = match.span("secret")
                whole = match.group(0)
                return whole[:start - match.start()] + f"[REDACTED:{name}]" + whole[end - match.start():]
            return f"[REDACTED:{name}]"
        text = pattern.sub(mark, text)
    return text, [Redaction(field=field, type=name, count=n) for name, n in sorted(counts.items())]


def redact_fields(values: dict[str, str]) -> tuple[dict[str, str], list[Redaction]]:
    """Redact several named fields of one item at once."""
    clean: dict[str, str] = {}
    redactions: list[Redaction] = []
    for name, value in values.items():
        clean[name], found = redact(value, field=name)
        redactions.extend(found)
    return clean, redactions


_MARKER = re.compile(r"\[REDACTED:([^\]]+)\]")


def redact_payload(payload):
    """Redact every string inside a raw source payload (dicts, lists, scalars),
    before it is kept under raw/ or normalized. Returns (clean, count)."""
    total = 0

    def walk(value):
        nonlocal total
        if isinstance(value, str):
            clean, found = redact(value)
            total += sum(r.count for r in found)
            return clean
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        return value

    return walk(payload), total


def redactions_in(values: dict[str, str]) -> list[Redaction]:
    """The Redaction entries for fields whose text was already redacted
    upstream (redact_payload), read back from the markers it left."""
    out: list[Redaction] = []
    for name, value in values.items():
        counts = Counter(_MARKER.findall(value or ""))
        out.extend(Redaction(field=name, type=kind, count=n) for kind, n in sorted(counts.items()))
    return out
