"""
matching.py - finding known entities named in prose. Company-agnostic.

Everything here works from a VOCABULARY the adapter supplies (service names,
people, customers) and never from knowledge of any particular company. An adapter
says what the services and people are called, and this module finds them.

  service_alias_map / alias_pattern / service_mentions
      canonical service names -> generated aliases -> MENTIONS with a quote
  build_person_vocabulary / person_pattern / person_mentions
      only aliases that identify ONE person: full name, email, @handle
  document_credits
      who a document's header says wrote / owns / updated it

The one layout-dependent input is `speaker_sources`: the sources whose text
records discussion as "Name: text" lines (chat threads, ticket comments). The
adapter declares them; the speaker is masked there, because speaking is
authorship, already its own edge, and not a mention.
"""

from __future__ import annotations

import re

from .connectors import SourceRecord

# How much of the canonical name an alias keeps, and therefore how much it is
# worth as evidence. These sit either side of the 0.9 routing gate on purpose.
ALIAS_EXACT = 1.0        # 'billing-service'
ALIAS_VARIANT = 0.95     # 'billing service', 'billing_service', 'BillingService'
ALIAS_STEM = 0.7         # 'billing' - a word, in a company that bills people


def service_aliases(canonical: str) -> dict[str, float]:
    """Generated, not listed. A listed alias table is a record of the misses
    you already found; a generator covers the ones you have not."""
    out: dict[str, float] = {canonical: ALIAS_EXACT}
    parts = canonical.split("-")

    out.setdefault(" ".join(parts), ALIAS_VARIANT)
    out.setdefault("_".join(parts), ALIAS_VARIANT)
    out.setdefault("".join(w.capitalize() for w in parts), ALIAS_VARIANT)

    # 'billing-service' -> 'billing'. Only when dropping the suffix leaves
    # something, and only ever as weak evidence.
    for suffix in ("service", "engine", "server", "platform", "sync-service"):
        if canonical.endswith("-" + suffix):
            stem = canonical[: -(len(suffix) + 1)]
            # Two letters is an abbreviation, not a name: 'ml-platform' made
            # every "ML" in the company a mention of one component (53 of
            # them). Three survives, so 'k8s-platform' keeps 'k8s'.
            if len(stem) >= 3:
                out.setdefault(stem, ALIAS_STEM)
                out.setdefault(stem.capitalize(), ALIAS_STEM)
                out.setdefault(stem + " service", ALIAS_VARIANT)
    return out


def service_alias_map(services: dict[str, dict]) -> dict[str, tuple[str, float]]:
    """alias -> (service id, confidence) for every service, generated from
    its canonical name. Longest alias wins when two overlap, so
    'inventory-sync-service' is not read as the service 'inventory-sync'
    plus a stray word."""
    alias_map: dict[str, tuple[str, float]] = {}
    for name, service in services.items():
        for alias, confidence in service_aliases(name).items():
            key = alias.lower()
            # A stem that collides with another service's canonical name is
            # not an alias at all, it is that other service.
            if key in services and services[key]["id"] != service["id"]:
                continue
            existing = alias_map.get(key)
            if existing is None or confidence > existing[1]:
                alias_map[key] = (service["id"], confidence)
    return alias_map




def service_mentions(record: SourceRecord, alias_map: dict[str, tuple[str, float]],
                     alias_re: re.Pattern, stats=None) -> list[dict]:
    """Find services named in a record's title and body.

    Hyphenated names are why this is not extract.mentions(): every pattern
    there is built for Python identifiers, and `\\w` does not match a hyphen,
    so `billing-service` is invisible to all of them. A closed vocabulary
    deserves a matcher that knows it is closed.
    """
    found: dict[str, dict] = {}

    def scan(text: str, in_title: bool) -> None:
        if not text:
            return
        haystack = stats.mask(text, record.source) if stats is not None else text
        for match in alias_re.finditer(haystack):
            alias = match.group(0).lower()
            hit = alias_map.get(alias)
            if not hit:
                continue
            service_id, confidence = hit
            # A bare stem that is one of the corpus's commonest words is not
            # a reference to anything. `data-platform` generates the stem
            # "data", and in a data company that matched 42 times and made
            # the data platform look like the second most discussed system
            # here. Same rule as extract.py applies to bare code tokens, same
            # statistic, asked of the same corpus.
            if (stats is not None and confidence <= ALIAS_STEM
                    and "-" not in alias and " " not in alias
                    and stats.too_common(alias)):
                continue
            window = " ".join(text[max(0, match.start() - 70):match.end() + 70].split())
            entry = found.get(service_id)
            if entry is None:
                found[service_id] = {
                    "targetId": service_id, "rel": "MENTIONS", "source": record.source,
                    "sourceId": record.id, "extractor": "rule:service-alias",
                    "confidence": confidence, "quote": window[:200], "count": 1,
                    "inTitle": in_title, "surface": match.group(0),
                }
                continue
            entry["count"] += 1
            entry["inTitle"] = entry["inTitle"] or in_title
            if confidence > entry["confidence"]:
                entry.update(confidence=confidence, quote=window[:200],
                             surface=match.group(0))

    scan(record.title or "", True)
    scan(record.text or "", False)
    return list(found.values())


def alias_pattern(alias_map: dict[str, tuple[str, float]]) -> re.Pattern:
    # Longest first so the alternation prefers the most specific alias.
    aliases = sorted(alias_map, key=len, reverse=True)
    return re.compile(r"(?<![\w-])(" + "|".join(re.escape(a) for a in aliases)
                      + r")(?![\w-])", re.IGNORECASE)


def build_person_vocabulary(people_rows: list[dict]) -> dict[str, tuple[str, float]]:
    """alias -> (full name, confidence), for finding people named in prose.

    Only forms that identify ONE person are used:
      full name        'Marcus Chen'               1.0
      email address    'marcus.chen@example.com'   1.0
      @handle          '@marcus.chen'              0.95

    First names and surnames are deliberately absent. This company has three
    Marcuses and two Chens, and "Marcus said" is a question for a human, not
    a fact. A bare handle without its @ is absent for the same reason as the
    stems in the service matcher: `chen` and `priya` and `jordan` are words
    long before they are references. Any alias two people share is dropped
    rather than guessed at.
    """
    candidates: dict[str, set[tuple[str, float]]] = {}

    def offer(alias: str, name: str, confidence: float) -> None:
        if alias:
            candidates.setdefault(alias.lower(), set()).add((name, confidence))

    for person in people_rows:
        name = person["name"]
        offer(name, name, 1.0)
        offer(person.get("email") or "", name, 1.0)
        if person.get("slack"):
            offer("@" + person["slack"], name, 0.95)

    out = {}
    for alias, owners in candidates.items():
        names = {n for n, _ in owners}
        if len(names) == 1:
            out[alias] = (names.pop(), max(c for _, c in owners))
    return out


def person_pattern(alias_map: dict[str, tuple[str, float]]) -> re.Pattern:
    aliases = sorted(alias_map, key=len, reverse=True)
    # Trailing guard refuses a following word character AND a dot-then-word,
    # so '@chen' does not match inside '@chen.zhang'.
    return re.compile(r"(?<![\w@.])(" + "|".join(re.escape(a) for a in aliases)
                      + r")(?![\w-]|\.\w)", re.IGNORECASE)


# "Name: text" at the start of a line is how a Slack thread records who
# SPOKE. That is authorship, and it is already an AUTHORED edge; reading it as
# a mention would make every participant "mentioned" in every thread they
# posted in, which is the same fact counted twice under a different name.
SPEAKER_PREFIX = re.compile(r"(?m)^[^:\n]{1,60}: ")


def person_mentions(record: SourceRecord, alias_map: dict[str, tuple[str, float]],
                    alias_re: re.Pattern, stats=None,
                    speaker_sources: tuple[str, ...] | set[str] = ()) -> list[dict]:
    """People named in a record's title and body. Same shape as
    service_mentions(), so both go through load.mentions() unchanged."""
    found: dict[str, dict] = {}

    def scan(text: str, in_title: bool) -> None:
        if not text:
            return
        haystack = stats.mask(text, record.source) if stats is not None else text
        # Tickets now carry comment threads in the same "Name: text" shape, so
        # the speaker of a comment is masked there too - they COMMENTED, which
        # is its own edge, and are not also "mentioned".
        if record.source in speaker_sources and not in_title:
            haystack = SPEAKER_PREFIX.sub(lambda m: " " * len(m.group(0)), haystack)
        for match in alias_re.finditer(haystack):
            hit = alias_map.get(match.group(0).lower())
            if not hit:
                continue
            name, confidence = hit
            window = " ".join(text[max(0, match.start() - 70):match.end() + 70].split())
            entry = found.get(name)
            if entry is None:
                found[name] = {
                    "personName": name, "rel": "MENTIONS", "source": record.source,
                    "sourceId": record.id, "extractor": "rule:person-alias",
                    "confidence": confidence, "quote": window[:200], "count": 1,
                    "inTitle": in_title, "surface": match.group(0),
                }
                continue
            entry["count"] += 1
            entry["inTitle"] = entry["inTitle"] or in_title
            if confidence > entry["confidence"]:
                entry.update(confidence=confidence, quote=window[:200],
                             surface=match.group(0))

    scan(record.title or "", True)
    scan(record.text or "", False)
    return list(found.values())


# Labels a document uses in its header to say who stands behind it, and the
# role each one means. Label text varies ("Authors", "Maintained by",
# "Last Updated") so matching is case-insensitive and the role is normalised.
ROLE_LABELS = {
    "author": "author", "authors": "author", "written by": "author",
    "drafted by": "author", "prepared by": "author",
    "owner": "owner", "owners": "owner", "document owner": "owner",
    "maintainer": "owner", "maintained by": "owner",
    "last updated": "last-updated", "last updated by": "last-updated",
    "primary contact": "contact",
    "incident commander": "incident-commander",
    "approved by": "approver", "reviewed by": "reviewer",
}
# Letter-boundaries, not \b. `_` is a regex word character, so in the italic
# header form "_Last updated: July 2026 by Diana Reeves_" there is no \b
# between "_" and "Last" - and every italic header in the corpus, including
# the employee handbook's, was silently skipped.
# Three emphasis styles appear in the corpus - **bold**, _italic_ and
# *italic* - and a header in any of them is still a header. The single-asterisk
# form was missed at first, which dropped Marcus Chen as author of the local
# dev setup guide.
ROLE_LINE = re.compile(
    r"(?:\*{1,2}|_)?(?<![A-Za-z])("
    + "|".join(sorted((re.escape(k) for k in ROLE_LABELS), key=len, reverse=True))
    + r")(?![A-Za-z])(?:\*{1,2}|_)?\s*:", re.IGNORECASE)


OWNERSHIP_HEADING = re.compile(r"\b(ownership|owners?|maintainers?|contacts?|authors?)\b",
                               re.IGNORECASE)


def document_credits(record: SourceRecord, alias_map: dict[str, tuple[str, float]],
                     alias_re: re.Pattern,
                     speaker_sources: tuple[str, ...] | set[str] = ()) -> list[dict]:
    """Who a document's HEADER says wrote, owns or updated it.

    Only the header counts - everything before the first second-level heading.
    That boundary matters: a postmortem's action-item table says "Owner: @lisa.n"
    for one follow-up task, and a roadmap lists "Owner: David Kim" per
    initiative. Those people own a line item, not the document, and reading
    the whole file would have made them its owners.

    A header line can name a team as well as people ("Owner: Data Team (David
    Kim, @david.kim)"); only the people are credited, and only through the same
    unambiguous aliases the mention extractor uses - full name, email or
    @handle - so "Owner: @chen" resolves and a bare "Chen" would not.
    """
    # Documents only, and not chat threads stored as documents: a thread has
    # no header, and "Owner: ..." in one is somebody talking.
    if record.kind != "document" or record.source in speaker_sources or not record.text:
        return []
    # The header, plus any section whose HEADING is about ownership - the
    # data-platform README declares its owner under "## Team & Ownership",
    # which describes the whole repository, not one item inside it.
    header = []
    in_header, in_ownership = True, False
    for line in record.text.splitlines():
        heading = re.match(r"^\s*#{2,}\s+(.*)", line)
        if heading:
            in_header = False
            in_ownership = bool(OWNERSHIP_HEADING.search(heading.group(1)))
            continue
        if in_header or in_ownership:
            header.append(line)

    out: dict[tuple[str, str], dict] = {}
    for line in header:
        labels = list(ROLE_LINE.finditer(line))
        for index, label in enumerate(labels):
            role = ROLE_LABELS[label.group(1).lower()]
            end = labels[index + 1].start() if index + 1 < len(labels) else len(line)
            segment = line[label.end():end]
            for match in alias_re.finditer(segment):
                hit = alias_map.get(match.group(0).lower())
                if not hit:
                    continue
                name = hit[0]
                out.setdefault((name, role), {
                    "personName": name, "role": role,
                    "quote": " ".join(line.split())[:200],
                    "surface": match.group(0),
                })
    return list(out.values())
