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
| Jira auth | Service account with an API token, read-only project permissions. Project readers come from who may browse each project, which needs the global "Browse users and groups" permission; without it the readers group is written empty and grants nobody. Issues with a security level are readable by that level's group, written with no members. | Default |
| Drive auth | Service account added to selected shared drives, or to shared folders where there is no Google Workspace. Domain-wide delegation is an opt-in upgrade for full coverage. | Default |
| Drive file types | Docs, Slides, text PDFs and uploaded Word and PowerPoint files get full text; Office files are unzipped and read with the standard library, against size budgets, refusing any part with a DOCTYPE. Sheets and Excel files get metadata, ACLs and refs only in v1. Every other file type keeps metadata, so the graph still knows the file, its owner and who may read it. | Default |
| GitHub files | Docs on the default branch only: Markdown, reStructuredText and CODEOWNERS (ADRs, READMEs, handbooks, ownership), as `document` items. Source code and config are never read: they hold real secrets, dwarf everything else, and say what a system does rather than who decided what. A doc is re-read only when its blob changes. | Default |
| Slack ACLs | Public channels are `internal` (the whole workspace can read them); private channels name a members group. | Default |
| Slack grouping | Threads keep their `thread_ts`; other messages form bursts (gaps of 20 minutes at most, never across midnight UTC), so the export and the API build the same IDs. | Default |
| Drive ACL fallback | A file whose permissions the service account cannot list takes its parent folder's; a limited-access folder never does, and a file with neither is not written. | Default |
| Cross-source links | Slack, Drive and Jira resolve GitHub links through the GitHub pipeline's state in the bucket, and every source resolves Jira keys through the Jira pipeline's; unresolved links stay in `meta`. A night runs GitHub, then Jira, then the rest, so GitHub resolves Jira keys from the previous night. | Default |
| Models | Customer's cloud model service (Bedrock or Vertex) via private endpoint; in local dev, the Anthropic API with the same Claude model we run on Bedrock, so prompts carry over unchanged. | Default |
| Cadence | Nightly incremental run per source; weekly reconciliation; first backfill 2 years, configurable. | Default |
| Quality | Spot-check reviews of sampled extractions after each prompt change; a labeled gold set is deferred. | Default |
| Test data | A sandbox we own across the sources (GitHub, Slack, Drive, and Jira when added), seeded with realistic content. The club pilot is dropped: access was not granted. | Settled |
| Cost | Skip LLM extraction on trivial items; per-run token budget that halts LLM steps, not fetching. On the Anthropic API, extraction runs as one Message Batch per run, at half price. | Default |

## Architecture

Everything runs in the customer's cloud account, and the staging bucket is the only output. Sources are pulled in over TLS; nothing is pushed out.

&#91;embedded content: swarm architecture · customer VPC\]

Sources are pulled in read-only; the bucket is the handoff point, and the graph build reads only runs with a complete manifest.

| Component | In the customer VPC | In local dev |
| --- | --- | --- |
| Pipeline jobs | One container image per source, run as batch jobs (ECS Fargate, Cloud Run Jobs or Kubernetes Jobs) | `docker compose -f swarm/compose.yaml run --rm <source>` |
| Identity pass | Batch job after the source jobs finish | Same image, run by hand |
| Scheduler | EventBridge Scheduler or Cloud Scheduler | `swarm/nightly.sh`, by hand or from cron |
| Secrets | Secrets Manager or Secret Manager | `.env` for tokens, key files under `data/`; neither is committed |
| Model endpoint | Bedrock or Vertex AI over a private endpoint | Direct model API behind the same interface |
| Staging bucket | S3 or GCS, customer-managed keys, VPC-only bucket policy | RustFS, an S3-compatible server |
| Network | Private subnets; egress only to source APIs and the model endpoint | Docker network with `internal: true` plus an allowlisted egress proxy |

The same image runs in both columns. Only configuration differs: endpoint URLs, bucket names and credentials. The model call sits behind one interface, `Model.extract(system, text, schema)` in `swarm/model.py`, so moving from a direct API to Bedrock or Vertex is a config change (`SWARM_MODEL_BACKEND`).

## Local development setup

Until Phase 5, everything runs on a laptop as a single Docker Compose project, `swarm/compose.yaml`. Each service is its own container, and two Docker networks enforce the same rule as the customer VPC: pipelines can reach the internet only through an allowlisting proxy.

&#91;embedded content: local dev · one Docker Compose project\]

The arrows from the bottom row are mounts: `.env` supplies tokens; `data/` holds the Slack export and the GitHub and Drive key files, each mounted read-only into its pipeline; and `./storage-data` holds the bucket's files.

**Staging bucket.** The MinIO image is no longer published on Docker Hub, so the bucket is RustFS (`rustfs/rustfs`), another S3-compatible server on the same ports (API 9000, console 9001). Pipelines speak plain S3 through `swarm/store.py`, so nothing above the compose file depends on which server it is.

| Service | Image | Networks | Lifecycle | Role |
| --- | --- | --- | --- | --- |
| `storage` | `rustfs/rustfs` | internal, host | Always on | S3-compatible staging bucket; API on 9000, console on 9001, published on localhost only |
| `proxy` | `ubuntu/squid` | internal, egress | Always on | The only route to the internet, limited to an allowlist |
| `init` | Built from `pipelines/sample` | internal | Once | Creates the bucket |
| `sample` | Built from `pipelines/sample` | internal | On demand | Replays `samples/run-0` through the real stages; `sample probe URL...` checks the allowlist |
| `github`, `slack`, `drive` | Built from `pipelines/{source}` | internal | On demand | One batch run of that source's pipeline, then exit |
| `jira` | Built from `pipelines/jira` | internal | On demand | One batch run of the Jira pipeline, then exit; runs after `github` and before `slack` and `drive` |
| `identity` | Built from `pipelines/identity` | internal | On demand | After the source runs: reads every complete run's people, writes identity records |

The `host` network exists only so Docker publishes the bucket's ports to the laptop: it publishes nothing for a container that is on internal networks alone. No pipeline is on it.

**Everyday commands**

```bash
docker compose -f swarm/compose.yaml up -d storage proxy     # start the always-on services
docker compose -f swarm/compose.yaml run --rm init           # create the bucket, once
docker compose -f swarm/compose.yaml run --rm github         # one GitHub run
swarm/nightly.sh                                             # github, jira, slack, then drive
```

**Compose shape** (abridged; `swarm/compose.yaml` is the source of truth)

```yaml
x-pipeline: &pipeline
  env_file: [{path: ../.env, required: false}]
  environment:
    HTTPS_PROXY: http://proxy:3128
    NO_PROXY: storage
    S3_ENDPOINT: http://storage:9000
  networks: [internal]
  profiles: [jobs]

services:
  storage:
    image: rustfs/rustfs:1.0.1
    ports: ["127.0.0.1:9000:9000", "127.0.0.1:9001:9001"]
    volumes: ["../storage-data:/data"]
    networks: [internal, host]

  proxy:
    image: ubuntu/squid:6.6-24.04_edge
    volumes: ["./proxy/squid.conf:/etc/squid/squid.conf:ro"]
    networks: [internal, egress]

  slack:
    <<: *pipeline
    build: {context: .., dockerfile: pipelines/slack/Dockerfile}
    volumes: ["../data/slack-export:/data/slack-export:ro"]

  # github and drive follow the same pattern, mounting data/github and data/gdrive

networks:
  internal:
    internal: true
  egress: {}
  host: {}
```

`profiles: [jobs]` keeps `docker compose up` from starting the pipelines; they run only when called by name. The proxy allowlist in `swarm/proxy/squid.conf` covers `api.github.com`, `*.atlassian.net`, `slack.com`, `www.googleapis.com`, `oauth2.googleapis.com` and `api.anthropic.com`; anything else is refused.

## Sources and connector interface

Each source is a thin fetcher behind one interface; redaction, ACLs, extraction and writing are shared. Adding a source means writing a fetcher and a normalizer, nothing else.

| Source | Category | Auth | Scope granted by | Item kinds | Notes |
| --- | --- | --- | --- | --- | --- |
| GitHub | Code | Customer-created GitHub App, read-only | Repos selected at install | `repo`, `commit`, `pull_request`, `issue`, `document` | Review comments fold into the PR's text; `document` is a doc file on the default branch (see "GitHub files" under Decisions) |
| Jira | Tickets | Service account, API token | Projects the account can browse | `issue` (`issue_type` in meta) | ID uses the site's cloud id and the internal issue id; the key and any key from before a move are aliases. Descriptions and comments arrive as ADF and are turned into text before redaction |
| Slack | Chat | Customer-created internal app, bot token | Channels the bot is invited to | `thread`, `channel` | Internal apps keep Tier 3 rate limits; confirm on first install. No DMs. |
| Google Drive | Docs | Service account | Shared drives it is added to, or folders shared with its email (domain-wide delegation optional) | `document`, `folder` | Docs and Slides exported to text; PDFs, Word and PowerPoint files read; Sheets, Excel and other types metadata only in v1 |

**Connector interface**

```python
class Connector(Protocol):
    source: str

    def enumerate(self) -> Iterator[RawContainer]: ...
    def fetch(self, container: RawContainer, since: Checkpoint | None) -> Iterator[RawItem]: ...
    def list_ids(self, container: RawContainer) -> Iterator[str]: ...   # for reconciliation
    def acl(self, item: RawItem) -> Acl: ...

class Normalizer(Protocol):
    run_id: str                               # set by the runner before the first to_item

    def to_item(self, raw: RawItem, acl: Acl) -> ItemRecord: ...
```

A connector may also have three optional methods, which the runner calls when they are present. All three current sources implement the first two, and GitHub implements the third.

| Method | Runner calls it | Used for |
| --- | --- | --- |
| `groups(container, *, run_id) -> list[GroupRecord]` | After each container's items | The group records that the container's item ACLs name |
| `checkpoint(container) -> Checkpoint \| None` | After each container's items | Where this run got to; saved only once the manifest is written |
| `published_state() -> dict` | When the manifest is written | Lookup state for other sources, or for the source's own next run, written to `state/{source}/{name}.json` (GitHub's `repos.json` and `docs.json`) |

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
| GitHub doc file | `github:repo:{repo_id}:file:{path}`, path percent-encoded except `/`; a rename is a new ID | `acme/api/docs/adr/0002.md` |
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
state/{source}/backlog.json                          # items still due an LLM extraction
state/{source}/batches.json                          # Message Batches sent but not yet read
state/{source}/unresolved.json                       # items naming GitHub links or Jira keys no ref stands for yet
state/github/repos.json                              # repo names -> ids, read by Slack and Drive
state/github/docs.json                               # per repo: visibility and each doc's blob sha, read by the next GitHub run
state/jira/keys.json                                 # every Jira key seen, old keys included -> issue id, read by every source
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
{"schema_version": "2", "source": "slack", "run_id": "2026-10-20T03:00:00Z-slack", "status": "complete", "started_at": "...", "finished_at": "...", "counts": {"items": 1840, "extractions": 912, "changes": 14, "rejected": 3}, "rejections": {"MissingAcl": 3}, "acl_coverage": 0.998, "files": ["items/part-00001.jsonl"], "extractor_versions": ["decisions-v3"], "llm_budget_hit": false}
```

The counts above are illustrative. `rejections` counts items not written, by reason; `acl_coverage` is the share of fetched items that had a readable ACL, the rest being the `MissingAcl` rejections. A run that fails writes no manifest; its partial files are ignored and overwritten on retry.

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
2. **Fetch** items changed since the per-container checkpoint, plus a 7-day lookback to catch edits and late replies. Slack also rereads threads active in the last 30 days. Paginate with cursors and honor `Retry-After`.
3. **Redact** secrets in memory with detect-secrets or gitleaks patterns, before anything touches disk. Record what was stripped in `redactions`.
4. **Write raw** redacted payloads to `raw/`, expiring after 14 days.
5. **Normalize** to item records. Slack groups messages into threads and bursts; GitHub and Jira fold comments into their parent; Drive exports Docs and Slides to text and reads PDFs, Word and PowerPoint files.
6. **Attach ACLs** from the source: channel members, repo visibility, Jira project permissions, Drive sharing. An item with no ACL fails validation and is not written.
7. **Deterministic extraction:** people with roles, container, and typed refs from fields, URLs and keys in text (Jira keys, `#123`, Drive links). GitHub links and Jira keys resolve through what those pipelines published, so whichever source runs first cannot link to the others yet: an item left with unresolved links is kept in `state/{source}/unresolved.json`, and every later run of that source resolves it again, writing a new record only when a link appears. A full pass from an empty bucket is therefore GitHub, Jira, Slack, Drive, then GitHub again.
8. **LLM extraction** on items that pass the cost filter (with no model configured, they wait in the backlog for a run with one), against the closed ontology, writing extraction records with evidence spans and `item_version`. The model answers in a fixed JSON shape and quotes its evidence; code finds each quote in the item text to make the span. Anything it cannot check is dropped, not written: a quote not in the text, a type outside the ontology, a relation to an ID the model was not given, or a relation the ontology does not allow. One prompt per kind: Slack conversations, PRs, issues and documents. Repos, channels and folders are containers, and a commit's message is short and repeated on its PR, so those kinds are never sent.
9. **Write records** to `items/` and `extractions/`, deduplicated by `id` and `version`. An unchanged `content_hash` skips re-extraction.
10. **Write the manifest** with counts, files and extractor versions.
11. **Advance checkpoints** in `state/`.

**Reconciliation (weekly per source).** List every ID in scope with `list_ids`, compare against previously seen IDs, and write a `tombstone` for anything missing. Re-read ACLs for all items and write an `acl_update` where they changed. Nightly runs also emit `acl_update` for any fetched item whose ACL changed.

**Identity pass (nightly, after all sources).**

1. Collect every source identity from the `people` fields of every complete source run, with every name and email seen for it.
2. Cluster exact email matches deterministically.
3. Send each cluster no email joins, with the clusters whose names come closest (a shared word of three letters or more; GitHub logins count as names), to the LLM, which accepts or rejects each pairing with a confidence and its evidence. Pairs accepted at 0.8 or above merge. Answers are kept in `state/identity/decisions.json`, so a pair is asked once; with no model, those clusters stay apart until a run with one. The pass never splits a cluster; a disputed merge is undone by hand (`method: manual`).
4. Write `identity` records. Clusters keep stable `person_id`s across runs (`state/identity/clusters.json`): when clusters merge, the id most members already had survives, and each other id gets a record with `merged_into`.

## Security and privacy controls

The swarm holds a copy of the customer's data, so these controls ship in every phase rather than being added at the end.

| Control | How |
| --- | --- |
| Network | Private subnets, private endpoints to storage and the model, egress allowlist of source APIs only, bucket policy denying non-VPC access |
| Encryption | Customer-managed keys at rest, TLS in transit |
| Least privilege | Each pipeline role writes only its own `runs/{source}/`, `raw/{source}/` and `state/{source}/` prefixes, and reads `state/github/` to resolve GitHub links; the graph job is read-only; no role can delete |
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

**Spot-check reviews.** After each prompt or model change, review about 20 extraction records across sources against their source items. `python -m swarm.review` samples them from the latest complete runs and writes a review sheet with each item's text and what was extracted. The sheet quotes source data, so it goes in `data/reviews/` (git-ignored). Note misses and invented entities, without the quoted text, in `eval/reviews/`; those notes can seed a gold set later.

| Check | Measures | Target | Runs |
| --- | --- | --- | --- |
| ACL coverage | Written items with a non-empty `acl` | 100%, hard fail | Every run |
| Unreadable ACLs | Fetched items refused for having no readable ACL: `acl_coverage` and `rejections` in the manifest | Reported, not fatal | Every run |
| Idempotency | Rerunning the same window produces no new records | Zero diff, hard fail | CI |
| Secret leakage | Secret-pattern hits in staging | Zero, hard fail | Every run |
| Ref resolution rate | Share of ref targets that exist as items | Tracked, no target | Every run |

**Cost controls**

- Skip LLM extraction on trivial items: Slack conversations under 3 messages or 200 characters, bot-only tickets (with Jira), empty PRs and issues, and documents with no text. Deterministic extraction still runs.
- Re-extract only when `content_hash` changes or the prompt version changes. A prompt version covers everything the prompt renders, including the ontology, and `test_extract.py` pins a digest of each prompt so a change cannot ship under an old version. A new version re-extracts the items a run fetches; older items keep their old extraction until they change or are backfilled.
- A per-run token budget (`SWARM_LLM_TOKEN_BUDGET`) halts LLM steps when reached. Fetching and deterministic extraction continue, and the manifest records `llm_budget_hit: true`. Items still due an extraction go into a backlog in `state/{source}/backlog.json`, which the next run works through first. A model outage (rate limits, timeouts or 5xx after the SDK's retries) sends the run's remaining items to the backlog in the same way. A refusal or a cut-off answer is not asked again until the item or the prompt changes. A bad key or an unknown model fails the run.
- Every manifest carries the run's total input and output tokens. Each model call produces exactly one extraction record (possibly empty), so cost per item is `tokens / counts.extractions`; per-item counts are in the run's log.
- Local dev uses `claude-opus-5-5` at effort `medium` with the system prompt cached. Refusals fall back server-side (`fallbacks: "default"`) on direct API calls only; Bedrock has no server-side fallback.
- On the Anthropic API, extraction goes through the Message Batches API by default (`SWARM_MODEL_BATCH=on`), at half the per-token price. A run queues its items while fetching, sends them as one batch once fetching is done, and waits up to `SWARM_BATCH_WAIT_MINUTES` (default 60; most batches end well within that). A batch still running when the wait ends is recorded in `state/{source}/batches.json`, its items stay in the backlog, and the next run reads it first. An item asked about but not answered (expired, or a server error) is asked again next run. Batches take no refusal fallback, so a refused item stays refused, and the token budget is checked before sending against an estimate. `SWARM_MODEL_BATCH=off` switches to one direct call per item; Bedrock always calls directly, since it has no Message Batches.

## Testing

Everything except LLM extraction is deterministic, so most of the code is covered by ordinary tests that run in CI without network access or model calls.

| Layer | What it covers | How |
| --- | --- | --- |
| Unit tests | Normalizers, ID builders, deterministic ref extraction, thread grouping, redaction | `pytest`, one test module per source |
| Fixtures | Realistic source data, hand-made | Small hand-made exports and recorded API responses in `tests/fixtures/{source}/`, each including at least one planted secret |
| Contract tests | Every record matches the schema | Validate all output against the JSON Schema generated from the Pydantic models |
| Idempotency | Reruns change nothing | Each source's gate rehearsal (`test_{source}_gate_rehearsal.py`) runs its pipeline twice on fixtures and asserts the second run writes no new records |
| Storage | The write and manifest stages work against a real S3 API | `s3`-marked tests; CI starts the RustFS bucket as a service, and locally they are skipped unless `S3_ENDPOINT` answers |
| Compose stack | The pipeline images run end to end | Not in CI yet: run by hand with `docker compose -f swarm/compose.yaml run --rm sample` (and the source pipelines on fixtures) |
| LLM steps | Prompt and parsing code paths | `test_extract.py` runs the stage with a scripted model in CI: what is dropped, budget and backlog, outages, refusals, re-extraction on a new prompt version, and batches (one per run, a batch that outlives its run, unanswered and invalid requests). `swarm.model.Replay` replays recorded answers and can record real ones; real model calls only in spot-check reviews |

Data fetched from the sandbox (or, later, a customer) never enters the repo or CI. `data/`, `storage-data/` and `.env` are in `.gitignore`. The repo layout is under "In this repo".

## Access prerequisites

Every source is now our own sandbox, so the team grants all access itself. The sandbox is built; each source still has to be connected to the pipelines (credentials in `.env` and `data/`).

| Access | Needed for | Provided by | Status |
| --- | --- | --- | --- |
| The sandbox GitHub org, with the read-only GitHub App created and installed (or a token for a first run) | Phase 1 | Our team | Connected (2026-10-23) |
| A Slack export of the sandbox workspace | Phase 2 | Our team | None yet: Slack cannot backdate messages, so the sandbox runs through the API only |
| An internal Slack app in the sandbox workspace, its bot invited to the channels in scope | Phase 2 | Our team | Connected (2026-10-23) |
| A Google Cloud project for the Drive service account | Phase 2 | Our team | Done |
| The service account added to the sandbox's shared drive, or its folders shared with the account's email | Phase 2 | Our team | Connected (2026-10-23): five folders |
| Anthropic API key for local dev | Phase 3 | Our team | Done (in `.env`) |
| A Jira Cloud site in the sandbox, seeded with realistic tickets, epics and links | Phase 4 | Our team | Seeded (79 issues in five projects); not yet connected |
| AWS test account with Bedrock model access enabled | Phase 5 | Our team | Not started |

## In this repo

This branch holds the swarm alone. The earlier graph, API and UI code lives on the `hackathon` branch, and nothing here imports it. Two swarm terms also exist there with other meanings:

| Swarm term | Where it lives | Same word on `hackathon` |
| --- | --- | --- |
| `Connector` (live source fetcher) | `swarm/interfaces.py` | `tesseract/connectors.py`, the export file readers |
| Ontology, record contract | `ontology.yaml`, `swarm/records.py` | `tesseract/contract.py`, the graph's own node and edge types |

```
ontology.yaml          the closed vocabulary, v1
swarm/records.py       Pydantic models for every record type and the manifest
swarm/schema.py        generates swarm/schemas/*.json from those models
swarm/ids.py           the ID builders, in the formats above
swarm/interfaces.py    Connector, Normalizer and the raw types they pass
swarm/run.py           the shared runner: the pipeline stages for any Connector + Normalizer
swarm/stages/          redact, acl, write, manifest, and the hard-fail run checks
swarm/store.py         the bucket: S3 (RustFS in local dev) or a local folder
swarm/http.py          the shared HTTP client: proxy from env, pagination, rate-limit backoff
swarm/sources/github/  GitHub: App auth, connector, normalizer, deterministic refs
swarm/sources/slack/   Slack: Web API and export connectors, thread and burst grouping, normalizer
swarm/sources/gdrive/  Drive: service-account auth, connector (text export, PDFs, Office files, permissions), normalizer
swarm/sources/jira/    Jira: basic auth, connector (search, comments, browsers), ADF to text, normalizer
swarm/identity.py      the identity pass: email union-find, the model for the rest, stable person ids
swarm/sources/links.py links between sources: Drive URLs, GitHub links via state/github/, Jira keys via state/jira/
swarm/model.py         the model interface: direct API, Bedrock, and replay of recorded answers
swarm/stages/extract.py stage 8: cost filter, the call or the batch, quotes to spans, budget and backlog
swarm/stages/prompts.py the extraction prompts, one per kind, versioned
swarm/review.py        the spot-check review sheet
swarm/cli.py           what every pipeline's main.py shares
swarm/compose.yaml     the local dev stack; swarm/proxy/squid.conf is its egress allowlist
swarm/nightly.sh       one night's runs in order: github, jira, slack, drive
pipelines/github/      the GitHub pipeline image and its read-only App manifest
pipelines/slack/       the Slack pipeline image and its read-only internal app manifest
pipelines/drive/       the Drive pipeline image
pipelines/jira/        the Jira pipeline image
pipelines/identity/    the identity pass image
pipelines/sample/      the Phase 0 job: replays samples/run-0 through the real stages
samples/run-0/         the sample run for the graph engineer (rebuilt by samples/make_run0.py)
tests/fixtures/        hand-made source API responses and the planted-token corpus
tests/swarm_tests/     unit, contract, gate-rehearsal and s3-marked tests
eval/bucket.py         the bucket as the graph side reads it (complete runs, newest record per id), read-only
eval/check_sandbox.py  the sandbox's answer key, deterministic half: coverage, scope, redaction, ACLs, links, identity
eval/check_extraction.py the sandbox's answer key, model half: the decisions, owners, systems and customers the stories need
eval/reviews/          spot-check review notes, without quoted source text
data/                  local only, git-ignored: the Slack export and the GitHub and Drive key files
```

`python -m eval.check_sandbox` and `python -m eval.check_extraction` (with `S3_BUCKET`, `S3_ENDPOINT` and the bucket's credentials) run the sandbox's answer key against a bucket the pipelines wrote. A full pass from an empty bucket is GitHub, Jira, Slack, Drive, GitHub again, then identity. With `SWARM_MODEL_REPLAY` set to a directory, model answers are recorded there and replayed on later runs, so iterating on anything but a prompt costs nothing.

```bash
pip install -e '.[dev,swarm]'                             # into .venv; tests need detect-secrets from [swarm]
python -m swarm.schema                                    # regenerate swarm/schemas after a model change
python samples/make_run0.py                               # regenerate samples/run-0 after a schema change

docker compose -f swarm/compose.yaml run --rm sample      # a second run writes zero records
docker compose -f swarm/compose.yaml run --rm sample probe https://api.github.com https://example.com
docker compose -f swarm/compose.yaml run --rm slack --export /data/slack-export   # cold start, once
```

The always-on services and the nightly script are under "Local development setup".

**Connecting Slack.** A workspace admin creates an app from `pipelines/slack/app-manifest.yaml` (read-only scopes, no DMs), installs it, and invites its bot to each channel in scope. The bot token goes in `SLACK_BOT_TOKEN`. For the cold start, unpack the admin's export into `data/slack-export/` (or put the `.zip` there and pass its path) and set `SLACK_WORKSPACE_URL`. The export run writes checkpoints in the API's shape, so the first nightly run reads only the last week; private channels the export left out are backfilled from the API on that first run.

**Connecting Drive.** In a Google Cloud project, enable the Drive API, create a service account, and download its JSON key to `data/gdrive/service-account.json`. Add the account's email as a Viewer to each shared drive in scope; without Google Workspace, share the folders with it instead and list their ids in `GDRIVE_FOLDERS`.

## Roadmap

The build runs in six phases, each ending at a gate the graph engineer can verify. Phases are sequenced, not dated; the first real data flows when the sandbox is connected.

&#91;embedded content: swarm roadmap · 6 phases, each with a gate\]

Each diamond is the phase's exit gate. The plan was to start each phase only after the previous gate passed. In practice, the gates for Phases 0–2 depend on people and access outside the code (the graph engineer's sign-off, a connected sandbox), so Phases 1 and 2 were built against fixtures while those gates were still open. Each gate still has to pass, in order, before its phase counts as done.

| Phase | Built | Gate |
| --- | --- | --- |
| 0 | Yes | Open: waiting on the graph engineer's ontology sign-off and sample-run ingest |
| 1 | Yes, run on the sandbox | Open: the run meets the swarm's criteria; waiting on the graph side's ingest |
| 2 | Yes, run on the sandbox | Open: all three sources pass by hand, reruns write nothing; nightly scheduling not set up |
| 3 | Yes, run on the sandbox | Open: all four sources extracted and reviewed; waiting on the swarm owner's sign-off |
| 4 | Jira and identity built, run on the sandbox | Open: reconciliation not started; graph engineer's sign-off on the new record shapes |
| 5 | Not started | — |

**Owners.** Each phase has one owner who drives it to its gate. No owners are assigned yet; fill in the column before the Phase 0 gate review.

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
- [x] Docker Compose with an S3-compatible bucket (RustFS), an internal network and an egress allowlist
- [ ] Hand-written sample run that the graph engineer ingests (`samples/run-0/` written; awaiting ingest)

**Gate:** the graph engineer reads a sample run end to end with no schema questions.

### Phase 1: GitHub end to end

- [x] GitHub App manifest and `GitHubConnector`, run on the sandbox
- [x] Repos, PRs, issues and commits normalized, with review comments folded into each PR
- [x] ACLs from repo visibility and collaborators
- [x] Secret redaction with a test corpus of planted tokens
- [x] Deterministic refs from closing keywords, `#123` references and URLs
- [x] Docs on the default branch (Markdown, reStructuredText, CODEOWNERS) as `document` items

**Gate:** a valid manifest-complete run from the sandbox's GitHub repos, ingested by the graph side, with zero secret hits and 100% ACL coverage.

**Gate status: waiting on the graph side's ingest.** The first sandbox run (2026-10-23) was manifest-complete over the four repos the App is installed on: 62 items, 100% ACL coverage, zero secret hits, all five planted fake secrets redacted, and nothing from the repo left out of the install. A second run read the seven docs; a rerun wrote nothing. `tests/swarm_tests/test_github_gate_rehearsal.py` keeps checking the same criteria on hand-made responses (`tests/fixtures/github/`).

### Phase 2: Slack and Google Drive

- [x] `SlackExportConnector`: thread and burst grouping, mention resolution, ACLs from channel membership (tested on fixtures; the sandbox has no export)
- [x] `SlackApiConnector` with checkpoints, lookback and backoff; threads active in the last 30 days are reread so late replies are not missed
- [x] `DriveConnector` for Docs, Slides, text PDFs and uploaded Word and PowerPoint files, over shared drives or folders shared with the service account. With Google Workspace, add the service account to a shared drive; with personal Google accounts, share the relevant folders with the service account's email instead, since shared drives require Workspace
- [ ] Nightly runs for all three sources on the sandbox (each passes by hand; `swarm/nightly.sh` is ready; scheduling not set up)

**Gate:** three sources run nightly; rerunning a window produces zero new records.

**Gate status: waiting on nightly runs.** On the sandbox (2026-10-24), Slack read 320 messages from the 13 channels its bot is in (the uninvited channel and its canaries stayed out) into 105 items, and Drive read all 36 files in the five shared folders; both had full ACL coverage, zero secret hits, and wrote nothing on a rerun. `test_slack_gate_rehearsal.py` and `test_gdrive_gate_rehearsal.py` run each pipeline on fixtures (`tests/fixtures/slack/`, `tests/fixtures/gdrive/`) into a bucket the GitHub fixture run already wrote, and check that reruns write zero records. They also check that a Slack cold start from the export followed by an API run writes nothing new. The real images were also run in the compose stack on the fixture export: the first run wrote 11 items and the rerun wrote zero.

### Phase 3: LLM extraction and evaluation

- [x] Model interface with a direct-API backend and a Bedrock backend (`swarm/model.py`; Bedrock not yet run against a real endpoint)
- [x] Extraction prompts per kind, versioned, writing extraction records with evidence (thread, pull_request, issue, document)
- [ ] Spot-check review of sampled extractions after each prompt change (first review: `eval/reviews/2026-10-25-sandbox.md`; awaiting the swarm owner's sign-off)
- [x] Cost filter, content-hash skip and per-run token budget, with a backlog for what the budget or an outage defers

**Gate:** a spot-check review signs off on extractions from each source, and token cost per item is logged in every manifest.

**Gate status: waiting on sign-off.** All four sources were extracted on the sandbox (2026-10-25): 170 records, 426k input and 92k output tokens, every manifest logging its tokens. `eval/check_extraction.py` passes 11/11, and the first review (`eval/reviews/2026-10-25-sandbox.md`) lists what held up and five open problems.

### Phase 4: Jira, identity and change handling

- [x] The sandbox's Jira site seeded with tickets, epics and links, and `JiraConnector`, run on the sandbox
- [x] Identity pass: email clustering, LLM matching for leftovers, `identity` records (on the sandbox: one cluster per person, no false merges)
- [ ] Roster of the sandbox's people and their handles in each source, used to review identity clusters
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
| A Viewer service account may not be allowed to list a file's permissions | Files without a readable ACL are rejected, not written | Parent-folder fallback; check `acl_coverage` and `rejections` in the first real run's manifest before considering any wider role |
| Google group members are unreadable without the Admin SDK | Readers who come in through a group are not granted anything | Group records carry the group's email; delegation fills members later |
| A Slack reply to a thread quiet for over 30 days is missed nightly | The thread's text is stale until reconciliation | Weekly reconciliation rereads every thread |
| A container's checkpoint advances past items rejected in that run (for example, no readable ACL) | A rejected item is fetched again only while it is inside the 7-day lookback or after it changes; after that it is missing until reconciliation, which is not built yet | `rejections` in each manifest shows the count; reconciliation (Phase 4) rereads every item |
| Slack export omits private channels and DMs on most plans | Cold start misses private context | Backfill private channels via the API for channels the bot is invited to |
| Sandbox data is synthetic: cleaner and smaller than a real company's | Prompts and costs tuned on it may not hold for a customer | Seed it with messy, realistic content (long threads, edits, deletions, bots, planted secrets); recheck extraction and cost on the first customer's data |
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
