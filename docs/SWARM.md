# Company Brain: Swarm Layer Design & Roadmap

Oct 20, 2026 · @Karthik

## Overview and scope

The swarm layer is a set of batch pipelines, one per source, that run inside the customer's VPC and turn GitHub, Jira, Slack and Google Drive into versioned, permission-aware records in a staging bucket. The graph engineer builds the knowledge graph from those records. This doc fixes the design and lays out the build in six phases.

**In scope:** fetching from the four sources, redaction, normalization, ACL capture, deterministic and LLM extraction, cross-source identity resolution, change handling (edits, deletions, permission changes), and packaging for a customer VPC.

**Out of scope:** graph construction and querying, the MCP and API layer, approval-gated actions ("hands"), and real-time freshness. We run ingest-then-graph as batch ETL; event-driven updates are deferred.

**Principles**

- Plain code by default. LLM calls only where judgment is needed: semantic extraction and ambiguous identity matches.
- The record contract is the product boundary. Everything upstream of it can change freely; the contract changes only by versioned agreement with the graph engineer.
- Scope equals what the customer grants. The swarm sees only the channels, repos, projects and drives it is explicitly given access to.
- Nothing leaves the customer's boundary, including model calls.

## Decisions

Every design question below is settled. Rows marked default were open questions; each takes the conventional choice and can be revisited without breaking the record contract.

| Area | Decision | Status |
| --- | --- | --- |
| Data flow | Ingest-then-graph, batch ETL. Freshness deferred. | Settled |
| Where it runs | Fully self-contained in the customer's VPC. No vendor control plane. | Settled |
| Staging | Object storage (S3 or GCS) in the customer's VPC, customer-managed keys, no public path. | Settled |
| Sources | GitHub, Jira, Slack, Google Drive. Microsoft Teams and SharePoint are the second wave. | Settled |
| Coordination | Fixed pipeline per source; LLM steps only for extraction and identity matching. | Settled |
| Record contract | Schema v2: item and extraction records, approved by the graph engineer. | Settled |
| Vocabulary | Closed list of entity and relation types; open extensions only in `attributes`. | Default |
| Run handoff | Each run writes `_manifest.json` last; the graph side reads only runs with a manifest. | Default |
| Changes | `tombstone` and `acl_update` records, produced by a weekly reconciliation pass. | Default |
| Identity resolution | Owned by the swarm: a pass after all sources writes `identity` records. Email match first, LLM for leftovers. | Default |
| Scope | Opt-in by grant: invited Slack channels, selected repos, permitted Jira projects, shared drives the service account joins. | Default |
| Slack auth | Customer-created internal app from our manifest; bot token. Export as cold-start path. | Default |
| GitHub auth | Customer-created GitHub App, read-only, installed on selected repos. | Default |
| Jira auth | Service account with an API token, read-only project permissions. | Default |
| Drive auth | Service account added to selected shared drives, or to shared folders where there is no Google Workspace. Domain-wide delegation is an opt-in upgrade for full coverage. | Default |
| Drive file types | Docs, Slides and text PDFs get full text. Sheets get metadata, ACLs and refs only in v1. | Default |
| Models | Customer's cloud model service (Bedrock or Vertex) via private endpoint; in local dev, the Anthropic API with the same Claude model we run on Bedrock, so prompts carry over unchanged. | Default |
| Cadence | Nightly incremental run per source; weekly reconciliation; first backfill 2 years, configurable. | Default |
| Quality | Spot-check reviews of sampled extractions after each prompt change; a labeled gold set is deferred. | Default |
| Cost | Skip LLM extraction on trivial items; per-run token budget that halts LLM steps, not fetching. | Default |

## Architecture

Everything runs in the customer's cloud account, and the staging bucket is the only output. Sources are pulled in over TLS; nothing is pushed out.

&#91;embedded content: swarm architecture · customer VPC\]

Sources are pulled in read-only; the bucket is the handoff point, and the graph build reads only runs with a complete manifest.

| Component | In the customer VPC | In local dev |
| --- | --- | --- |
| Pipeline jobs | One container image per source, run as batch jobs (ECS Fargate, Cloud Run Jobs or Kubernetes Jobs) | `docker compose run <source>` |
| Identity pass | Batch job after the source jobs finish | Same image, run by hand |
| Scheduler | EventBridge Scheduler or Cloud Scheduler | Manual runs |
| Secrets | Secrets Manager or Secret Manager | `.env` file, never committed |
| Model endpoint | Bedrock or Vertex AI over a private endpoint | Direct model API behind the same interface |
| Staging bucket | S3 or GCS, customer-managed keys, VPC-only bucket policy | MinIO |
| Network | Private subnets; egress only to source APIs and the model endpoint | Docker network with `internal: true` plus an allowlisted egress proxy |

The same image runs in both columns. Only configuration differs: endpoint URLs, bucket names and credentials. The model call sits behind one interface, `extract(text, schema) -> entities`, so moving from a direct API to Bedrock or Vertex is a config change.

## Local development setup

Until Phase 5, everything runs on a laptop as a single Docker Compose project. Each service is its own container, and two Docker networks enforce the same rule as the customer VPC: pipelines can reach the internet only through an allowlisting proxy.

&#91;embedded content: local dev · one Docker Compose project\]

The arrows from the bottom row are mounts: `.env` supplies tokens, the Slack export is mounted read-only into `slack`, and `./minio-data` holds MinIO's files.

| Service | Image | Networks | Lifecycle | Role |
| --- | --- | --- | --- | --- |
| `minio` | `minio/minio` | internal | Always on | S3-compatible staging bucket; API on port 9000, console on 9001 |
| `proxy` | `ubuntu/squid` | internal, egress | Always on | The only route to the internet, limited to an allowlist |
| `github`, `jira`, `slack`, `drive` | Built from `pipelines/{source}` | internal | On demand | One batch run of that source's pipeline, then exit |
| `identity` | Built from `pipelines/identity` | internal | On demand, after source runs | Reads the run's items from MinIO and writes identity records |

**Everyday commands**

```bash
docker compose up -d minio proxy     # start the always-on services
docker compose run --rm github       # one GitHub run
docker compose run --rm identity     # identity pass after the sources
```

**Compose skeleton**

```yaml
services:
  minio:
    image: minio/minio
    command: server /data --console-address ":9001"
    ports: ["9000:9000", "9001:9001"]
    volumes: ["./minio-data:/data"]
    networks: [internal]

  proxy:
    image: ubuntu/squid
    volumes: ["./proxy/squid.conf:/etc/squid/squid.conf:ro"]
    networks: [internal, egress]

  slack:
    build: ./pipelines/slack
    env_file: .env
    environment:
      HTTPS_PROXY: http://proxy:3128
      S3_ENDPOINT: http://minio:9000
    volumes: ["./data/slack-export:/data/slack-export:ro"]
    networks: [internal]
    profiles: [jobs]

  # github, jira, drive and identity follow the same pattern

networks:
  internal:
    internal: true
  egress: {}
```

`profiles: [jobs]` keeps `docker compose up` from starting the pipelines; they run only when called by name. The proxy allowlist in `proxy/squid.conf` covers `api.github.com`, `*.atlassian.net`, `slack.com`, `www.googleapis.com`, `oauth2.googleapis.com` and `api.anthropic.com`; anything else is refused.

## Sources and connector interface

Each source is a thin fetcher behind one interface; redaction, ACLs, extraction and writing are shared. Adding a source means writing a fetcher and a normalizer, nothing else.

| Source | Category | Auth | Scope granted by | Item kinds | Notes |
| --- | --- | --- | --- | --- | --- |
| GitHub | Code | Customer-created GitHub App, read-only | Repos selected at install | `repo`, `commit`, `pull_request`, `issue` | Review comments fold into the PR's text |
| Jira | Tickets | Service account, API token | Projects the account can browse | `issue` (`issue_type` in meta) | ID uses the internal issue id; the key is an alias, since keys change on project moves |
| Slack | Chat | Customer-created internal app, bot token | Channels the bot is invited to | `thread`, `channel` | Internal apps keep Tier 3 rate limits; confirm on first install. No DMs. |
| Google Drive | Docs | Service account | Shared drives it is added to, or folders shared with its email (domain-wide delegation optional) | `document`, `folder` | Docs and Slides exported to text; Sheets metadata only in v1 |

**Connector interface**

```python
class Connector(Protocol):
    source: str

    def enumerate(self) -> Iterator[RawContainer]: ...
    def fetch(self, container: RawContainer, since: Checkpoint | None) -> Iterator[RawItem]: ...
    def list_ids(self, container: RawContainer) -> Iterator[str]: ...   # for reconciliation
    def acl(self, item: RawItem) -> Acl: ...

class Normalizer(Protocol):
    def to_item(self, raw: RawItem, acl: Acl) -> ItemRecord: ...
```

Slack has two connectors behind the same interface: `SlackExportConnector` for the cold start and tests, and `SlackApiConnector` for nightly runs. Both feed one normalizer, which groups messages into threads.

## Record schema v2

The swarm emits six record types. `item` and `extraction` are approved; `tombstone`, `acl_update`, `group` and `identity` extend the same contract and need the graph engineer's sign-off before Phase 4.

| Record type | Written by | What it says |
| --- | --- | --- |
| `item` | Source pipelines | A normalized source object with deterministic facts: people with roles, typed refs, container, ACL |
| `extraction` | LLM step | Entities and relations inferred from one item version, with confidence and evidence spans |
| `tombstone` | Reconciliation | An item no longer exists or is out of scope: `deleted`, `out_of_scope` or `access_revoked` |
| `acl_update` | Reconciliation and nightly runs | An item's permissions changed with no content change |
| `group` | Source pipelines | Who is in a group that item ACLs name, so the graph side can check a viewer against an item |
| `identity` | Identity pass | A cluster of source identities that are the same person, with method and confidence |

**Item and extraction records** follow the approved v2 shape: stable `id` from immutable source IDs, `aliases`, `kind`, `source`, `container`, `title`, `text`, `url`, `created_at`, `updated_at`, `version`, `content_hash`, `people` with roles, typed `refs` with `via`, `meta`, structured `acl`, `redactions`, `run_id`, `fetched_at`. Extractions carry `item_id`, `item_version`, `extractor` (model and prompt version), entities with `local_id`, relations and evidence.

**New record shapes**

```json
{"schema_version": "2", "record_type": "tombstone", "id": "slack:T01:thread:C42:1729450000.1234", "source": "slack", "reason": "deleted", "detected_at": "2026-10-25T03:10:00Z", "run_id": "..."}
{"schema_version": "2", "record_type": "acl_update", "id": "gdrive:file:1AbC", "source": "gdrive", "acl": {"visibility": "restricted", "principals": ["user:sam@acme.com"]}, "observed_at": "2026-10-25T03:12:00Z", "run_id": "..."}
{"schema_version": "2", "record_type": "group", "id": "group:slack:T01:channel:C42:members", "source": "slack", "name": "#eng-auth members", "members": ["user:sam@acme.com", "user:priya@acme.com"], "unresolved": 0, "observed_at": "2026-10-25T03:00:20Z", "run_id": "..."}
{"schema_version": "2", "record_type": "identity", "person_id": "person:c:7f3a91", "members": [{"id": "person:slack:U123", "source": "slack", "handle": "U123", "email": "sam@acme.com"}, {"id": "person:github:slee", "source": "github", "handle": "slee", "email": null}], "method": "llm", "confidence": 0.82, "evidence": "GitHub profile name and commit email match Slack display name", "run_id": "..."}
```

**ID formats.** Every `id` and every ref `target` use these patterns, so the linker can match references by string.

| Object | ID pattern | Alias example |
| --- | --- | --- |
| GitHub repo | `github:repo:{repo_id}` | `acme/api` |
| GitHub PR or issue | `github:repo:{repo_id}:pr:{number}`, `...:issue:{number}` | `acme/api#412` |
| GitHub commit | `github:repo:{repo_id}:commit:{sha}` | short sha |
| Jira issue | `jira:{site}:issue:{issue_id}` | `NEXP-318` |
| Jira project | `jira:{site}:project:{project_id}` | `NEXP` |
| Slack channel | `slack:{team_id}:channel:{channel_id}` | `#eng-auth` |
| Slack thread | `slack:{team_id}:thread:{channel_id}:{thread_ts}` | permalink |
| Drive file or folder | `gdrive:file:{file_id}` | file name |
| Source identity | `person:{source}:{handle}` | display name |
| Resolved person | `person:c:{cluster_hash}` | name |

**Bucket layout.** Each source writes only under its own prefix; nothing can delete.

```
runs/{source}/{run_id}/items/part-00001.jsonl
runs/{source}/{run_id}/extractions/part-00001.jsonl
runs/{source}/{run_id}/changes/part-00001.jsonl      # tombstones and acl_updates
runs/{source}/{run_id}/groups/part-00001.jsonl       # group memberships
runs/{source}/{run_id}/_manifest.json                # written last
runs/identity/{run_id}/identities/part-00001.jsonl
raw/{source}/{run_id}/...                            # redacted payloads, expire after 14 days
state/{source}/checkpoints.json
```

**ACL principals.** An item's `acl` names who may read it, as principals of three kinds:

| Principal | Means | Example |
| --- | --- | --- |
| `user:{email}` | One person, by the email they sign in with | `user:sam@acme.com` |
| `group:{source}:{immutable ids}` | A source-native group; its members arrive as a `group` record | `group:slack:T01:channel:C42:members` |
| `domain:{domain}` | Everyone with an email in that domain | `domain:acme.com` |

Group names use immutable IDs only, never a repo or org slug. Group members are users, nested groups, or source identities (`person:github:slee`) where the source gives no email, as GitHub usually doesn't; the graph side resolves those through identity records. Members the source lists but can't name are counted in `unresolved`.

**Reading rules.** The graph side's half of the contract:

1. Read a run only once `_manifest.json` exists with `status: complete`, and only the files it lists. Apply runs in `run_id` order, identity runs after the same night's source runs.
2. Newest content wins: of two item records with one `id`, the one with the later `updated_at` (then `fetched_at`) is current. `version` says whether two records are the same version, not which is newer.
3. Newest permissions win: an item's ACL comes from its latest `item` or `acl_update`, by `fetched_at` or `observed_at`. A group's members come from its latest `group` record.
4. A ref may point at an ID not ingested yet: keep it and resolve it when that item arrives. A `tombstone` or `acl_update` for an ID never ingested is a no-op.
5. A `tombstone` retires the item and every extraction with that `item_id`.
6. A source identity belongs to exactly one cluster: the one in the latest `identity` record that lists it. A record with `merged_into` retires its cluster.
7. A group member written as a source identity is that person, resolved through identity records.
8. Evidence span offsets count Unicode code points.

**Run manifest.** The graph side reads a run only once its manifest exists and `status` is `complete`.

```json
{"schema_version": "2", "source": "slack", "run_id": "2026-10-20T03:00:00Z-slack", "status": "complete", "started_at": "...", "finished_at": "...", "counts": {"items": 1840, "extractions": 912, "changes": 14}, "files": ["items/part-00001.jsonl"], "extractor_versions": ["decisions-v3"], "llm_budget_hit": false}
```

The counts above are illustrative. A run that fails writes no manifest; its partial files are ignored and overwritten on retry.

## Ontology

The vocabulary is closed: extractors may only emit the types below, and anything else goes in `attributes`. It lives in a versioned `ontology.yaml` that both the prompts and the record validator load, so a new type is one file change plus a version bump agreed with the graph engineer.

**Entity types**

| Type | Comes from | Example |
| --- | --- | --- |
| `person` | Deterministic: authors, assignees, reviewers, mentions | Sam Lee |
| `repo`, `commit`, `pull_request`, `issue`, `thread`, `channel`, `document`, `folder` | Deterministic: one per item and container | `acme/api#412` |
| `team` | LLM, plus Slack user groups and Google groups when present | Platform team |
| `project` | LLM, plus Jira epics | Auth migration |
| `decision` | LLM | Use OIDC instead of SAML |
| `system` | LLM: a service, component or tool being discussed | Billing service |
| `action_item` | LLM | Sam to draft the rollout plan |
| `customer` | LLM | An external account named in tickets or threads |

**Relation types**

| Type | Meaning | Comes from |
| --- | --- | --- |
| `authored` | Person created the item | Deterministic |
| `assigned_to` | Item or action item is owned by a person | Deterministic (Jira), LLM (action items) |
| `reviewed` | Person reviewed a PR or approved a doc | Deterministic |
| `participated_in` | Person commented on or took part in an item without authoring it | Deterministic |
| `mentions` | Item mentions a person or entity | Deterministic, LLM |
| `references` | Item links to another item | Deterministic (URLs, keys) |
| `implements` | PR or commit implements an issue | Deterministic (fields, closing keywords) |
| `closes` | PR closes an issue | Deterministic |
| `part_of` | Item or entity belongs to a project or epic; an item's container is its `container` field instead | Deterministic, LLM |
| `member_of` | Person belongs to a team | Deterministic (groups), LLM |
| `owns` | Person or team owns a system, project, repo or document | Deterministic (owner fields), LLM |
| `works_on` | Person is actively working on a project | LLM |
| `decided_by` | Decision was made by a person or team | LLM |
| `decided_in` | Decision was made in this item | LLM |
| `supersedes` | A decision or document replaces an earlier one | LLM |
| `depends_on` | Project, system or issue depends on another | Deterministic (Jira links), LLM |
| `blocks` | Issue blocks another | Deterministic (Jira links) |

Deterministic relations are written on the `item` record as `refs` or `people`; LLM relations go in `extraction` records. Item-backed entity types are never produced by the LLM, which keeps the graph's skeleton exact. Each relation lists the entity types it may join (`from`, `to` in `ontology.yaml`, copied into the schemas as `x-relations`), and the validator refuses anything outside them.

**Person roles.** Each role on an item's `people` list means one relation: `author` and `reporter` mean `authored`, `reviewer` means `reviewed`, `assignee` means `assigned_to`, `commenter` and `participant` mean `participated_in`, `mentioned` means `mentions`, and `owner` means `owns`.

## Pipeline stages

Every source runs the same eleven stages; only the first two are source-specific. Checkpoints advance only after the manifest is written, so a failed run is simply retried.

1. **Enumerate** containers in scope: repos, projects, channels, shared drives. Cache users for mention resolution.
2. **Fetch** items changed since the per-container checkpoint, plus a 7-day lookback to catch edits and late replies. Paginate with cursors and honor `Retry-After`.
3. **Redact** secrets in memory with detect-secrets or gitleaks patterns, before anything touches disk. Record what was stripped in `redactions`.
4. **Write raw** redacted payloads to `raw/`, expiring after 14 days.
5. **Normalize** to item records. Slack groups messages into threads and bursts; GitHub and Jira fold comments into their parent; Drive exports Docs and Slides to text.
6. **Attach ACLs** from the source: channel members, repo visibility, Jira project permissions, Drive sharing. An item with no ACL fails validation and is not written.
7. **Deterministic extraction:** people with roles, container, and typed refs from fields, URLs and keys in text (Jira keys, `#123`, Drive links).
8. **LLM extraction** on items that pass the cost filter, against the closed ontology, writing extraction records with evidence spans and `item_version`.
9. **Write records** to `items/` and `extractions/`, deduplicated by `id` and `version`. An unchanged `content_hash` skips re-extraction.
10. **Write the manifest** with counts, files and extractor versions.
11. **Advance checkpoints** in `state/`.

**Reconciliation (weekly per source).** List every ID in scope with `list_ids`, compare against previously seen IDs, and write a `tombstone` for anything missing. Re-read ACLs for all items and write an `acl_update` where they changed. Nightly runs also emit `acl_update` for any fetched item whose ACL changed.

**Identity pass (nightly, after all sources).**

1. Collect every raw identity from the run's `people` fields.
2. Cluster exact email matches deterministically.
3. Send leftovers with candidate matches (similar names, GitHub profile name, commit emails) to the LLM, which accepts or rejects each pairing with a confidence.
4. Write `identity` records. Clusters keep stable `person_id`s across runs; a merge or split writes a new record that the graph side applies.

## Security and privacy controls

The swarm holds a copy of the customer's data, so these controls ship in every phase rather than being added at the end.

| Control | How |
| --- | --- |
| Network | Private subnets, private endpoints to storage and the model, egress allowlist of source APIs only, bucket policy denying non-VPC access |
| Encryption | Customer-managed keys at rest, TLS in transit |
| Least privilege | Each pipeline role writes only its own `runs/{source}/` and `raw/{source}/` prefixes; the graph job is read-only; no role can delete |
| Read-only sources | Every source credential is read-only; no write scopes are requested |
| Permissions travel with data | Every item carries a structured `acl`; items without one fail validation |
| Revocation | Tombstones and ACL updates within one reconciliation cycle (7 days at most, 1 day for ACL changes seen in nightly runs) |
| Redaction | Secrets stripped in memory before the raw write; staging is scanned for secret patterns as a release check |
| Retention | Raw payloads expire after 14 days; records are kept until tombstoned |
| Logging | Every bucket read and write is logged; record contents never appear in application logs or traces |
| Model calls | Only to the customer's own model endpoint over a private link |
| Consent | Scope is opt-in by grant; DMs and personal Drive files are excluded by default |

## Evaluation and cost controls

Extraction quality is checked by hand-reviewing samples for now; a labeled gold set is deferred. Automated checks still gate every run.

**Spot-check reviews.** After each prompt or model change, review about 20 extraction records across sources against their source items. Note misses and invented entities in `eval/reviews/`; those notes can seed a gold set later.

| Check | Measures | Target | Runs |
| --- | --- | --- | --- |
| ACL coverage | Items with a non-empty `acl` | 100%, hard fail | Every run |
| Idempotency | Rerunning the same window produces no new records | Zero diff, hard fail | CI |
| Secret leakage | Secret-pattern hits in staging | Zero, hard fail | Every run |
| Ref resolution rate | Share of ref targets that exist as items | Tracked, no target | Every run |

**Cost controls**

- Skip LLM extraction on trivial items: Slack threads under 3 messages or 200 characters, bot-only tickets, empty PR descriptions with no comments. Deterministic extraction still runs.
- Re-extract only when `content_hash` changes or the prompt version changes.
- A per-run token budget halts LLM steps when reached. Fetching and deterministic extraction continue, and the manifest records `llm_budget_hit: true` so the backlog is processed next run.
- Log tokens per item and per source to every manifest, so cost per customer can be estimated from a pilot.

## Testing

Everything except LLM extraction is deterministic, so most of the code is covered by ordinary tests that run in CI without network access or model calls.

| Layer | What it covers | How |
| --- | --- | --- |
| Unit tests | Normalizers, ID builders, deterministic ref extraction, thread grouping, redaction | `pytest`, one test module per source |
| Fixtures | Realistic source data without real club data | Small hand-made exports and recorded API responses in `tests/fixtures/{source}/`, each including at least one planted secret |
| Contract tests | Every record matches the schema | Validate all output against the JSON Schema generated from the Pydantic models |
| Idempotency | Reruns change nothing | Run a pipeline twice on fixtures against MinIO and assert the second run writes no new records |
| Integration | The Compose stack works end to end | CI starts `minio` and `proxy`, runs each pipeline on fixtures, and checks the manifest |
| LLM steps | Prompt and parsing code paths | Recorded model responses replayed in CI; real model calls only in spot-check reviews |

Real club data never enters the repo or CI. `data/` and `.env` are in `.gitignore`.

**Repo layout**

```
company-brain/
  docker-compose.yml
  ontology.yaml
  proxy/squid.conf
  swarm/            # shared stages: redact, acl, extract, write, manifest
    records.py      # Pydantic models for all record types
  pipelines/
    github/  jira/  slack/  drive/  identity/
  tests/
    fixtures/{source}/
  eval/reviews/     # spot-check review notes
  data/             # local only, gitignored
```

## Access prerequisites

Each phase is blocked until someone with the right role grants access. Request these before the phase starts.

| Access | Needed for | Provided by | Status |
| --- | --- | --- | --- |
| Owner or admin on the club's GitHub org, to create and install the GitHub App | Phase 1 | Club GitHub org owner | Not started |
| Slack workspace export | Phase 2 | Club Slack workspace owner or admin | Not started |
| Permission to create an internal Slack app and invite its bot to channels | Phase 2 | Club Slack workspace admin | Not started |
| A Google Cloud project for the Drive service account | Phase 2 | Whoever on the team owns our Google Cloud account | Not started |
| The service account added to a shared drive, or folders shared with its email | Phase 2 | Club Google Workspace admin, or the folder owners | Not started |
| Anthropic API key for local dev | Phase 3 | Our team | Not started |
| Free Jira Cloud site, seeded with realistic tickets | Phase 4 | Our team | Not started |
| AWS test account with Bedrock model access enabled | Phase 5 | Our team | Not started |

## In this repo

The swarm lives next to the graph code, not inside it. Several of its terms already mean something else here:

| Swarm term | Where it lives | Existing term it is not |
| --- | --- | --- |
| `Connector` (live source fetcher) | `swarm/interfaces.py` | `tesseract/connectors.py`, the export file readers |
| Ontology, record contract | `ontology.yaml`, `swarm/records.py` | `tesseract/contract.py`, the graph's own node and edge types |
| Compose stack | `swarm/compose.yaml` | `compose.yaml`, the graph, API and UI stack |

```
ontology.yaml          the closed vocabulary, v1
swarm/run.py           the shared runner: the pipeline stages for any Connector + Normalizer
swarm/http.py          the shared HTTP client: proxy from env, pagination, rate-limit backoff
swarm/sources/github/  GitHub: App auth, connector, normalizer, deterministic refs
pipelines/github/      the GitHub pipeline image and its read-only App manifest
swarm/                 records.py, ids.py, interfaces.py, store.py, stages/, schemas/*.json, compose.yaml, proxy/
pipelines/sample/      the Phase 0 job: replays samples/run-0 through the real stages
tests/fixtures/        hand-made source API responses and the planted-token corpus
samples/run-0/         the sample run for the graph engineer (rebuilt by samples/make_run0.py)
tests/swarm_tests/     unit, contract and s3-marked tests
```

**Staging bucket in local dev.** The MinIO image is no longer published on Docker Hub, so `swarm/compose.yaml` runs RustFS (`rustfs/rustfs`), another S3-compatible server. It uses the same ports (API 9000, console 9001) and keeps its files in `./storage-data`. Pipelines speak plain S3 through `swarm/store.py`, so nothing above the compose file depends on which server it is.

```bash
pip install -e '.[dev,swarm]'
python -m swarm.schema                                    # regenerate swarm/schemas after a model change
python samples/make_run0.py                               # regenerate samples/run-0 after a schema change

docker compose -f swarm/compose.yaml up -d storage proxy
docker compose -f swarm/compose.yaml run --rm init        # create the bucket, once
docker compose -f swarm/compose.yaml run --rm sample      # a second run writes zero records
docker compose -f swarm/compose.yaml run --rm sample probe https://api.github.com https://example.com
```

## Roadmap

The build runs in six phases, each ending at a gate the graph engineer can verify. Phases are sequenced, not dated; the first real data flows at the end of Phase 1.

&#91;embedded content: swarm roadmap · 6 phases, each with a gate\]

Each diamond is the phase's exit gate; the next phase starts only when its gate passes.

**Owners.** Each phase has one owner who drives it to its gate. Fill in the owner column before Phase 0 starts.

| Phase | Owner | Signs off on the gate |
| --- | --- | --- |
| 0: Contracts and skeleton | Unassigned | Graph engineer |
| 1: GitHub end to end | Unassigned | Graph engineer |
| 2: Slack and Google Drive | Unassigned | Graph engineer |
| 3: LLM extraction | Unassigned | Swarm owner, from spot-check reviews |
| 4: Jira, identity and change handling | Unassigned | Graph engineer |
| 5: Customer VPC packaging | Unassigned | Co-founder who runs the engagement |

### Phase 0: Contracts and skeleton

- [ ] `ontology.yaml` v1 with the entity and relation lists, signed off by the graph engineer (written; awaiting sign-off)
- [x] Pydantic models and JSON Schema for all six record types and the manifest
- [x] Repo skeleton: `Connector` and `Normalizer` interfaces, shared redact, ACL, write and manifest stages
- [x] Docker Compose with MinIO, an internal network and an egress allowlist (RustFS stands in for MinIO; see below)
- [ ] Hand-written sample run that the graph engineer ingests (`samples/run-0/` written; awaiting ingest)

**Gate:** the graph engineer reads a sample run end to end with no schema questions.

### Phase 1: GitHub end to end

- [x] GitHub App manifest and `GitHubConnector` (built and tested on fixtures; not yet run on the club's repos)
- [x] Repos, PRs, issues and commits normalized, with review comments folded into each PR
- [x] ACLs from repo visibility and collaborators
- [x] Secret redaction with a test corpus of planted tokens
- [x] Deterministic refs from closing keywords, `#123` references and URLs

**Gate:** a valid manifest-complete run from the club's GitHub repos, ingested by the graph side, with zero secret hits and 100% ACL coverage.

**Gate status: deferred.** No source access exists yet. Until the sandbox org does, `tests/swarm_tests/test_github_gate_rehearsal.py` runs the whole pipeline on hand-made GitHub API responses (`tests/fixtures/github/`) and checks the gate's criteria. The first real run is `GITHUB_TOKEN=... GITHUB_REPOS=org/repo docker compose -f swarm/compose.yaml run --rm github`.

### Phase 2: Slack and Google Drive

- [ ] `SlackExportConnector` on the club's Slack export: thread and burst grouping, mention resolution, ACLs from channel membership
- [ ] `SlackApiConnector` with checkpoints, lookback and backoff
- [ ] `DriveConnector` for Docs, Slides and text PDFs. If the club has Google Workspace, add the service account to a shared drive; if it uses personal Google accounts, share the relevant folders with the service account's email instead, since shared drives require Workspace
- [ ] Nightly runs for all three sources on club data

**Gate:** three sources run nightly; rerunning a window produces zero new records.

### Phase 3: LLM extraction and evaluation

- [ ] Model interface with a direct-API backend and a Bedrock backend
- [ ] Extraction prompts per kind, versioned, writing extraction records with evidence
- [ ] Spot-check review of sampled extractions after each prompt change
- [ ] Cost filter, content-hash skip and per-run token budget

**Gate:** a spot-check review signs off on extractions from each source, and token cost per item is logged in every manifest.

### Phase 4: Jira, identity and change handling

- [ ] Jira sandbox seeded with tickets, epics and links, and `JiraConnector`
- [ ] Identity pass: email clustering, LLM matching for leftovers, `identity` records
- [ ] Hand-made roster of club members and their handles in each source, used to review identity clusters
- [ ] Weekly reconciliation writing tombstones and ACL updates
- [ ] Graph engineer signs off on the three new record shapes

**Gate:** a review of identity clusters against the roster finds no false merges, and a removed channel or unshared doc produces a tombstone or ACL update within one cycle.

### Phase 5: Customer VPC packaging

- [ ] Terraform module for AWS: bucket, KMS key, Fargate jobs, EventBridge schedules, IAM roles, private endpoints, Secrets Manager
- [ ] Bedrock as the production model backend
- [ ] Install runbook for the one-week engagement: create apps, grant scope, apply Terraform, first backfill
- [ ] Security checklist matching the controls table

**Gate:** a clean deploy into a fresh AWS account from the runbook alone, with a spot-check review passing on Bedrock.

## Risks and deferred questions

The biggest risks are coverage gaps from source permissions, not engineering.

| Risk | Impact | Mitigation |
| --- | --- | --- |
| Slack rate tier for new internal apps is unclear | Bulk history fetch becomes impractically slow | Confirm the tier on first install; fall back to export for backfill |
| Drive coverage without domain-wide delegation is partial | Docs outside shared drives are missed | Offer delegation as an opt-in upgrade; report coverage per drive |
| Slack export omits private channels and DMs on most plans | Cold start misses private context | Backfill private channels via the API for channels the bot is invited to |
| Club test data is shallow (free Slack plans show limited history) | Extraction quality is judged on a thin corpus | Seed a Jira sandbox and add synthetic test threads |
| LLM quality varies by model available in the customer's region | Extraction targets missed on some deployments | Spot-check extractions on the customer's model before go-live |
| Deletions lag up to 7 days | Deleted content is briefly queryable | Shorten the reconciliation interval for sensitive sources if a customer requires it |
| Schema changes break the graph side | Ingest failures | `schema_version` on every record; changes only by agreement |

**Deferred, revisit after the first customer**

- Event-driven freshness (webhooks) instead of nightly batch.
- A labeled gold set with precision and recall scoring, once prompt changes become frequent.
- Microsoft Teams and SharePoint via Microsoft Graph, and a GCP Terraform module.
- Sheets content, scanned PDFs, Confluence, Linear, GitLab, email and calendar.
- Per-customer scope configuration UI beyond grant-based scope.
- A cost model per seat or per GB, once pilot manifests give real token numbers.
