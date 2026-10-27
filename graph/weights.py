"""
weights.py - every number the swarm build puts on an edge, in one place.

Two numbers, never mixed:

  confidence  is this edge TRUE?  0..1, goes in the edge's provenance like
              every other producer's. A field the source itself filled is 1.0;
              a link spotted in prose is less, by how it was spotted.
  weight      how STRONG is it?   the role's strength. Ranks "who knows about
              X" and "what matters here"; says nothing about truth. It does not
              fade with age: old work counts the same as new.

The swarm sends raw signals (roles, `via`, timestamps) and no weights, on
purpose: these values are first guesses, and tuning them against an answer key
(the Tesseract sandbox's storyline.md) must not need the swarm to re-run.
Change a number here, rebuild, re-score.
"""

from __future__ import annotations

# How a ref was found (swarm `refs[].via`) -> confidence the link is real.
# A field is a link someone set in the tool itself (Jira's "duplicates",
# "blocks", parent); a closing keyword is a deliberate statement; a URL is unambiguous; a bare
# `#12` could be another repo's #12; a channel mention is the loosest.
VIA_CONFIDENCE = {
    "field": 1.00,
    "closing_keyword": 0.95,
    "url": 0.90,
    "key": 0.80,
    "mention": 0.70,
}
UNKNOWN_VIA_CONFIDENCE = 0.60

# Person role on an item -> strength of the tie. Authoring or owning a thing is
# most of what makes someone the person to ask; being @-mentioned is little.
ROLE_STRENGTH = {
    "author": 1.0,
    "reporter": 1.0,
    "owner": 0.8,
    "reviewer": 0.7,
    "assignee": 0.6,
    "commenter": 0.4,
    "participant": 0.4,
    "mentioned": 0.2,
}

# How much a source is trusted when a model reads it. Order from Karthik's design doc:
# code > org data > docs > tickets > chat.
SOURCE_AUTHORITY = {"github": 1.0, "gdrive": 0.9, "jira": 0.8, "slack": 0.7}

def ref_confidence(via: str | None) -> float:
    return VIA_CONFIDENCE.get(via or "", UNKNOWN_VIA_CONFIDENCE)


def role_weight(role: str) -> float:
    """Weight of one person-role-item tie."""
    return ROLE_STRENGTH.get(role, 0.2)


def corroborate(confidences: list[float]) -> float:
    """Noisy-or: the same fact from several independent items. Two 0.7 sightings
    make 0.91, not 1.4 and not 0.7."""
    miss = 1.0
    for c in confidences:
        miss *= 1.0 - c
    return round(1.0 - miss, 4)


def extraction_confidence(model_confidence: float, source: str) -> float:
    """An extracted relation: the model's own 0..1 score times how far its source is trusted.
    The swarm sends no explicit/implicit/inferred label (asked for), so the model's score is the
    only per-relation signal; the quote has already been checked against the text."""
    return round(min(max(model_confidence, 0.0), 1.0) * SOURCE_AUTHORITY.get(source, 0.7), 4)
