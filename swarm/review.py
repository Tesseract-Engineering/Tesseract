"""
review.py - a spot-check review sheet for sampled extractions
(docs/SWARM.md, "Evaluation and cost controls").

After each prompt or model change, sample about 20 extraction records across
sources and read each one against its item. This writes the sheet: per
sample, the item text, what was extracted with its quotes, and blanks for the
reviewer to note misses and invented facts.

    python -m swarm.review                          the bucket, the latest run of each source
    python -m swarm.review --local DIR              a local folder written with --local
    python -m swarm.review --prompt thread-v2 -n 30 only that prompt version, 30 samples

Only complete runs (with a manifest) are read. The sheet quotes source data,
so it goes under data/reviews/, which is git-ignored; what the review finds
goes in eval/reviews/ as notes, without the quoted text.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

from .cli import store_for
from .records import ExtractionRecord, ItemRecord, Manifest, parse_line
from .store import ObjectStore

SOURCES = ("github", "jira", "slack", "gdrive")
MAX_TEXT = 4000


def complete_runs(store: ObjectStore, source: str) -> list[Manifest]:
    runs = []
    for key in store.list(f"runs/{source}/"):
        if key.endswith("/_manifest.json"):
            runs.append(Manifest.model_validate_json(store.get(key) or b""))
    return sorted(runs, key=lambda m: m.run_id)


def records_of(store: ObjectStore, manifest: Manifest, folder: str) -> list:
    out = []
    for name in manifest.files:
        if name.startswith(f"{folder}/"):
            raw = store.get(f"runs/{manifest.source}/{manifest.run_id}/{name}") or b""
            out += [parse_line(line) for line in raw.decode("utf-8").splitlines() if line]
    return out


def sample(store: ObjectStore, *, n: int, prompt: str | None, all_runs: bool,
           seed: int) -> list[tuple[ExtractionRecord, ItemRecord | None]]:
    """Extractions from the latest run of each source that has any (or from
    every run), spread evenly across sources, each with its item version."""
    by_source: dict[str, list[ExtractionRecord]] = {}
    items: dict[tuple[str, str], ItemRecord] = {}
    for source in SOURCES:
        runs = complete_runs(store, source)
        for m in runs:
            items.update({(r.id, r.version): r for r in records_of(store, m, "items")})
        with_extractions = [m for m in runs if m.counts.get("extractions")]
        for m in with_extractions if all_runs else with_extractions[-1:]:
            found = [r for r in records_of(store, m, "extractions")
                     if prompt is None or r.extractor.prompt_version == prompt]
            if found:
                by_source.setdefault(source, []).extend(found)

    rng = random.Random(seed)
    picked: list[ExtractionRecord] = []
    pools = {s: rng.sample(rs, len(rs)) for s, rs in sorted(by_source.items())}
    while len(picked) < n and any(pools.values()):
        for pool in pools.values():
            if pool and len(picked) < n:
                picked.append(pool.pop())
    return [(r, items.get((r.item_id, r.item_version))) for r in picked]


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _quotes(spans) -> str:
    return " / ".join(f"“{_cell(s.quote)}”" for s in spans)


def sheet(samples: list[tuple[ExtractionRecord, ItemRecord | None]], *, now: datetime) -> str:
    versions = sorted({r.extractor.prompt_version for r, _ in samples})
    models = sorted({r.extractor.model for r, _ in samples})
    lines = [f"# Extraction spot check, {now:%Y-%m-%d}", "",
             f"{len(samples)} samples · prompts {', '.join(versions) or '-'} · models {', '.join(models) or '-'}", "",
             "For each sample, mark what is wrong: an entity or relation the text does not support (invented), "
             "a fact the text states that was not extracted (missed), or a wrong type or endpoint.", ""]
    for i, (rec, item) in enumerate(samples, 1):
        lines += [f"## {i}. {item.title if item else rec.item_id}", "",
                  f"`{rec.item_id}` · {rec.source} {item.kind if item else ''} · {rec.extractor.prompt_version}"
                  + (f" · {item.url}" if item else ""), ""]
        if item:
            text = item.text if len(item.text) <= MAX_TEXT else item.text[:MAX_TEXT] + "\n[... cut for the sheet]"
            lines += ["```text", text, "```", ""]
        else:
            lines += ["(item record not found in the complete runs read)", ""]
        if rec.entities:
            lines += ["| Entity | Type | Name | Evidence |", "| --- | --- | --- | --- |"]
            lines += [f"| {e.local_id} | {e.type} | {_cell(e.name)} | {_quotes(e.evidence)} |" for e in rec.entities]
            lines.append("")
        if rec.relations:
            lines += ["| Relation | From | To | Confidence | Evidence |", "| --- | --- | --- | --- | --- |"]
            lines += [f"| {r.type} | `{r.source}` | `{r.target}` | {r.confidence:.2f} | {_quotes(r.evidence)} |"
                      for r in rec.relations]
            lines.append("")
        if not rec.entities and not rec.relations:
            lines += ["Nothing extracted.", ""]
        lines += ["- Invented:", "- Missed:", "- Wrong type or endpoint:", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--local", metavar="DIR", help="read a local folder instead of the bucket")
    parser.add_argument("-n", type=int, default=20, help="how many samples (default 20)")
    parser.add_argument("--prompt", help="only this prompt version, e.g. thread-v1")
    parser.add_argument("--all-runs", action="store_true", help="sample every complete run, not only the latest")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--out", default="data/reviews", help="where the sheet goes (default data/reviews)")
    args = parser.parse_args(argv)

    now = datetime.now(timezone.utc)
    picked = sample(store_for(args.local), n=args.n, prompt=args.prompt, all_runs=args.all_runs, seed=args.seed)
    if not picked:
        print("no extraction records found in complete runs", file=sys.stderr)
        return 1
    out = Path(args.out) / f"{now:%Y-%m-%dT%H%M%SZ}-spot-check.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(sheet(picked, now=now), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
