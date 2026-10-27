# graph

Builds the Neo4j knowledge graph from what the swarm writes. The swarm (`swarm/`, `docs/SWARM.md`) fetches each source and writes runs into a bucket; this folder reads those runs and writes the graph. It decides nothing about fetching, and the swarm decides nothing about the graph.

```
bucket runs/ ──▶ swarm_records.py ──▶ build_swarm.py ──▶ the door (contract.py, ingest.py) ──▶ Neo4j
                 reading rules 1-8     items → nodes,        validates every write,
                 current state         refs/roles → edges    provenance on each one
```

| File | What it does |
|---|---|
| `swarm_records.py` | Reads runs (a folder, a zip of `runs/`, or the S3 bucket) and applies SWARM.md's reading rules: complete manifests only, newest content and ACL win, tombstones retire items and their extractions, identity clusters. |
| `build_swarm.py` | Turns that state into node and edge proposals. Repos, channels, folders and Jira projects become `Container`s; PRs and issues `Issue`s; threads and docs `Document`s. Roles become `AUTHORED`, `REVIEWED`, `ASSIGNED_TO`...; refs become `CLOSES`, `IMPLEMENTS`, `PART_OF`, `BLOCKS`, `DUPLICATES`, `REFERENCES`. |
| `weights.py` | Every number in the graph: ref confidence by `via`, role weight, source authority for extractions, noisy-or when sources agree. |
| `contract.py`, `ingest.py`, `store.py`, `load.py` | The door: what node and edge types may exist, and the only code that writes to Neo4j. |
| `resolve.py` | Fallback people matching for accounts the swarm's identity run left unclustered. |
| `swarm_queries.py` | Reading as one viewer: only items their principals can see, and aggregates (who did the most where) computed per query, never stored. |
| `test_build_swarm.py` | `samples/run-0` end to end plus the reading rules. No database needed. |

## Run it

```
pip install -r graph/requirements.txt
python graph/test_build_swarm.py                                  # no database

cd graph
python build_swarm.py --root ../samples/run-0 --dry-run           # what it would write
S3_ENDPOINT=http://localhost:9000 python build_swarm.py --root s3 --env ../.env --ns kg:sandbox
```

`--root s3` reads the bucket named in the swarm's `.env` (`S3_BUCKET`, `STORAGE_ACCESS_KEY`, `STORAGE_SECRET_KEY`; `S3_ENDPOINT` for RustFS in local dev). Neo4j defaults to `neo4j://localhost:7687`; set `NEO4J_URI`, `NEO4J_USER` and `NEO4J_PASSWORD` to point elsewhere. A build replaces its namespace unless `--keep` is given.

## Permissions

Each node keeps its source ACL as `visibility` (`public`, `internal`, `restricted`) and `aclGroups`. A viewer's principals are `user:{email}`, `domain:{domain}` and every `Group` their person is `MEMBER_OF`. Extracted entities (decisions, action items) are only as visible as the items they came from. Counts and rankings are never stored on nodes, because a sum over items a viewer cannot read would leak them.
