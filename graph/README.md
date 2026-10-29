# graph

Builds the Neo4j knowledge graph from what the swarm writes. The swarm (`swarm/`, `docs/SWARM.md`) fetches each source and writes runs into a bucket; this package reads those runs and writes the graph. It decides nothing about fetching, and the swarm decides nothing about the graph.

```
bucket runs/ ──▶ swarm_records.py ──▶ build_swarm.py ──▶ the door (contract.py, ingest.py) ──▶ Neo4j
                 reading rules 1-9     items → nodes,        validates every write,
                 current state         refs/roles → edges    provenance on each one
```

| File | What it does |
|---|---|
| `swarm_records.py` | Reads runs (the S3 bucket, or a folder holding `runs/`) and applies SWARM.md's reading rules: complete manifests only, newest content and ACL win, tombstones retire items and their extractions, identity clusters followed through `merged_into`, and the current extraction per item (rule 9). |
| `build_swarm.py` | Turns that state into node and edge proposals. Repos, channels, folders and Jira projects become `Container`s; PRs and issues `Issue`s; threads and docs `Document`s. Roles become `AUTHORED`, `REVIEWED`, `ASSIGNED_TO`...; refs become `CLOSES`, `IMPLEMENTS`, `PART_OF`, `BLOCKS`, `DUPLICATES`, `REFERENCES`. |
| `refs.py` | Source refs: where a live provider finds an item again, as a `forge://` URI (see "Source refs" below). |
| `weights.py` | Every number in the graph: ref confidence by `via`, role weight, source authority, noisy-or when sources agree. |
| `contract.py`, `ingest.py`, `store.py`, `load.py` | The door: what node and edge types may exist (the swarm's ontology), and the only code that writes to Neo4j. |
| `resolve.py` | Fallback people matching for identities no swarm cluster lists. Merges on exact email only; anything weaker is written as a `ReviewItem`. |
| `swarm_queries.py` | Reading as one viewer: `READABLE`, `EDGE_READABLE`, `LIVE` and `viewer()` for anything that reads the graph for a person, plus aggregates (who did the most where) computed per query, never stored. |
| `test_build_swarm.py` | `samples/run-0` end to end, the reading rules, and the access cases. No database needed. |

## Run it

From the repo root:

```
pip install -e '.[graph]'                                           # or: pip install -r graph/requirements.txt
python -m graph.test_build_swarm                                    # no database

python -m graph.build_swarm --root samples/run-0 --dry-run          # what it would write
S3_ENDPOINT=http://localhost:9000 python -m graph.build_swarm --root s3 --env .env --ns kg:sandbox
```

Inside the compose stack (`docs/SWARM.md`), the `graph` job does the same against `storage` and `neo4j` on the internal network: `docker compose -f swarm/compose.yaml run --rm graph [--dry-run]`.

`--root s3` reads the bucket named in the environment or `--env` file (`S3_BUCKET`, `STORAGE_ACCESS_KEY`, `STORAGE_SECRET_KEY`; `S3_ENDPOINT` for RustFS in local dev). Neo4j is `NEO4J_URI` (default `neo4j://localhost:7687`), `NEO4J_USER` and `NEO4J_PASSWORD`, which has no default. A build replaces its namespace, and nothing outside it, unless `--keep` is given.

## What a node carries

Items keep the source's fields as properties (Jira status, PR state, message counts...). `sourceUpdatedAt` is when the source last changed the item; `ingestedAt` is when this build wrote it. People carry `isBot`. Every node and edge records its `source`, `producer` and `confidence`.

Confidence is how sure we are a fact is true: 1.0 for a field the source filled, less for a link spotted in prose (by `via`), and the model's own score for anything extracted. `sourceAuthority` (code over chat) is stored separately on extracted edges, for ranking; it never lowers a confidence. Edges under 0.80 are stored with `decision: "escalate"`, and readers leave them out by default (`swarm_queries.CONFIDENCE_FLOOR`).

## Source refs

A node built from an item that a live provider can fetch again carries a **source ref**, so a live reader (forge, `docs/FORGE.md`) can go from the node to the item without parsing its link:

| Property | Value |
|---|---|
| `ref` | the `forge://` URI below |
| `refProvider`, `refKind` | its parts: `github` / `jira` / `gdrive`, and the kind within that provider |
| `contentSha` | GitHub docs only: the git blob SHA the text was read from (the swarm item's `version`) |

| Provider | Kind | URI | Link |
|---|---|---|---|
| github | blob | `forge://github/blob/acme/api@<sha>:src/auth/session.py` | `https://github.com/acme/api/blob/<sha>/src/auth/session.py` |
| github | pull, issue | `forge://github/pull/acme/api/412` | `https://github.com/acme/api/pull/412` |
| github | repo | `forge://github/repo/acme/api` | `https://github.com/acme/api` |
| jira | issue | `forge://jira/issue/AUTH-42` | `<jira base url>/browse/AUTH-42` |
| gdrive | file, folder | `forge://gdrive/file/<fileId>` | `https://drive.google.com/file/d/<fileId>/view` |

`build_swarm.source_ref` reads the ref from the item's link (`refs.from_url`), and keeps it only when its kind is the item's: a GitHub issue whose link is a PR gets no ref. A Drive item with no link gets its ref from its id. A GitHub doc's ref pins the commit its permalink was read at (`meta.last_commit`); when the swarm had no commit and linked the branch, the ref has no version. Its path is `meta.path`, not the URL-quoted path in the link.

Slack is index-only: the graph holds its text, but no provider fetches it again. GitHub commits and Jira projects have no live kind, so they get no ref either. Nothing else about a node changes when it has no ref. `ref` is indexed on `Issue`, `Document`, `Commit` and `Container`. Every build prints a **Refs** section after the proposals:

```
Refs
  github  3/4 items with a ref (1 no live kind)
  jira    3/4 items with a ref (1 no live kind)
  slack: index only
```

A *mismatch* is an item of a fetchable kind whose link gave no ref of that kind.

## Permissions

Each node keeps its source ACL as `visibility` (`public`, `internal`, `restricted`) and `aclGroups`. A viewer's principals are `user:{email}`, `domain:{domain}` and every `Group` their person is `MEMBER_OF`, including groups inside groups.

Extracted entities (decisions, action items, systems) get one node per access set: a decision named in a public repo and in a private channel is two nodes, so the public one carries nothing from the private one. Extracted edges carry the access of the items they were read from, and readers check the edge as well as its endpoints. Counts and rankings are never stored, because a sum over items a viewer cannot read would leak them.

A wrong identity merge would give one person the other's access, so identities are only merged on email (or by the swarm's identity run); same-name matches wait for a person as `ReviewItem`s.
