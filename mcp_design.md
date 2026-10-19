# Integration Forge: Agent-Built Wrapper MCP for the Company Brain

Oct 10, 2026 · @Dhruv

## Summary

Integration Forge (working name) takes an existing knowledge graph plus a set of credentials and, in one unattended run, produces a tested wrapper MCP that lets any agent retrieve live context from every source in the graph.

The core bet: **agents write declarative connector specs and small sandboxed transforms against a fixed, hand-written runtime — never whole servers.** Generated specs are diffable, testable and reversible; generated servers are none of those.

The graph does double duty. It tells the harness what to build (which providers, which resource kinds, which operations) and it is the test oracle (every node with a source reference is a test case). The demo shows an empty wrapper become a verified multi-source retrieval layer, then a consumer agent answering a cross-source question it could not answer before, with a measured retrieval score against a baseline.

## Design principles

Six rules drive every decision below; when a choice is unclear, the earlier rule wins.

1. **Generate specs, not servers.** Auth, HTTP, pagination, rate limiting, caching and the MCP protocol are written once by humans. Agents only produce a per-provider connector spec (YAML) and, where unavoidable, pure transform functions. A bad spec fails a test; a bad server fails in production.
2. **Intent-level tools, not endpoint mirrors.** Consumer agents see about eight graph-shaped tools (search, get entity, expand neighbors, read content), not hundreds of API wrappers. GitHub's own server [recommends enabling only the toolsets you need](https://github.com/github/github-mcp-server) to help tool choice and shrink context; a wrapper over four providers has the same problem four times over.
3. **Reuse before generate.** For each provider the harness prefers, in order: an official upstream MCP server, then a machine-readable API description (OpenAPI, Google Discovery), then agent-authored code from docs. Strategies can mix per operation.
4. **The graph is the spec and the oracle.** The harness builds only what graph nodes reference, and tests every build against sampled nodes before it is served.
5. **Agents never hold secrets.** Builders work with credential handles; a broker injects tokens at egress.
6. **Read-only first.** Retrieval ships in v1. Writes are a separate, gated path with its own credentials (see Security).

## System architecture

The system splits into an agent-driven build layer that runs once per source set and a hand-written runtime that serves every query; the versioned spec registry is the only thing that crosses between them.

&#91;embedded content: system architecture · build time above, run time below\]

The build harness reads the graph, the credential broker and a catalog of provider sources, and publishes tested specs. The runtime loads those specs into its adapters; consumer agents only ever talk to the front door, which answers from the graph index and calls upstreams live through the broker.

## Adapter contract and resource refs

Every integration, however it is built, implements one interface keyed on one reference format. That is what lets agents generate integrations without the consumer side ever changing.

**Canonical refs.** Each graph node that points at an external asset carries a `SourceRef`: provider, kind, native id, and an optional version pin. Its URI form is what agents pass around and what MCP resources expose:

```
forge://github/blob/acme/api@3f9c2e1:src/auth/session.py
forge://github/pull/acme/api/412
forge://gdrive/file/1AbC...xyz
forge://notion/page/8c1f...e2
forge://m365/driveItem/b!Xy...9/01ABC...
```

Pin code refs to a commit SHA so a ref keeps meaning the same bytes. If the existing graph stores URLs rather than native ids, the first agent task per provider is a `ref_from_url` normalizer, tested by round-tripping every sampled URL.

**The interface** (Python shown; the runtime calls it, never the consumer):

```python
class Adapter(Protocol):
    provider: str
    def capabilities(self) -> Capabilities: ...            # kinds x ops supported, write support
    def resolve(self, ref: SourceRef) -> Resource: ...     # live metadata: title, updated_at, url, owners
    def fetch(self, ref: SourceRef, fmt: Fmt = "text",
              span: Span | None = None) -> Content: ...    # live content, paged for large items
    def search(self, query: str, kinds: list[str],
               limit: int = 10) -> list[Hit]: ...          # provider-native search, for gaps in the graph
    def children(self, ref: SourceRef,
                 cursor: str | None = None) -> Page: ...   # folders, repos, databases
    def changes(self, since: Cursor) -> ChangePage: ...    # feeds graph freshness
    def acl(self, ref: SourceRef) -> list[Principal]: ...  # who may see it
```

**The connector spec** is what agents actually write. The runtime turns it into an `Adapter` with one of three executors, chosen per operation:

- `upstream_mcp` calls a tool on an official provider MCP server.
- `http` runs a declarative REST or GraphQL call with JSONPath field maps and a named pagination style.
- `sandbox` runs an agent-written pure function (no network, no filesystem) over data the runtime fetched.

```yaml
provider: gdrive
spec_version: 3
auth: { credential: cred://gdrive-demo, scheme: oauth2_bearer }
kinds:
  file:
    match_nodes: { label: Document, where: "source.provider == 'gdrive'" }
    resolve:
      executor: http
      call: GET https://www.googleapis.com/drive/v3/files/{id}
      params: { fields: "id,name,mimeType,modifiedTime,webViewLink", supportsAllDrives: true }
      map: { title: $.name, updated_at: $.modifiedTime, url: $.webViewLink, mime: $.mimeType }
    fetch:
      executor: http
      by_mime:
        application/vnd.google-apps.document:
          call: GET https://www.googleapis.com/drive/v3/files/{id}/export
          params: { mimeType: text/plain }
        default:
          call: GET https://www.googleapis.com/drive/v3/files/{id}
          params: { alt: media }
          transform: sandbox:extract_text   # pdf/docx -> text
    search:
      executor: upstream_mcp
      server: https://drivemcp.googleapis.com/mcp/v1
      tool: search_files                    # arg names come from tools/list, not guessed
tests: generated                            # written by the Verifier, see Verification
```

A static validator checks every spec before it runs: hosts must be on the provider's allowlist, methods must be read-only in v1, and every transform must be declared.

## Wrapper MCP surface

Consumer agents see a small, fixed tool set; adding a provider changes what the tools can reach, not what the tools are.

| Tool | What it does | Backed by |
| --- | --- | --- |
| `search_brain(query, kinds?, sources?, limit)` | Hybrid keyword + vector search over the graph; returns nodes with refs and snippets | Graph index |
| `get_entity(id_or_ref)` | Node properties plus live metadata, with a `stale` flag when the source changed since indexing | Graph + `resolve` |
| `get_neighbors(id, relation?, depth ≤ 2)` | Graph expansion: the PR that implements a doc, the people on a project | Graph |
| `read_content(ref, span?, format?)` | Live content, paged so large files never flood context | `fetch` |
| `search_source(provider, query)` | Provider-native search for assets the graph missed | `search` |
| `recent_changes(since, sources?)` | What changed where, for "what's new on X" questions | `changes` |
| `describe_sources()` | Live providers, kinds, health and spec versions | Spec registry |

Every result carries provenance (`ref`, `url`, `fetched_at`, `spec_version`) in `structuredContent`, and source text is wrapped and labelled as untrusted so the consumer can treat it as data, not instructions.

**Resources.** Each ref is also an MCP resource through a template (`forge://{provider}/{kind}/{id}`), so hosts that load context directly can skip the tool call.

**Protocol revision.** Target the [2026-07-28 MCP revision](https://modelcontextprotocol.io/specification/2026-07-28/changelog), which removes sessions and the initialize handshake: every request carries its own version and capabilities, and servers must implement `server/discover`. Two consequences for this design:

- Because the tool list is fixed, the wrapper rarely needs list-change notifications (now opt-in via `subscriptions/listen`). Returning `tools/list` in a deterministic order, which the revision recommends, keeps client and prompt caches warm.
- Cross-call state (a long `read_content` paging cursor, a pending write) travels as an explicit server-minted handle in tool arguments, which is the revision's replacement for session state.

**Phase 2: code mode.** For heavy consumers, also expose the same operations as a typed SDK inside a sandbox, so an agent writes one script that searches, filters and reads instead of chaining tool calls. Anthropic's [code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp) write-up describes this pattern: intermediate results stay in the execution environment rather than passing through the model's context.

## The build harness

Three agent roles and two deterministic stages turn the graph into registered specs; anything that can be plain code is plain code, so agents spend their judgment only where it is needed.

Each role is a system prompt, a tool allowlist and an output schema, so it runs on whatever agent loop you already have. All roles read and write a shared **build ledger**: one JSON record per provider with status, attempts, chosen strategies, spec versions and test results. The ledger is also what the demo shows live.

| Role | Kind | Sees | Produces |
| --- | --- | --- | --- |
| Planner | Agent, one per run | Inventory, probe report, provider catalog | Strategy per (kind, operation), with rationale |
| Builder | Agent, one per provider, in parallel | Plan, docs, upstream `tools/list`, 20–50 sample refs | Connector spec + transforms |
| Verifier | Agent + test runner, separate context | Inventory, spec, test fixtures; never the Builder's reasoning | Generated tests, pass/fail report |
| Inventory, Probe, Registrar | Code | Graph, broker, registry | Inputs and the final registration |

**The loop**, per provider:

1. **Inventory** (code). Query the graph for each provider's node kinds, counts, relation types and a stratified sample of refs (by mime type, repo, database). Output `inventory.json`.
2. **Probe** (code). The broker validates each credential handle and reports granted scopes and rate limits. If the scopes cannot cover the kinds in the inventory, stop here and say which scope is missing.
3. **Discover** (Planner). Find candidate sources in priority order: official MCP server (call `server/discover` and `tools/list`), API description, docs. Build a coverage matrix of candidate × required operation.
4. **Plan** (Planner). Pick a strategy per kind and operation; mixing is allowed, e.g. GitHub `resolve` over REST but `search` through the upstream server. Optional human checkpoint.
5. **Synthesize** (Builder). Write the spec and any transforms. Builder tools: `http_probe` (GET only, allowlisted hosts, auth injected by the broker), `mcp_probe`, `read_docs`, and `run_spec(sample_refs)` to iterate locally.
6. **Verify** (Verifier). Generate and run the tests in the next section.
7. **Register** (code). Version the spec, hot-load it into the runtime, update `describe_sources`.
8. **Repair.** On failure the Builder gets the failing cases and diffs, up to 3 attempts. After that the provider ships as degraded: only the kinds that pass are served, and the ledger says why.

The Verifier writes its fixtures from the inventory before it sees any spec, so the tests are held out from the work they grade.

## Verification

A spec is served only after it passes contract tests built from graph samples; the retrieval eval then measures whether the whole wrapper actually helps an agent.

**Contract tests** (deterministic, one set per sampled node):

| Test | Passes when |
| --- | --- |
| Resolve | `resolve(ref)` succeeds and title matches the graph; `updated_at` is at or after the graph's value |
| Fetch | Content is non-empty, valid text, under the size cap; code files match the pinned blob SHA |
| Ref round-trip | `ref → url → ref` returns the same ref |
| Children | Listing a container returns every child the graph knows about |
| Not found | A fabricated id returns a clean not-found error, not a 500 |
| Out of scope | An asset outside the credential's reach returns a permission error |
| Budget | p95 latency and upstream calls per operation stay under set limits |

Proposed bar to register a kind: every required operation passes on at least 95% of sampled nodes, with each failure explained in the ledger (deleted upstream, permission changed).

**Retrieval eval** (end to end, with an LLM):

- From sampled nodes, generate questions answerable only from that node's content, plus multi-hop questions built from graph edges ("which PR implemented the decision in the auth design doc, and who reviewed it?"). Each question keeps its gold refs.
- Run a fixed consumer agent three ways: wrapper tools, the raw upstream MCP servers side by side, and no tools.
- Score recall@5 of gold refs in tool results, answer correctness (LLM judge plus spot checks), tool calls and tokens per answer.

Generation and answering use the same corpus, so paraphrase the questions and weight multi-hop ones; single-hop lookups flatter every system equally. The wrapper-versus-raw-MCP comparison is the headline number for the demo.

## Security, permissions and the write path

The biggest risk is not a broken integration but an agent, steered by text inside a document it read, using real credentials; the design contains that by keeping secrets, network and writes out of agent hands.

**Credentials.** Secrets live in a vault behind the broker. Agents and specs reference `cred://` handles only. All outbound calls, at build time and run time, go through an egress proxy that injects auth, enforces the provider host allowlist and, in v1, blocks non-read methods. Use separate credentials for build (read-only, narrow) and, later, for writes.

**Sandboxing.** Transforms run with no network, no filesystem and CPU and memory limits; they get JSON in and return JSON out. Builders cannot run arbitrary shell; their only reach is through the probe tools above.

**Prompt injection.** Builders read docs and sample content, and consumers read everything, so both are exposed. Google's own Drive MCP guide [warns about indirect prompt injection](https://developers.google.com/workspace/drive/api/guides/configure-mcp-server) from untrusted documents. Mitigations here: content is labelled untrusted in tool results; the static spec validator rejects any host or method outside policy no matter what the Builder was persuaded to write; spec diffs go to human review before production.

**Permissions.** A graph built with an admin token can leak: a consumer asking on behalf of an intern should not see the board deck. Store ACLs on nodes at index time (via `acl`), filter `search_brain` and `get_neighbors` by the caller's principal, and re-check live on `read_content`. The demo can run as one identity, but every runtime call should already take a `principal` argument so this is not a rewrite later.

**Write path (phase 2).** Writes go through two steps, never one:

1. `propose_change(ref, op, payload)` validates the change and returns a diff and a `change_id` handle.
2. `apply_change(change_id)` executes it with an idempotency key and an optimistic-concurrency check (blob SHA, etag or Notion `last_edited_time`), then writes an audit record.

Approval rides on the new protocol's multi round-trip pattern: the server returns an `input_required` result asking for confirmation, and the client retries with the answer ([changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog)). Policy decides which changes need a human.

## Provider notes

For the demo, pick a mix that exercises all three strategies: GitHub through its official server (reuse), Drive and Notion over REST (generation), and Microsoft 365 as a stretch generated from the Graph API.

| Provider | Upstream MCP to reuse | Native API for gaps | Ref key | Watch out for |
| --- | --- | --- | --- | --- |
| [GitHub](https://github.com/github/github-mcp-server) | Official server, remote at `api.githubcopilot.com/mcp/` or local Docker; toolsets selectable; `--read-only` skips write tools | REST + GraphQL | `owner/repo@sha:path`, issue/PR number | Pin refs to commit SHAs; search endpoints have tighter rate limits than reads |
| [Google Drive](https://developers.google.com/workspace/drive/api/guides/configure-mcp-server) | Google's remote Drive server, 8 tools incl. `search_files` and `read_file_content`; scopes `drive.readonly` + `drive.file` | Drive API v3 | `fileId` | Server is in Developer Preview and needs program membership, so keep a REST path ready; Google-native docs need `export`, not `alt=media`; shared drives need `supportsAllDrives` |
| [Notion](https://getunblocked.com/blog/notion-mcp/) | Hosted server at `mcp.notion.com/mcp`, browser OAuth, returns Markdown | Notion REST API with an integration token | page / data source id | Browser OAuth is awkward for headless agents, so an integration token over REST is simpler for the demo; the integration must be shared to each page |
| [Microsoft 365](https://www.scalekit.com/blog/microsoft365-mcp-vs-api) | No single official server covers the suite; the productivity servers need Agent 365 and Copilot licensing | Microsoft Graph v1.0 | `driveId` + `itemId` | App permissions such as `Files.Read.All` need admin consent; use delta queries to feed `changes` |

The Drive row doubles as a good repair demo: a first spec that fetches Google Docs with `alt=media` fails the fetch test, and the Builder fixes it by switching to `export`.

## Demo plan

The demo is a before-and-after on one hard question: an agent fails it with an empty wrapper, the harness builds the integrations live, and the same agent answers it with citations.

**Setup.** Seed a small demo workspace your graph code indexes ahead of time: one GitHub org with 2–3 repos, a Drive folder of design docs, a Notion space of meeting notes and decisions. Plant cross-source links on purpose (a design doc that names a decision, a Notion page that links the PR, a PR that touches the file) so multi-hop questions have real answers.

**Script.**

1. **Before.** `describe_sources()` is empty. Ask the consumer agent the headline question, e.g. "Why did we switch auth providers, which PR did it, and is the design doc still accurate?" It cannot answer.
2. **Kick off.** Start a Forge run with the graph and three credential handles. Show the build ledger as a live board: each provider moving through discover, plan, build, verify.
3. **One decision, narrated.** GitHub reuses the official server; Drive and Notion are generated from the REST APIs. Show the Planner's coverage matrix.
4. **One failure, repaired.** The Drive fetch test fails on Google Docs; the Builder switches to `export` and the rerun passes.
5. **Registered.** `describe_sources()` now lists three providers; the tool list is unchanged.
6. **After.** Same question. The agent searches the brain, walks to the PR and the doc, reads both live, flags that the doc predates the PR, and answers with refs.
7. **Scorecard.** Contract pass rates per provider, and recall@5, accuracy and tokens per answer for wrapper versus raw upstream servers versus no tools.

**Make it robust.**

- Record a full successful run as a fallback video.
- Add a replay mode: recorded HTTP fixtures plus cached model responses, so the live run is deterministic and works offline.
- Pre-authorize every OAuth credential the morning of the demo.
- Keep the Drive REST path even if you plan to show the Drive MCP server, since that server is in preview.

## Build order, risks and open questions

Build the runtime with one hand-written spec first; only then let agents write specs, because the agents need a working contract and test runner to aim at.

```
forge/
  runtime/              # hand-written: the wrapper MCP
    server.py           # MCP front door, 2026-07-28 revision
    tools/              # search_brain, get_entity, read_content, ...
    executors/          # upstream_mcp.py, http.py, sandbox.py
    broker/             # credential handles, egress proxy
    policy/             # ACL filter, audit log, budgets
  specs/                # agent-generated, versioned
    github/v1.yaml
    gdrive/v3.yaml
    gdrive/transforms/
  harness/
    roles/              # planner, builder, verifier: prompt + tools + output schema
    pipeline.py         # inventory -> probe -> discover -> plan -> synthesize -> verify -> register
    ledger/
  evals/
    contract/           # generated from graph samples
    retrieval/          # questions + gold refs
  demo/
    seed/
    replay/             # recorded HTTP + model fixtures
```

**Build order.**

1. Runtime, broker and the `http` executor, with a hand-written GitHub spec. Proves the contract end to end.
2. Contract test runner and fixture generation from the graph.
3. Builder agent on Drive, with the repair loop.
4. Planner, the `upstream_mcp` executor, and Notion.
5. Retrieval eval with the baseline comparison.
6. Demo seed, replay mode, scorecard view.

**Risks.**

- **Ref quality.** If graph nodes lack stable native ids, everything downstream wobbles. Check this first.
- **Protocol versions.** Upstream servers may still speak the 2025-11-25 revision; the upstream executor must negotiate both, and `server/discover` doubles as a compatibility probe. Check your MCP SDK's support for the stateless revision before committing to it.
- **Preview dependencies.** The Drive MCP server is in Developer Preview.
- **Eval leakage.** Questions generated from the same corpus overstate recall; weight multi-hop and paraphrased questions.

**Open questions.**

- [ ] What graph store and language is the existing knowledge-graph code in? That sets the runtime's language.
- [ ] Which agent harness will run the roles?
- [ ] Is per-user ACL filtering in scope for the demo, or one identity?
- [ ] Is Microsoft 365 in the demo or a stretch goal?

## Sources

- [MCP 2026-07-28 changelog](https://modelcontextprotocol.io/specification/2026-07-28/changelog)
- [GitHub MCP Server README](https://github.com/github/github-mcp-server)
- [Configure the Drive MCP server (Google)](https://developers.google.com/workspace/drive/api/guides/configure-mcp-server)
- [Notion MCP: hosted vs. self-hosted (Unblocked)](https://getunblocked.com/blog/notion-mcp/)
- [Microsoft 365 MCP, API, or hybrid? (Scalekit)](https://www.scalekit.com/blog/microsoft365-mcp-vs-api)
- [Code execution with MCP (Anthropic)](https://www.anthropic.com/engineering/code-execution-with-mcp)
