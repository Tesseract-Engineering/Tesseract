"""
corpus.py - statistics over the whole record set, computed before extraction.

Two things a single record cannot know about itself:

  BOILERPLATE   a line that appears verbatim in a quarter of the corpus is a
                template, not prose. Nobody wrote it about this record.
  SPECIFICITY   a token that appears in a quarter of the corpus does not
                identify anything. Frequency is the opposite of reference.

Both were learned the expensive way. Django's pull-request template contains
the line "This PR targets the `main` branch."  The backtick rule read `main`,
resolved it by leaf name to django/contrib/admin/views/main.py, and stamped it
confidence 1.0 because the SYNTAX was unambiguous. 240 of 300 issues acquired a
mention of the admin changelist, 30% of every MENTIONS edge in the graph, and
every one of them sailed through auto_approve and into a domain.

The pre-existing defence was a hardcoded stoplist of English words that are
also function names in `requests` - "get", "post", "session". It was written
against one repository and silently protected nothing on the next one. A
stoplist is a list of the false positives you have already seen. These two
statistics are derived from whatever corpus you actually loaded, so they
transfer; the stoplist stays as a floor, not as the defence.

Masking, not deleting. Boilerplate is blanked with spaces so every offset in
the masked text still indexes the original. Extraction reads the masked copy
and quotes the original, which keeps `quote` verbatim from the source - a
provenance requirement, and one that stripping the lines would have broken.
"""

from __future__ import annotations

import re
from collections import Counter

# A line repeated in this share of a source's records is a template.
BOILERPLATE_SHARE = 0.25
# A bare token appearing in this share of records identifies nothing.
COMMON_TOKEN_SHARE = 0.15
# Below this many records the shares above are noise, so neither test fires.
MIN_RECORDS = 20

WORD = re.compile(r"[A-Za-z_][\w]*")
# Markdown checkboxes differ only by their state, so `- [x] foo` and `- [ ] foo`
# are the same template line filled in two ways.
CHECKBOX = re.compile(r"^(\s*[-*]\s*)\[[ xX]\]")


def _key(line: str) -> str:
    """Normalised form used to decide whether two lines are the same template."""
    return " ".join(CHECKBOX.sub(r"\1[ ]", line).split()).lower()


class CorpusStats:
    """Boilerplate lines and token document-frequency, measured over records.

    Boilerplate is counted PER SOURCE. A checklist that every GitHub PR carries
    says nothing about a commit message, and pooling the two lets a big source
    drown out a small one.
    """

    def __init__(self, records, *, boilerplate_share: float = BOILERPLATE_SHARE,
                 common_token_share: float = COMMON_TOKEN_SHARE,
                 min_records: int = MIN_RECORDS) -> None:
        self.boilerplate_share = boilerplate_share
        self.common_token_share = common_token_share
        self.min_records = min_records

        per_source_lines: dict[str, Counter] = {}
        per_source_total: Counter = Counter()
        for record in records:
            source = record.source
            per_source_total[source] += 1
            # A line repeated WITHIN one record still counts once: what makes a
            # line boilerplate is appearing across records, not within one.
            seen = {_key(line) for line in (record.text or "").splitlines() if line.strip()}
            per_source_lines.setdefault(source, Counter()).update(seen)

        self.record_count = dict(per_source_total)
        self.boilerplate: dict[str, set[str]] = {}
        for source, counts in per_source_lines.items():
            total = per_source_total[source]
            if total < min_records:
                self.boilerplate[source] = set()
                continue
            cutoff = boilerplate_share * total
            self.boilerplate[source] = {line for line, n in counts.items() if n >= cutoff}

        # Token frequency is measured on the MASKED text. A token that occurs
        # only inside a template is already gone, and counting it here would
        # let the same boilerplate justify its own exemption.
        self.total_records = sum(per_source_total.values())
        token_df: Counter = Counter()
        for record in records:
            masked = self.mask(record.text or "", record.source)
            token_df.update({m.group(0).lower() for m in WORD.finditer(masked)})
        self.token_df = token_df

    # ---- boilerplate ------------------------------------------------------ #

    def mask(self, text: str, source: str) -> str:
        """Blank this source's template lines, preserving every offset.

        The returned string has the same length as `text`, so a span found in
        it can be quoted straight out of the original.
        """
        templates = self.boilerplate.get(source)
        if not templates or not text:
            return text
        out = []
        for line in text.splitlines(keepends=True):
            stripped = line.rstrip("\r\n")
            if stripped.strip() and _key(stripped) in templates:
                out.append(" " * len(stripped) + line[len(stripped):])
            else:
                out.append(line)
        return "".join(out)

    def masked_share(self, text: str, source: str) -> float:
        if not text:
            return 0.0
        masked = self.mask(text, source)
        blanked = sum(1 for a, b in zip(text, masked, strict=True) if a != b)
        return blanked / len(text)

    # ---- specificity ------------------------------------------------------ #

    def too_common(self, token: str) -> bool:
        """True when a BARE token is too frequent in this corpus to refer.

        Qualified names (Class.method) are exempt: they are precise by
        construction, and a precise name that happens to be popular is still
        pointing at one thing.
        """
        if self.total_records < self.min_records:
            return False
        return self.token_df.get(token.lower(), 0) > self.common_token_share * self.total_records

    def df_share(self, token: str) -> float:
        if not self.total_records:
            return 0.0
        return self.token_df.get(token.lower(), 0) / self.total_records

    # ---- reporting -------------------------------------------------------- #

    def report(self, limit: int = 4) -> list[str]:
        lines = []
        for source, templates in sorted(self.boilerplate.items()):
            n = self.record_count.get(source, 0)
            if not templates:
                lines.append(f"  {source:<8} {n:>4} records - no template lines "
                             f"({'below threshold' if n < self.min_records else 'none repeated'})")
                continue
            lines.append(f"  {source:<8} {n:>4} records - {len(templates)} template line(s) masked")
            for line in sorted(templates, key=len, reverse=True)[:limit]:
                lines.append(f"             {line[:88]!r}")
        return lines

    def common_tokens(self, limit: int = 8) -> list[tuple[str, float]]:
        cutoff = self.common_token_share * (self.total_records or 1)
        common = [(t, n / (self.total_records or 1))
                  for t, n in self.token_df.items() if n > cutoff]
        return sorted(common, key=lambda kv: -kv[1])[:limit]
