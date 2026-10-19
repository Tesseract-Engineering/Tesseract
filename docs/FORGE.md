# Integration Forge

The Forge puts live providers (GitHub, Jira, Google Drive) behind one MCP server, next to the graph. Agents search and walk the graph, then read live content through **provider specs**: declarative YAML that a hand-written reference (GitHub) or the Builder agent (Jira, Drive) produces and a conformance run grades against the graph before it is served.

The design and the checkpoint plan (CP0 to CP8) are kept outside the repo. This page records what exists and the decisions made along the way; it grows with each checkpoint.

## Install

```bash
pip install -e '.[dev,forge]'     # Python 3.11+
```

The `forge` extra adds the MCP SDK, `pyyaml` and `httpx`. The base install and the API's Docker image do not include it, and nothing on the `/ask` path imports `tesseract.forge`.

## Vocabulary

Several design terms already mean something else in this repo. The Forge uses the names in the middle column.

| Design term | Name here | Existing term it would collide with |
|---|---|---|
| `Adapter` (live provider interface) | **Provider** | `adapters/`, the export-layout readers |
| Connector spec | **Provider spec** | `connectors.py`, the export file readers |
| Contract tests | **Conformance tests** | `contract.py`, the ontology |
| Build harness / build | **Forge run** | `build.py`, the graph build |

"The door" still means [ingest.py](../tesseract/ingest.py), and "approval door" still means [actions.py](../tesseract/actions.py).

## Layout

```
tesseract/forge/          code (runtime/, broker/, harness/)
forge/                    data: policy, specs, eval sets, demo seed (see forge/README.md)
var/forge/                git-ignored: credentials, registry, ledger, runs, replay
```

## Spike results (CP0, Oct 19, 2026)

### MCP protocol revision

| Peer | Version | Negotiated |
|---|---|---|
| Python MCP SDK client, `mode="auto"` (stdio) | `mcp` 2.3.0 | **2026-07-28** (stateless, via `server/discover`) |
| Python MCP SDK client, `mode="legacy"` (stdio) | `mcp` 2.3.0 | 2025-11-25 (initialize handshake) |
| Claude Code, `claude -p --mcp-config` (stdio) | 2.1.285 | **2026-07-28** |

**Decision: target 2026-07-28.** The server is built on `mcp.server.mcpserver.MCPServer` (the 2.x name for `FastMCP`), which answers both eras, so older clients still connect through the handshake. All cross-call state (content pages, change cursors) still goes in server-minted handles, as the design asks, so nothing depends on transport sessions.

Notes for CP3:

- A tool returning a bare `dict` produces text content only. A typed return (`dict[str, ...]`, a `TypedDict` or a Pydantic model) also produces an output schema and `structuredContent`, which provenance relies on.
- Tools receive the request context by declaring a `Context` parameter; `ctx.protocol_version` gives the revision per request.

The spike lives in `tests/forge_spike_server.py`, and `tests/test_forge_spike.py` re-checks both SDK rows on every test run. To repeat the Claude Code row:

```bash
echo '{"mcpServers":{"spike":{"command":"'$PWD'/.venv/bin/python","args":["'$PWD'/tests/forge_spike_server.py"]}}}' > /tmp/spike.json
echo "Call the ping tool once and reply with only the JSON it returned." \
  | claude -p --strict-mcp-config --allowedTools mcp__spike__ping --mcp-config /tmp/spike.json
```

### Anthropic SDK

The pinned `anthropic>=0.30,<1` resolves to **0.125.0**, the last 0.x release, and it has what the agent harness (CP5) needs. **Decision: keep the pin; no move to 1.x.**

| Need | In 0.125.0 |
|---|---|
| Tool runner | `client.beta.messages.tool_runner` and `@beta_tool` |
| Structured outputs | `output_config={"format": ...}` on `messages.create` and `beta.messages.create` |
| Effort per role | `output_config={"effort": ...}` (not a top-level parameter) |
| Refusal fallback | `fallbacks=` on `beta.messages.create` only |

So the harness calls the beta surface throughout. Because we stay on 0.x, the SDK and the broker's `httpx` client can share the `httpx` package; the broker still builds its own client and never reuses the SDK's.

## JSONPath

Specs address response fields with a small in-house subset, [jsonpath.py](../tesseract/forge/runtime/jsonpath.py): `$`, `.name`, `['any key']`, `[0]`, `[-1]` and `[*]`. Filters, slices, recursive descent and scripts do not parse, and the static validator (CP3) rejects any spec field that does not parse.

## Source refs

A **SourceRef** names an item in a live provider, in a form that can be compared, fetched and linked. It is defined in [refs.py](../tesseract/refs.py):

| Provider | Kind | URI | Link |
|---|---|---|---|
| github | blob | `forge://github/blob/acme/api@<sha>:src/auth/session.py` | `https://github.com/acme/api/blob/<sha>/src/auth/session.py` |
| github | pull, issue | `forge://github/pull/acme/api/412` | `https://github.com/acme/api/pull/412` |
| github | repo | `forge://github/repo/acme/api` | `https://github.com/acme/api` |
| jira | issue | `forge://jira/issue/AUTH-42` | `<jira_base_url>/browse/AUTH-42` |
| gdrive | file, folder | `forge://gdrive/file/<fileId>` | `https://drive.google.com/file/d/<fileId>/view` |

`refs.from_url` also accepts the links people paste (Google Docs, Sheets and Slides hosts, `open?id=`, PR `/files` tabs, query strings, fragments). A `blob/<branch>/` link takes the first segment after `blob` as the version, so a branch name containing a slash is misread. Refs that Tesseract writes always pin a SHA.

A connector sets `SourceRecord.ref`, and `load.artifacts` writes these properties on Commit, Issue and Document nodes:

| Property | Value |
|---|---|
| `ref`, `refProvider`, `refKind` | the URI and its parts (only when the record has a ref) |
| `url` | the record's link, for every artifact type (Documents used to drop it) |
| `sourceUpdatedAt` | when the source last changed the item, from connector `meta["updatedAt"]` |
| `contentSha` | content hash the source reports (git blob SHA), from `meta["contentSha"]` |

`ref` is indexed per artifact label. Every build prints a **Refs** section after Coverage: records with a ref per live source, every record whose link parses to a different ref than it carries, and the index-only sources. Jira gets refs today. GitHub and Drive get them in CP2, and Slack, Outlook, Teams, SharePoint and `docs/` stay index-only.

Because node hashes include their properties, the first rebuild after this change rewrites every artifact node once.

## Status

| CP | Name | State |
|---|---|---|
| 0 | Foundations | done |
| 1 | Source refs in the graph | done (integration test not run locally; needs Neo4j) |
| 2 | GitHub and Drive ingest, link edges, seed | not started |
