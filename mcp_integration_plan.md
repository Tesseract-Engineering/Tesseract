# Integration Forge in Tesseract: integration plan

Draft · Oct 19, 2026 · companion to [mcp_design.md](mcp_design.md)

## Summary

This plan adapts the Integration Forge design to the code that exists in this repository. It is split into nine checkpoints (CP0 to CP8). Each one ends in something you can demonstrate and leaves the existing `/ask` path working.

Three adaptations shape everything else:

1. **Refs come first.** Today, graph nodes carry ids derived from export files and no resolvable source reference. CP1 and CP2 put a `SourceRef` on every node from a live provider before any runtime work starts. This is the design's own top risk ("check this first").
2. **The provider set changes.** The demo plan moves from GitHub, Drive and Notion to **GitHub, Jira and Google Drive**:
   - GitHub is the hand-written reference spec, and later the reuse showcase through its official MCP server.
   - Jira is the Builder's first generated spec, because its keys are already native ids in the graph.
   - Drive is the second generated spec and carries the repair demo.
3. **The build still reads exports.** New `snapshot` commands pull GitHub and Drive into the export layout, with native ids in each file. The graph build stays offline and deterministic, and the snapshots double as replay fixtures. Only snapshot and the runtime call provider APIs, and both go through the broker.

`search_brain` ships keyword-only (Neo4j full-text). Vector search is deferred.

## Decisions this plan locks in

| Decision | Choice | Why |
|---|---|---|
| Runtime language and store | Python 3.11+, Neo4j 5 | That is what the graph code is written in. This answers the design's first open question. |
| Demo providers | GitHub, Jira, Google Drive | GitHub's ingest code already exists, and Jira keys are already native ids. Drive keeps the repair demo. Notion moves to phase 2. |
| How the graph gets live-provider data | `snapshot` commands write export files; `build` reads them | Keeps the ADAPTERS.md principle, makes builds reproducible, and gives replay for free |
| Search | Keyword (existing `knowledgeSearch` full-text index) | Already works with ACLs; vectors add an index and an embedding dependency without changing the demo |
| Who the caller is | The server is bound to a viewer by configuration or bearer token, never by a tool argument | A model must not be able to name a more privileged viewer. Internal functions still take `principal`, as the design asks. |
| Per-user ACL | In scope, using the existing viewer model | Already implemented for every read, so keeping it is cheaper than removing it |
| M365 | Phase 2 | Its export ids (site and folder names) need driveId and itemId work first |
| Agent loop | New, Anthropic SDK tool runner, separate from `llm.py` | The answer path stays tool-free, as [ARCHITECTURE.md](docs/ARCHITECTURE.md) promises |
| Ontology change | One new edge type, `LINKS_TO` | Needed for document-to-PR and document-to-document links found as URLs in text |

## Vocabulary

Several design terms already mean something else here. The plan uses the names on the right.

| Design term | Name in this repo | Existing term it would have collided with |
|---|---|---|
| `Adapter` (live provider interface) | **Provider** | `adapters/`, the export-layout readers |
| Connector spec | **Provider spec** | `connectors.py`, the export file readers |
| Contract tests | **Conformance tests** | `contract.py`, the ontology |
| Build harness / build | **Forge run** | `build.py`, the graph build |
| (n/a) | "the door" still means [ingest.py](tesseract/ingest.py); "approval door" still means [actions.py](tesseract/actions.py) | |

## Target layout

```
tesseract/
  refs.py                     # CP1: SourceRef, URIs, url <-> ref
  snapshot/                   # CP2: github.py, gdrive.py -> export layout
  forge/
    runtime/
      server.py               # CP3: MCP front door
      graph_tools.py          # CP3: search / entity / neighbors over the graph
      spec.py                 # CP3: spec schema, loader, static validator
      provider.py             # CP3: Provider built from a spec
      registry.py             # CP3: active spec per provider
      executors/              # http.py (CP3), sandbox.py (CP5), upstream_mcp.py (CP6)
    broker/                   # CP3: credential handles, allowlist, egress, record/replay
    harness/
      inventory.py            # CP4
      fixtures.py, verify.py  # CP4: conformance fixtures and runner
      ledger.py               # CP4
      agent.py                # CP5: tool-runner loop
      roles/                  # CP5/CP6: planner, builder, verifier
      pipeline.py             # CP5
forge/                        # data, not code
  policy/hosts.yaml           # provider host allowlists
  specs/<provider>/v<N>.yaml  # registered specs (+ transforms/)
  evals/contract/             # generated conformance fixtures
  evals/retrieval/            # questions + gold refs
  demo/seed/manifest.yaml     # planted assets and links
var/forge/                    # git-ignored: credentials, registry, ledger, runs, replay
```

Forge dependencies go in a `forge` optional extra so the base install and the Docker image for the answer path do not change.

## Checkpoint overview

| CP | Name | You can show | Depends on | Size | Status |
|---|---|---|---|---|---|
| 0 | Foundations | Environment builds; SDK spikes answered | – | S | done |
| 1 | Source refs in the graph | `build --dry-run` prints ref coverage per source | 0 | M | done; Neo4j check pending |
| 2 | GitHub and Drive ingest, link edges, seed | `/ask` answers the headline question from the graph, citing PR, ticket and doc | 1 | L | |
| 3 | Runtime skeleton | Claude Code connects over MCP and reads a live PR through the hand-written GitHub spec | 2 | L | |
| 4 | Inventory, conformance, ledger | A spec is graded against held-out graph samples; a broken spec fails with a diff | 3 | M | |
| 5 | Agent harness and Builder | Builder writes passing Jira and Drive specs unattended, with one repair cycle | 4 | L | |
| 6 | Planner, upstream MCP, remaining tools | Coverage matrix; GitHub search reused from the official server; 7 tools | 5 | M | |
| 7 | Retrieval eval | Scorecard: wrapper vs raw upstream vs no tools | 6 | M | |
| 8 | Demo hardening | Two full rehearsals, one live and one offline replay | 7 | M | |

Sizes are relative (S < M < L), not estimates.

CP0 to CP2 are useful even if the Forge stops there: they give the current product live links and three new sources.

---

## CP0: Foundations

**Status: done (Oct 19, 2026).** Results are in [docs/FORGE.md](docs/FORGE.md#spike-results-cp0-oct-19-2026):
- MCP SDK 2.3.0 (`MCPServer`, the 2.x name for `FastMCP`). The SDK client and Claude Code 2.1.285 both negotiate the stateless **2026-07-28** revision; legacy clients fall back to 2025-11-25. Target 2026-07-28.
- `anthropic` 0.125.0, the newest release under the existing `<1` pin, has the tool runner, `output_config.format` and `output_config.effort`, plus `fallbacks` on the beta surface. The pin stays; there is no 1.x move.
- The JSONPath subset is [tesseract/forge/runtime/jsonpath.py](tesseract/forge/runtime/jsonpath.py). The spike is kept as a regression test in `tests/test_forge_spike.py`.

**Goal.** A working environment and answers to the two questions that could change the runtime's shape.

**Work**

1. **Environment.** Use Python 3.11 or newer. `pyproject.toml` requires `>=3.11`, CI runs 3.12, and the system Python on this machine is 3.9.6.
2. **Dependencies.** Add a `forge` extra with the Python MCP SDK (`mcp`), `pyyaml` and `httpx`. Implement JSONPath as a small in-house subset (`$.a.b`, `$.a[0]`, `$.a[*].b`). Specs only need that much, and it keeps the validator auditable.
3. **Spike: MCP protocol revision.** Build a one-tool server with the SDK and connect from both an SDK client and Claude Code. Record which protocol revision it speaks.
   - If the stateless 2026-07-28 revision is not supported yet, target the newest one that is.
   - Either way, keep all cross-call state in explicit server-minted handles, as the design already asks, so moving revisions later is a transport change only.
4. **Spike: Anthropic SDK.** Confirm that the pinned `anthropic>=0.30,<1` has the beta tool runner and structured outputs (`output_config.format`). If it does not, upgrade to 1.x. Note that 1.x is built on `httpx2`, so keep the broker's `httpx` client separate from the SDK's.
5. **Docs.** Create `docs/FORGE.md` with the vocabulary table and the results of both spikes. Add one paragraph to [ADAPTERS.md](docs/ADAPTERS.md): the build still reads exports, and `snapshot` is the only ingest-time code that calls provider APIs.
6. **Housekeeping.** Add `var/forge/` to `.gitignore` (`var/` is already ignored; make the rule explicit). Create the empty `forge/` data folder.

**Exit criteria**

- [x] `pip install -e '.[dev,forge]'` works on 3.11+, and `pytest` and `ruff` pass.
- [x] Both spike results are written in `docs/FORGE.md`, including the protocol revision chosen.

---

## CP1: Source refs in the graph

**Status: done, except one check (Oct 19, 2026).** The Neo4j integration test is written but has not run: this machine has no Neo4j or Docker. CI runs it. Details are in [docs/FORGE.md](docs/FORGE.md#source-refs). Choices made along the way:
- The Jira reader uses the export's own `company.json` when it has one, otherwise the configured company. The CSV `Updated` column becomes `sourceUpdatedAt`.
- `url` is now written for every artifact type, Issues included. An empty URL is omitted rather than stored as `""`.
- A Jira key that is not Jira-shaped gets no ref, and the Refs check counts it as missing instead of guessing one.
- An unpinned blob links to `blob/HEAD/...`, and `from_url` reads `HEAD` back as "no version", so it round-trips.

**Goal.** Every record from a live-resolvable source carries a `SourceRef` from reader to node, and every build reports how many do.

**Work**

1. **`tesseract/refs.py`.**
   - A frozen `SourceRef(provider, kind, id, version=None)` type.
   - `uri()` and `parse(uri)` for the `forge://` form.
   - `to_url(ref, company)` and `from_url(url)`, with one small parser per provider.
   - Canonical forms for v1:

     | Provider | Kind | URI |
     |---|---|---|
     | github | blob | `forge://github/blob/acme/api@<sha>:src/auth/session.py` |
     | github | pull / issue | `forge://github/pull/acme/api/412` |
     | github | repo | `forge://github/repo/acme/api` |
     | jira | issue | `forge://jira/issue/AUTH-42` |
     | gdrive | file / folder | `forge://gdrive/file/<fileId>` |

2. **`SourceRecord.ref`.** Add `ref: SourceRef | None = None` in [connectors.py](tesseract/connectors.py).
3. **Write refs onto nodes.** In [load.py](tesseract/load.py) `artifacts()`, write `ref`, `refProvider`, `refKind`, `url` (for every artifact type), `sourceUpdatedAt` and, when known, `contentSha`.
   - This fixes a current gap: Documents drop `url` today. Only Issues keep it, at [load.py:152](tesseract/load.py#L152), so the SharePoint sidecar URL is lost.
   - Node hashes include their properties, so the first rebuild after this change rewrites every node. That is expected and happens once.
4. **Index.** Add an index on `ref` for each artifact label in [store.py](tesseract/store.py) `ensure_schema`.
5. **Jira.** Add `jira_base_url` to the company file ([config.py](tesseract/config.py)). In [workspace.py](tesseract/adapters/workspace.py) `jira_tickets`, set `ref = jira/issue/<KEY>` and `url = <base>/browse/<KEY>`.
6. **Build check.** [build.py](tesseract/build.py) prints a **Refs** section next to Coverage:
   - records with a ref, per source;
   - every record whose `from_url(url) != ref`;
   - sources with no live provider, listed as "index only" (Slack, Outlook, Teams, SharePoint and `docs/` for now).
7. **Tests.** Round-trips `parse(uri(r)) == r` and `from_url(to_url(r)) == r` for each kind; properties written by `load.artifacts`; Jira ref and URL from a CSV fixture.

**Exit criteria**

- [x] `build --dry-run` on existing workspace data prints ref coverage: Jira at 100% when `jira_base_url` is set, everything else listed as index-only. Checked on a scratch workspace (jira, slack, sharepoint, docs).
- [ ] The integration test asserts `ref` and `url` on a Jira Issue node. *Written (`test_a_jira_issue_carries_its_ref_and_link`); not run yet because it needs Neo4j.*
- [ ] All existing tests pass, and `/ask` output is unchanged apart from the new properties. *Unit tests pass (117 passed). The Neo4j integration tests that guard `/ask` were skipped locally.*

---

## CP2: GitHub and Drive ingest, link edges, demo seed

**Goal.** The graph holds the three demo providers with native refs, plus the cross-source edges the headline question walks. The existing `/ask` can answer it before any runtime exists.

**Work**

1. **Snapshot commands.** `python -m tesseract.snapshot github --org acme --out $DATA/github` and `python -m tesseract.snapshot gdrive --folder <id> --out $DATA/gdrive`. They write files in the export layout below. Credentials come from the environment in CP2 and move behind broker handles in CP3.

   ```
   github/<owner>/<repo>/repo.json           default_branch, head_sha, html_url
   github/<owner>/<repo>/pulls/<n>.json      PR, reviews, changed files, merge_commit_sha, html_url
   github/<owner>/<repo>/issues/<n>.json     issue, comments, html_url
   github/<owner>/<repo>/tree/<path>         *.md and docs/** at head_sha
   gdrive/<folder path>/<name>.<ext>         Google Docs exported as text; other files downloaded
   gdrive/<folder path>/<name>.<ext>.meta.json   id, mimeType, modifiedTime, webViewLink, owners, permissions
   ```

2. **Readers in [connectors.py](tesseract/connectors.py).** Add `github_export(root)` and `gdrive_export(root)`, following the shape of `sharepoint_export`.

   | Item | Record | Notes |
   |---|---|---|
   | Pull request | `issue`, id `github:pull:<owner>/<repo>#<n>`, `isPullRequest` | Author goes in `people`. Reviewers go in `commenters` with `as: "review"`, which becomes the existing `COMMENTED` edge. Jira keys in the title, body or branch go in `refs.linkedIssues`. |
   | Issue | `issue`, id `github:issue:<owner>/<repo>#<n>` | Same shape as a PR |
   | Repo file | `document`, id `github:blob:<owner>/<repo>:<path>` | Ref pinned to `head_sha`; `contentSha` is the blob SHA |
   | Drive file | `document`, id `gdrive:<fileId>` | ACL from permissions: `anyone` or domain becomes `internal`; otherwise `private` with `gdrive:user:<email>` and `gdrive:group:<email>` groups |

   Two notes on the readers:
   - Check early that the snapshot credential is allowed to read `permissions`. If it is not, mark files `internal` and record the gap in the Refs check rather than guessing.
   - Retire `github_issues` (public API with a disk cache), since snapshot replaces it. `git_commits` stays for local repos.

3. **Workspace adapter.** Add `github` and `gdrive` to `PARTS` and `load()` in [workspace.py](tesseract/adapters/workspace.py).
4. **PR-to-ticket links.** The `REFERENCES` loop at [build.py:524](tesseract/build.py#L524) already turns `refs.linkedIssues` into edges. Restrict key extraction from PR text to project keys present in the Jira export, so strings like `UTF-8` are not matched.
5. **URL links (the one ontology change).**
   - Add `LINKS_TO: (_ARTIFACTS, _ARTIFACTS)` with props `{url}` in [contract.py](tesseract/contract.py).
   - Add `matching.url_links(record, ref_index)`: for each URL in a record's text, `refs.from_url`. If the ref names a node in this build, emit `LINKS_TO` with producer `parser:url`, confidence 1.0 and the line as the quote.
   - Add `LINKS_TO` to `Q_LINKED` in [answer.py](tesseract/answer.py) so `/ask` benefits too.
6. **Demo seed v1.** Create the seed workspace and describe it in `forge/demo/seed/manifest.yaml`:
   - Assets: a GitHub org with 2–3 repos; a Jira project (e.g. AUTH) with the decision ticket; a Drive folder of design docs (Google Docs, one PDF, one .docx).
   - Planted links: design doc → PR (URL); PR → AUTH ticket (key in title); PR reviewer; the doc's `**Owner:**` header.
   - One asset per provider that the read credential **cannot** see. CP4's out-of-scope test needs these.
   - One design doc marked "edit after snapshot", for the live `stale` moment in the demo.
7. **Seed as oracle.** `build --expect forge/demo/seed/manifest.yaml` fails when a planted edge is missing from the proposals.
8. **Tests.** Fixture-folder tests for both readers (like `test_microsoft_365`); a `LINKS_TO` test; a test that the Jira-key filter ignores unknown keys.

**Exit criteria**

- [ ] A build on the seed reports 100% ref coverage for github, jira and gdrive, and `--expect` passes.
- [ ] In the existing UI, `/ask` answers "Which PR switched auth providers, and who reviewed it?" with citations to the PR, the AUTH ticket and the design doc. This proves the graph half before any runtime work.
- [ ] Drive permissions arrive as ACLs; a viewer without the group gets the restricted note, not the text.

---

## CP3: Runtime skeleton: MCP front door, broker, `http` executor, GitHub spec

**Goal.** An MCP client connects, searches and walks the graph, and reads live GitHub content through a hand-written spec. This matches step 1 of the design's build order.

**Work**

1. **Graph tools** (`forge/runtime/graph_tools.py`).
   - Extract `search_records(terms, viewer, limit)` from `_keyword` in [answer.py](tesseract/answer.py) (the `Q_SEARCH` and `Q_ENTITY_NODES` part), so `/ask` and `search_brain` share one ACL-filtered query. `/ask` behaviour must not change; the integration tests guard it.
   - `entity(id_or_ref, viewer)` and `neighbors(id, relation, depth<=2, viewer)`.
   - Every returned node passes the `READABLE` rule. Unreadable neighbours come back as stubs (id, ACL groups, `owner_of`), never with titles.
   - Edge traversals filter `supersededAt IS NULL`. No read path does this today.
2. **Broker** (`forge/broker/`).
   - **Vault.** `var/forge/credentials.json` (mode 600, git-ignored) or environment variables, mapping `cred://github-demo` to `{provider, scheme, secret}`.
   - **`Broker.request(handle, method, url, ...)`** is the only HTTP client Forge code may use. It enforces the host allowlist in `forge/policy/hosts.yaml` and read-only methods (GET and HEAD). It injects auth, applies timeouts and per-provider rate limits, and counts calls for budgets. It also has a record/replay hook that CP8 switches on.
   - v1 has an explicit (host, path) allowlist for read-only POSTs, such as GraphQL queries. It starts empty.
   - **`Broker.probe(handle)`** returns granted scopes and rate limits (GitHub `X-OAuth-Scopes`, Jira `/myself`, Drive `about.get`).
   - The snapshot commands from CP2 move onto handles.
   - The broker runs in-process in v1. A network-level egress proxy is phase 2; sandboxed transforms have no network, so in practice the broker is the only way out.
3. **Spec loader and static validator** (`forge/runtime/spec.py`). The YAML schema follows the design. The validator rejects:
   - hosts not on the provider's allowlist;
   - non-read methods;
   - undeclared or missing transforms;
   - unknown executors;
   - JSONPath that does not parse;
   - kinds that are not SourceRef kinds.
4. **`http` executor.**
   - Templated calls, `params`, `by_mime` and `map`.
   - Pagination styles `link_header` (GitHub), `start_at` (Jira) and `page_token` (Drive).
   - Errors normalised: 404 → `NotFound`, 401/403 → `PermissionDenied`, 429 → `RateLimited` (with retry-after).
   - Built-in transforms reuse existing readers: `builtin:docx_text` and `builtin:pdf_text` wrap `read_docx` and `read_pdf`.
5. **Provider** (`forge/runtime/provider.py`). Built from a spec, with `resolve`, `fetch`, `search`, `children`, `changes` and `acl`. An operation the spec does not declare raises `Unsupported`.
6. **Registry** (`forge/runtime/registry.py`). An SQLite table in `var/forge/` holding provider, version, path, status (`served`, `degraded`, `off`), kinds served, conformance report id and timestamps. The runtime reloads when it changes.
7. **Hand-written `forge/specs/github/v1.yaml`.** Kinds `pull`, `issue` and `blob`; `resolve` and `fetch` over REST; blobs fetched with `contents?ref=<sha>`. Registered by hand in CP3; CP4 makes registration require a passing report.
8. **MCP server** (`forge/runtime/server.py`, `python -m tesseract.forge.serve`).
   - **Tools:** `search_brain`, `get_entity`, `get_neighbors`, `read_content`, `describe_sources`. `search_source` and `recent_changes` arrive in CP6. `tools/list` order is deterministic.
   - **Resources:** the template `forge://{provider}/{kind}/{id}`.
   - **Provenance:** every result carries `{ref, url, fetched_at, spec_version, origin}` in `structuredContent`, where `origin` is `live` or `index`. Source text is labelled `trust: "untrusted"` and fenced in the text content.
   - **`get_entity`:** sets `stale: true` when the live `updated_at` is later than the node's `sourceUpdatedAt`.
   - **`read_content`:**
     - Pages through an HMAC-signed handle the server mints, encoding ref, offset and spec version, with a page-size cap.
     - Falls back to the indexed copy (`origin: index`) when no spec serves the provider, so Slack, Outlook and the other sources stay readable.
     - Re-checks the graph ACL before every read. A provider-side per-user check needs per-user credentials (phase 2).
   - **Principal:**
     - stdio: bound to a viewer preset through `FORGE_VIEWER`.
     - HTTP: a bearer token maps to a viewer in the company file.
     - Never a tool argument.
   - **Transports:** stdio (Claude Code, local use) and streamable HTTP.

**Exit criteria**

- [ ] From Claude Code over stdio:
  - `search_brain` finds the seed PR;
  - `get_neighbors` walks doc → PR → ticket;
  - `read_content` returns the live PR body with `origin: live` and `spec_version: github/v1`.
- [ ] A restricted node is never returned with its title or text through any tool. The `/ask` restricted-document test is ported to the MCP tools.
- [ ] Validator unit tests reject an off-allowlist host, a POST and an undeclared transform.
- [ ] The `/ask` integration tests still pass unchanged.

---

## CP4: Inventory, conformance runner, ledger

**Goal.** Any spec can be graded against graph samples it never saw, and every result is recorded. This matches step 2 of the design's build order.

**Work**

1. **Inventory** (code, `harness/inventory.py`). Writes `var/forge/runs/<run>/inventory.<provider>.json` containing:
   - counts by kind;
   - relation types touching those kinds;
   - a stratified sample of 20–50 refs (by kind, format or MIME type, repo, Drive folder);
   - for each sampled ref, what the graph expects: title, `sourceUpdatedAt`, `contentSha`, known children, and a hash of the text prefix.
2. **Fixtures** (`harness/fixtures.py`). Generated from the inventory **before any spec exists** and written to `forge/evals/contract/<provider>/<run>.json`.

   | Test | Passes when | Adaptation to this repo |
   |---|---|---|
   | Resolve | Title matches the graph; `updated_at ≥ sourceUpdatedAt` | Titles compared after normalising whitespace and case |
   | Fetch | Non-empty, valid text, under the size cap; blobs match the pinned SHA | Graph text is capped (60k characters for documents, 20k for issue bodies), so compare a normalised prefix. Blobs compare `sha1("blob <len>\0" + bytes)` with `contentSha`. |
   | Round-trip | `from_url(to_url(ref)) == ref` | Uses `refs.py` from CP1 |
   | Children | Every child the graph knows is listed | Repo → blobs, Drive folder → files |
   | Not found | A fabricated id gives a clean `NotFound` | The id is built in the provider's own id format |
   | Out of scope | An asset outside the credential's reach gives `PermissionDenied` | Uses the planted assets from the seed manifest |
   | Budget | p95 latency and upstream calls per operation are under limits | Limits set in `forge/policy/budgets.yaml` |

3. **Runner.** `python -m tesseract.forge.verify --provider github --spec forge/specs/github/v1.yaml` runs every case through `Provider` and the broker. It writes a report with pass rates per operation, a diff for each failure (expected vs got), p95 latency and calls per operation.
4. **Pass bar.**
   - A kind registers when every required operation passes on at least 95% of samples and every failure carries an explanation code (`deleted_upstream`, `permission_changed`, …).
   - Otherwise the provider is `degraded` and only the passing kinds are served.
5. **Ledger** (`harness/ledger.py`). SQLite, one row per run and provider: stage, status, attempts, strategy per (kind, operation), spec versions, report ids, reasons and timestamps. There is also an append-only event table that the live board reads in CP8.
6. **Registrar.** `python -m tesseract.forge.register github v1` succeeds only if the latest report passes. It versions the spec, writes the registry row, and `describe_sources` reflects the change without a restart.

**Exit criteria**

- [ ] `github/v1` passes at least 95% on the seed, and the ledger shows the run.
- [ ] A spec broken on purpose (title mapped to the wrong field) fails Resolve with a readable diff and cannot be registered.
- [ ] The out-of-scope case returns `PermissionDenied`, not a 500.

---

## CP5: Agent harness, Builder on Jira then Drive, sandbox, repair loop

**Goal.** The Builder writes Jira and Drive specs that pass conformance with no human edits, and the repair loop has run at least once. This matches step 3 of the design's build order.

**Work**

1. **Agent loop** (`harness/agent.py`), kept separate from [llm.py](tesseract/llm.py).
   - Uses the Anthropic SDK tool runner (`client.beta.messages.tool_runner` with `@beta_tool` tools). A per-turn hook writes every tool call and result to the ledger's event table.
   - Model `claude-opus-5-5` by default (overridable with `FORGE_MODEL`).
   - Effort is set explicitly per role, because this model defaults to `medium`: Builder `high`, Planner `high`, Verifier `medium`.
   - Final output uses structured outputs (`output_config.format`) against each role's schema. Forced `tool_choice` is rejected on this model, so do not use it to force a final call.
   - Enable the server-side refusal fallback (`fallbacks: "default"`).
   - Set a maximum turn count and token budget per role.
   - Keep the system prompt and tool list byte-stable so prompt caching works across providers and repair attempts.
2. **Roles** (`harness/roles/`). Each role is a system prompt, a tool allowlist and an output schema, as in the design.
3. **Builder tools.** The Builder never sees secrets (handles only), cannot use a shell, and cannot write outside its draft folder.
   - `http_probe(handle, url, params)`: GET only, through the broker, allowlisted hosts.
   - `read_docs(url)`: allowlisted documentation hosts; content labelled untrusted.
   - `run_spec(spec_yaml, sample_refs)`: validator plus `Provider` on samples; returns per-ref results.
   - `write_transform(name, code)`: writes to the draft folder only.
4. **Keeping the Verifier independent.** Fixtures come from CP4 before the Builder runs, and the Builder never sees them. During repair it sees only failing case ids and diffs. The Verifier agent adds provider-specific cases (id formats, size caps) from the inventory and docs, and never sees the Builder's transcript.
5. **`sandbox` executor.** Agent-written transforms run as `python -I` subprocesses, with JSON over stdin and stdout and CPU and memory rlimits.
   - Under Docker Compose, the sandbox container runs with `--network none`.
   - Run outside Docker, it relies on rlimits and an import allowlist, which is weaker. `docs/FORGE.md` says so.
   - Built-in transforms are preferred over generated ones.
6. **Pipeline** (`harness/pipeline.py`).
   - Stages: inventory → probe → plan → synthesize → verify → register.
   - On failure, repair up to 3 times; after that the provider is degraded.
   - Until CP6, the plan is fixed: `http` for Jira and Drive.
   - If probe shows the scopes cannot cover the inventory's kinds, the run stops and names the missing scope.
7. **Order.** Jira first: it has a smaller API, and its keys are already in the graph. Drive second.
   - The expected repair is that `alt=media` fails Fetch on Google Docs and the Builder switches to `export`.
   - Do not script that mistake. If the Builder gets it right the first time, use whichever real repair the ledger recorded.

**Exit criteria**

- [ ] `python -m tesseract.forge.run --providers jira,gdrive` registers both, or degrades Drive with reasons in the ledger, with no human edits to either spec.
- [ ] The ledger shows at least one repair cycle, with the failing cases and the change that fixed them.
- [ ] **Injection test.** A seeded doc page tells the Builder to call an outside host. If the spec includes that host, the static validator rejects it and the ledger records why.
- [ ] No credential value appears in any spec, ledger row or transcript. A test checks the ledger for the vault's values.

---

## CP6: Planner, `upstream_mcp`, `search_source`, `recent_changes`

**Goal.** Strategy is chosen per (kind, operation) with reuse first, and the tool set is complete. This matches step 4 of the design's build order.

**Work**

1. **`upstream_mcp` executor.**
   - A Python MCP SDK client whose traffic goes through the broker (auth injected at egress, server URLs allowlisted).
   - Negotiates both the new and the 2025-11-25 protocol revisions, and takes argument names from `tools/list`.
   - Do **not** use the Messages API's server-side MCP connector here. It would have Anthropic's servers call the upstream with the token in the request, bypassing the broker (principle 5).
2. **Planner role and `mcp_probe(server, handle)`.**
   - Discovery in priority order: `server/discover` (or initialize) and `tools/list`; then API descriptions (GitHub OpenAPI, Drive Discovery document); then docs.
   - Output: a coverage matrix of candidate × required operation and a strategy per (kind, operation) with rationale. `--confirm-plan` adds the optional human checkpoint.
   - Expected outcome:
     - GitHub `search` uses the official server (the reuse showcase); `resolve` and `fetch` stay on REST, pinned to SHAs.
     - Jira uses REST, unless an official remote server works headless with an API token.
     - Drive uses REST, and the preview MCP server only if you are in the preview program.
3. **`search_source(provider, query)`.** Backed by provider `search`. Each hit is marked with whether its ref is already in the graph.
4. **`recent_changes(since, sources?)`.** Backed by `changes`, with cursors as server-minted handles:
   - GitHub: PRs and issues sorted by `updated` with `since`;
   - Jira: JQL `updated >=`;
   - Drive: the changes API with a start page token.
5. **Optional freshness loop.** Re-snapshot only the changed refs and run `build --keep`. The door's hashing and supersede logic already handle this.

**Exit criteria**

- [ ] The ledger shows a coverage matrix and plan for all three providers.
- [ ] GitHub `search` served through `upstream_mcp` passes conformance.
- [ ] `tools/list` has exactly 7 tools in a stable order, and adding a provider does not change it.

---

## CP7: Retrieval eval

**Goal.** A measured answer to whether the wrapper helps. This matches step 5 of the design's build order.

**Work**

1. **Question set** (`forge/evals/retrieval/`).
   - Single-hop questions from sampled nodes.
   - Multi-hop questions from graph paths, for example doc –`LINKS_TO`→ PR –`REFERENCES`→ ticket, and PR ←`COMMENTED{as: review}`– reviewer.
   - Each question keeps its gold refs. Paraphrase every question, make at least half multi-hop, hand-check a sample, then freeze the set.
2. **Runs.** One fixed consumer agent (same model and prompt) answers through:
   - the wrapper MCP;
   - the raw upstream MCP servers side by side (GitHub's official server, plus Jira and Drive servers where usable; missing ones are marked as such);
   - no tools;
   - existing `/ask` as a reference row.
3. **Metrics.** Recall@5 of gold refs in tool results, answer correctness (LLM judge plus manual spot checks), tool calls per answer and tokens per answer. Output a report in JSON and markdown.

**Exit criteria**

- [ ] One command produces the scorecard end to end.
- [ ] Re-running it in replay mode (CP8) reproduces the same numbers.

---

## CP8: Demo hardening

**Goal.** The demo runs reliably, live or offline.

**Work**

1. **Replay mode.** With `FORGE_REPLAY=1`, the broker serves recorded responses keyed by a hash of method, URL and parameters. Model responses are cached by a hash of the request. Snapshots from CP2 are already replay inputs.
2. **Live board.** Add a Forge view to [ui/](ui/) showing the ledger per provider moving through stages, the coverage matrix and the scorecard. The data comes from new `/forge/ledger`, `/forge/sources` and `/forge/scorecard` endpoints in [api.py](tesseract/api.py).
3. **Script**, adapted from the design:
   1. **Before.** `describe_sources()` shows no live providers. The graph tools still work from the index, so the agent can find nodes but cannot read live content, check staleness or search providers. For a starker contrast, run "before" with no tools.
   2. **Kick off** a Forge run with three credential handles; the board moves live.
   3. **One decision.** The coverage matrix: GitHub search reused from the official server; Jira and Drive generated from REST.
   4. **One repair.** The Drive Fetch failure and its fix.
   5. **Registered.** Three live providers; `tools/list` unchanged.
   6. **After.** Same question. The agent walks doc → PR → ticket, reads both live, and gets `stale: true` on the design doc edited after the snapshot.
   7. **Scorecard** from CP7.
4. **Rehearsal kit.**
   - A reset command: registry emptied, ledger cleared, graph unchanged.
   - Credentials pre-authorised on the morning of the demo.
   - A recorded fallback video.

**Exit criteria**

- [ ] Two consecutive full rehearsals pass: one live, and one in replay mode with the network off.

---

## Rules that hold at every checkpoint

- **One way out.** Only the broker opens network connections from Forge code. A test fails if any module under `tesseract/forge/` other than the broker imports `urllib`, `httpx` or `socket`.
- **No secrets outside the vault.** Specs, ledger rows, transcripts and replay recordings hold handles only. Replay recordings are scrubbed of auth headers.
- **ACL on every result.** Every tool result passes `READABLE` for the bound viewer. Unreadable items appear only as stubs.
- **Untrusted text stays labelled.** Content from sources is labelled untrusted in tool results and in Builder tool results alike.
- **`/ask` does not regress.** The existing unit and integration tests pass at the end of every checkpoint.
- **Docs move with the code.** Each checkpoint updates `docs/FORGE.md`, and [ARCHITECTURE.md](docs/ARCHITECTURE.md) gains a trust-model row for each new control (broker, validator, sandbox, bound principal).

## Phase 2 backlog

| Item | Builds on |
|---|---|
| Write path: `propose_change` and `apply_change` as a new action type on the existing approval door in [actions.py](tesseract/actions.py) (hash-pinned approval and journal already exist), plus an etag or SHA concurrency check, an idempotency key, and `input_required` for confirmation | CP3, actions.py |
| Code mode: the same operations as a typed SDK inside the sandbox | CP3, CP5 |
| M365: driveId and itemId for SharePoint, real ids for Teams and Outlook, delta queries for `changes` | CP1 |
| Slack: channel ids from the export's `channels.json`, so threads get refs | CP1 |
| Notion: snapshot reader and generated spec | CP2, CP5 |
| Vector search added to `search_brain` | CP3 |
| Per-user credentials and a live provider-side ACL check on `read_content` | CP3 |
| A network-level egress proxy in place of the in-process broker | CP3 |

## Risks

| Risk | Where it bites | Mitigation | Watch at |
|---|---|---|---|
| The MCP SDK does not support the stateless revision yet | CP3 transport | Spike in CP0; explicit handles make a later move transport-only | CP0 |
| Drive permissions are not readable with the snapshot credential | CP2 ACLs | Detect and report in the Refs check; mark files `internal`, never guess | CP2 |
| Google auth is awkward headless (OAuth consent vs service account) | CP2, CP8 | Decide in CP2; service account with the folder shared to it is simplest | CP2 |
| The Builder gets Drive right first time, so there is no repair to show | CP5, CP8 | Use whichever real repair was recorded; do not stage one | CP5 |
| Upstream servers speak different protocol revisions | CP6 | `upstream_mcp` negotiates both; `server/discover` doubles as a probe | CP6 |
| Eval questions leak from the same corpus | CP7 | Paraphrase, at least half multi-hop, hand-checked and frozen set | CP7 |
| The first rebuild after CP1 rewrites every node hash | CP1 | Expected once; the default build wipes the namespace | CP1 |
| `answer.py` refactor changes `/ask` results | CP3 | Extract the query only, keep scoring; integration tests guard it | CP3 |

## Open questions

- **Google auth.** Service account (shared folder) or an OAuth user credential? This decides CP2's snapshot setup.
- **Jira deployment.** Cloud (email plus API token, `/rest/api/3`) or Server/Data Center? This decides the Builder's docs and auth scheme.
- **GitHub's official server.** Can its remote endpoint be used headless with a fine-grained token for read-only toolsets? This is checked in CP6, and the reuse showcase depends on it.
- **Spec review.** Who reviews spec diffs before they are served outside the demo? The design requires human review before production.
- **Seed workspace.** Who owns it, and does it live in a dedicated GitHub org, Jira site and Drive?
