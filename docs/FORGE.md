# Forge

Forge is the MCP server that lets agents and apps search the company graph and read live content from GitHub, Jira and Google Drive, as one viewer, with provenance on every answer. It sits on top of the swarm (`docs/SWARM.md`) and the graph (`graph/README.md`): the swarm fetches sources into the bucket, the graph builds Neo4j from the bucket, and forge serves both to agents over MCP.

This document is for people building **around** forge: an agent that uses its tools, a front end, `recommend/`, an evaluation. It says what forge will expose when it is finished, what you can use today, and how to build against it in parallel. Forge's internals (the broker, provider specs, the build harness) are documented further down this file as they land.

**The contract in this document is the target for PR F8**, which builds the server. Until F8 merges, the tool shapes below may still change. If they do, F8 updates this file and lists every change in its PR description. After F8, the change rules in [Stability](#stability) apply.

## Status

| Surface | What you get | Available |
|---|---|---|
| Source refs on graph nodes (`ref`, `refProvider`, `refKind`, `contentSha`) | A stable name for every GitHub, Jira and Drive item | In review: #4 |
| `Neo4jStore.read`, `swarm_queries.readable()` / `edge_readable()` | Read the graph and check access without private methods | In review: #5 |
| MCP tools `search_brain`, `get_entity`, `get_neighbors`, `read_content`, `describe_sources` over stdio and HTTP | The forge contract below | F8 |
| Live reads of GitHub PRs, issues, repos and files | `read_content` with `origin: "live"` | F6 (provider), F8 (tool) |
| Forge in the compose stack, reachable from the laptop on `127.0.0.1:8765` | One URL for every local consumer | F9 |
| Live reads of Jira issues and Drive files | Same tools, more providers | F11 |
| `search_source`, `recent_changes` | Search a source directly; what changed since a time | F12 |
| A retrieval scorecard | How well forge answers, against baselines | F14 |

Slack stays **index-only**: its text is in the graph and searchable, but nothing fetches it live. The PR numbers (F1 to F16) refer to `forge_integration_plan.md`.

## Rules for building around forge

1. **Talk to forge over MCP. Do not import `forge`.** No component may import the `forge` package; a test enforces it (`tests/forge_tests/test_boundaries.py`, from F3). This keeps forge replaceable, and keeps you from depending on its internals.
2. **Shared contracts live in the graph, and you may import them.** `graph.refs` (the ref format), `graph.swarm_queries` (the access rule, `viewer()`) and `graph.store.Neo4jStore.read` are public. Forge uses the same modules, so a ref or an access decision means the same thing on both sides of the MCP boundary.
3. **Do not read forge's state.** `var/forge/` (registry, ledger, replay recordings, tokens, the handle key) is private and changes without notice.
4. **The viewer is not an argument.** No tool takes an email or a user id. Forge binds the viewer per connection (see [Who the caller is](#who-the-caller-is)). If your app serves several people, give each one their own token.
5. **Source text is untrusted.** Anything forge returns that came from a source (titles, snippets, bodies) may contain instructions written by anyone who could edit that source. Keep forge's `<source_content trust="untrusted">` wrapper when you pass text to a model, and tell the model it is data.
6. **Need something forge does not offer?** Open an issue or ask @dhruvpendharkar before working around it. Do not query Neo4j behind forge's back for the same thing a tool does: the access filtering would then live in two places.

## Connecting

### Who the caller is

Every call is answered as one **viewer**, an email address. Forge turns it into principals with `graph.swarm_queries.viewer()`: `user:<email>`, `domain:<domain>`, and every group the person is a member of. There is no anonymous mode: with no viewer, every tool returns a `no_viewer` error.

| Transport | How the viewer is set | Use it for |
|---|---|---|
| stdio | `FORGE_VIEWER_EMAIL` in the server's environment: one process, one viewer | Local agents, Claude Code, tests |
| Streamable HTTP | `Authorization: Bearer <token>`. Each token maps to one email. A missing or unknown token gets HTTP 401 | Front ends, shared or containerised use |

Create a token with `python -m forge token add <email>`. It prints the token once and stores only its hash. Whoever holds a token reads as that person, so treat it like a password.

Forge caches each viewer's principals for 60 seconds. Group membership comes from the last graph build, which is nightly.

### Claude Code

Over stdio, with the server started from your checkout:

```json
{"mcpServers": {"forge": {
  "command": "/path/to/Tesseract/.venv/bin/python",
  "args": ["-m", "forge", "serve", "--stdio"],
  "env": {"FORGE_VIEWER_EMAIL": "sam@acme.com",
          "NEO4J_URI": "neo4j://localhost:7687", "NEO4J_PASSWORD": "neo4j-local-dev"}}}}
```

Over HTTP, once forge runs in the compose stack (F9):

```bash
claude mcp add --transport http forge http://127.0.0.1:8765/mcp --header "Authorization: Bearer <token>"
```

F9 confirms the `/mcp` path.

### From Python

Use the MCP SDK's `Client` (`mcp>=2.3,<3`, the version forge pins). It takes a URL (streamable HTTP), `StdioServerParameters`, or, in tests, an `MCPServer` instance to connect to in process.

```python
import sys

import anyio
from mcp import Client, StdioServerParameters

FORGE = StdioServerParameters(command=sys.executable, args=["-m", "forge", "serve", "--stdio"],
                              env={"FORGE_VIEWER_EMAIL": "sam@acme.com", "NEO4J_URI": "neo4j://localhost:7687",
                                   "NEO4J_PASSWORD": "neo4j-local-dev"})


async def main():
    async with Client(FORGE) as forge:
        found = await forge.call_tool("search_brain", {"query": "oidc migration"})
        for hit in found.structured_content["hits"]:
            print(hit["title"], hit["ref"])

anyio.run(main)
```

Forge speaks MCP revision `2026-07-28` and still answers clients that only know the older initialize handshake.

### Install and environment

`pip install -e '.[dev,swarm,graph,forge]'` (Python 3.11+). Forge reads the graph from `NEO4J_URI` and `NEO4J_PASSWORD`, in namespace `FORGE_GRAPH_NS` (default `kg:swarm`, the namespace the graph build writes). Live reads use the same source credentials as the swarm (`.env.example`).

## Tools

Tools are listed in this order, and the order is stable. Every result comes back two ways:

- `structuredContent`: typed JSON, the fields below. Read this.
- Text content: the same facts for a model, with source text inside `<source_content>` wrappers.

### `search_brain`

Keyword search over the graph (Neo4j full-text), as the viewer.

| Argument | Type | Default | Notes |
|---|---|---|---|
| `query` | string | required | Plain words. Forge escapes Lucene syntax, so you cannot pass it operators |
| `labels` | list of strings | all | Node labels, such as `["Issue", "Decision"]` |
| `sources` | list of strings | all | `github`, `jira`, `gdrive`, `slack` |
| `limit` | int | 10 | At most 25 |

```json
{"hits": [
   {"id": "github:repo:7001:pr:412", "labels": ["Issue"], "title": "Switch staff login to OIDC",
    "snippet": "...replaces the SAML flow with OIDC for staff login...",
    "ref": "forge://github/pull/acme/api/412", "url": "https://github.com/acme/api/pull/412",
    "source": "github", "sourceUpdatedAt": "2026-10-14T16:05:00Z", "score": 3.12}],
 "trust": "untrusted",
 "provenance": {"node_id": null, "ref": null, "url": null, "fetched_at": "2026-11-02T09:14:03Z",
                "spec_version": null, "origin": "index"}}
```

Search covers items (`Issue`, `Document`, `Commit`: title, body and text) and named things (`Decision`, `Project`, `ActionItem`, `System`, `Team`, `Customer`, `Container`: name, title and aliases). **It does not find people.** To get from a person to their work, use `get_neighbors` on their node. `ref` and `url` are absent for index-only items such as Slack threads. Search is keyword-only; there is no vector search.

### `get_entity`

One node by graph id or by source ref, as the viewer, with a live freshness check when its provider is served.

| Argument | Type | Notes |
|---|---|---|
| `id_or_ref` | string | A graph id (`github:repo:7001:pr:412`) or a ref (`forge://github/pull/acme/api/412`) |

```json
{"entity": {"id": "github:repo:7001:pr:412", "labels": ["Issue"], "title": "Switch staff login to OIDC",
            "kind": "pull_request", "source": "github", "url": "https://github.com/acme/api/pull/412",
            "ref": "forge://github/pull/acme/api/412", "refProvider": "github", "refKind": "pull",
            "sourceUpdatedAt": "2026-10-14T16:05:00Z", "state": "merged",
            "body": "...", "textTruncated": true, "visibility": "restricted"},
 "stale": false,
 "live": {"title": "Switch staff login to OIDC", "updated_at": "2026-10-14T16:05:00Z",
          "url": "https://github.com/acme/api/pull/412"},
 "trust": "untrusted",
 "provenance": {"node_id": "github:repo:7001:pr:412", "ref": "forge://github/pull/acme/api/412",
                "url": "https://github.com/acme/api/pull/412", "fetched_at": "2026-11-02T09:14:05Z",
                "spec_version": "github/v1", "origin": "live"}}
```

- `entity` holds the node's own properties: the source's fields (Jira status, PR state, and so on, in camelCase), plus `labels`. `text` and `body` are cut to 2,000 characters, with `textTruncated: true`. Use `read_content` for the whole text. Internal bookkeeping (`meta`, `contentHash`, `runId`, provenance) is removed.
- `stale` is `true` when the source changed the item after the graph last read it, `false` when it has not, and `null` when there is no live check (no ref, or no served provider). `live` is `null` in the same cases.
- A node that does not exist and a node the viewer may not read give the **same** `not_found` error. Forge never reveals that something exists.

### `get_neighbors`

The nodes and edges around one node, as the viewer.

| Argument | Type | Default | Notes |
|---|---|---|---|
| `id` | string | required | A graph id. Use `get_entity` first to turn a ref into an id |
| `relation` | string | all | One edge type, such as `CLOSES` |
| `depth` | int | 1 | 1 or 2 |

```json
{"nodes": [{"id": "github:repo:7001:issue:398", "labels": ["Issue"], "title": "SAML login fails behind IdP proxy", "...": "..."}],
 "edges": [{"type": "CLOSES", "from": "github:repo:7001:pr:412", "to": "github:repo:7001:issue:398",
            "props": {"relation": "closes", "via": "closing_keyword", "confidence": 0.95}}],
 "trust": "untrusted",
 "provenance": {"node_id": "github:repo:7001:pr:412", "ref": "forge://github/pull/acme/api/412", "...": "..."}}
```

- Node dicts are shaped as in `get_entity`. Edge `props` are limited to `relation`, `via`, `role`, `confidence`, `quote` and `extractor`.
- At most 50 nodes.
- Only readable nodes and readable, current edges at or above the confidence floor (0.80) are returned. An edge into a node the viewer may not read is **dropped**, not stubbed, so counting a node's edges does not reveal hidden ones.

### `read_content`

The full text of an item, live from the source when forge can fetch it, otherwise from the graph. It is paged.

| Argument | Type | Default | Notes |
|---|---|---|---|
| `id_or_ref` | string | required | As in `get_entity` |
| `handle` | string | none | `next_handle` from the previous page |
| `max_chars` | int | 8000 | At most 20,000 |

```json
{"text": "...", "next_handle": "eyJub2RlX2lkIjoi...", "origin": "live", "trust": "untrusted",
 "provenance": {"node_id": "github:repo:7001:pr:412", "ref": "forge://github/pull/acme/api/412",
                "url": "https://github.com/acme/api/pull/412", "fetched_at": "2026-11-02T09:14:07Z",
                "spec_version": "github/v1", "origin": "live"}}
```

- `origin: "live"`: fetched from the source just now. `origin: "index"`: the text the swarm stored at its last run.
- **Live text and index text can differ in scope.** A live read returns the source's main text: a PR's or issue's body, a file's content. The swarm's stored text for a PR also folds in its reviews and comments.
- Secrets are redacted in both, the same way the swarm redacts them (`[REDACTED:...]`). Office files, PDFs and Jira's rich text are converted to plain text with the swarm's own converters.
- Live text is capped at 200,000 characters.
- **Paging.** Call again with `next_handle` until it is `null`. Handles are signed, tied to one node and one version of its content, and expire after 15 minutes. If the content changes between pages, or the handle has expired, you get a `bad_handle` error. Start again without a handle.

### `describe_sources`

Which providers forge serves, and at which spec version. No arguments.

```json
{"providers": [{"provider": "github", "spec_version": 1, "status": "served", "kinds": ["repo", "pull", "issue", "blob"]}],
 "index_only": ["slack"]}
```

`status` is `served` (every kind passed conformance) or `degraded` (only the listed `kinds` are served). A provider that is not listed is not live: its nodes still come back from the graph, with `origin: "index"`. Check this before promising a user live content.

### Later: `search_source` and `recent_changes` (F12)

- `search_source(provider, query)` searches a source directly (for example GitHub's search API) and marks each hit `in_graph: true|false`.
- `recent_changes(since, sources)` lists what changed in the sources since a time, using cursors carried in handles.

After F12, `tools/list` has exactly seven tools. Their result shapes are settled in F12, and this file is updated then.

### Conventions on every result

- **Provenance.** Each result carries `provenance` with these fields:
  - `node_id`, `ref`, `url`: the node the call was about. They are `null` on `search_brain`, which is about many nodes; cite each hit's own `ref` and `url`.
  - `fetched_at`: UTC, ISO 8601.
  - `spec_version`: such as `"github/v1"` for a live read, `null` otherwise.
  - `origin`: `"live"` or `"index"`.
- **Untrusted text.** Results that carry source text set `trust: "untrusted"`. In the text content, that text is inside `<source_content trust="untrusted" ref="...">...</source_content>`.
- **Errors** come back as MCP tool errors: `isError: true`, with `structuredContent` set to `{"error": {"code": "not_found", "message": "..."}}` and the text content `not_found: ...`. Branch on `code`. The message is for people and may change.

  | Code | Means | What to do |
  |---|---|---|
  | `not_found` | No such node, or the viewer may not read it | Treat both the same |
  | `no_viewer` | The connection has no viewer | Configure `FORGE_VIEWER_EMAIL` or a token |
  | `bad_handle` | A paging handle is tampered, expired, for another node, or for changed content | Call again without the handle |
  | `invalid_argument` | An argument is out of range or malformed | Fix the call |
  | `upstream_error` | The source failed or rate-limited a live read after retries | Retry later, or fall back to graph text |

- **Unknown fields.** Ignore fields you do not recognise. New fields are added without notice.

## The graph, as forge shows it

Forge returns graph nodes as the graph stores them, filtered by access. The vocabulary is `graph/contract.py`'s.

| Label | What it is |
|---|---|
| `Container` | A repo, Slack channel, Drive folder or Jira project |
| `Issue` | A GitHub PR or issue, or a Jira issue |
| `Commit` | A GitHub commit |
| `Document` | A Slack thread, a Drive file, or a file in a repo |
| `Person`, `Account` | A resolved human, and one source identity (a GitHub login, a Slack user) |
| `Group` | Who may read restricted items |
| `Decision`, `Project`, `ActionItem`, `System`, `Team`, `Customer` | Things the swarm's model extracted from text, each with quoted evidence on its `MENTIONS` edges |

Edges between items: `IN`, `REFERENCES`, `CLOSES`, `IMPLEMENTS`, `BLOCKS`, `DUPLICATES`, `PART_OF`, `DEPENDS_ON`. Between people and items: `AUTHORED`, `REVIEWED`, `PARTICIPATED_IN`, `ASSIGNED_TO`, `OWNS`, `HAS_ACCOUNT`, `MEMBER_OF`. Extracted: `MENTIONS`, `DECIDED_IN`, `DECIDED_BY`, `WORKS_ON`, `SUPERSEDES`.

- **Do not parse node ids.** They are the swarm's (`github:repo:7001:pr:412`), and some carry access information. An extracted entity's id ends in its access set, because a decision named in a public repo and in a private channel is two nodes. To identify an item in its source, use its `ref`.
- **Refs** name an item in its source: `forge://github/pull/acme/api/412`, `forge://jira/issue/AUTH-12`, `forge://gdrive/file/<fileId>`, `forge://github/blob/acme/api@<commit>:docs/adr/0002.md`. The full table, and which items have none, is in `graph/README.md` ("Source refs"). Use `graph.refs.parse`, `from_url` and `to_url` rather than string handling: `from_url` turns a link a user pasted into a ref you can pass to `get_entity`.
- **Freshness.** The graph is rebuilt from the swarm's nightly runs. `sourceUpdatedAt` is when the source last changed the item, as of that build. `stale` and `origin: "live"` tell you when the source has moved on since.

## What a viewer sees

Forge applies the graph's access rule (`graph.swarm_queries`) to every node and edge it returns:

- `public` nodes are readable by anyone with a viewer.
- `internal` nodes are readable by company members (anyone whose email belongs to a person in the graph). People, accounts and groups are internal.
- `restricted` nodes are readable by a viewer who holds one of their `aclGroups`.
- Edges extracted from a private item are as private as that item, even when both endpoints are readable.

Two limits to design around:

- **Access is the graph's, as of the last build.** A live read checks that the viewer may read the node in the graph; it does not re-check the source's permissions as that person. Forge reads sources with a service credential. If someone loses access to a document today, forge follows that after the next build. Per-user credentials and a live permission check are planned, not built.
- **Anything you keep outlives the viewer's access.** If you cache forge results, key them by viewer and keep them short-lived.

## Building in parallel, before forge ships

### 1. Read the graph directly, through the public modules

Once #4 and #5 merge, you can read what forge will serve, with the same access rule, using `graph.store.Neo4jStore.read` and `graph.swarm_queries`:

```python
from graph.store import Neo4jStore
from graph.swarm_queries import READABLE, viewer

NS = "kg:swarm"
with Neo4jStore() as store:                      # NEO4J_URI, NEO4J_PASSWORD
    v = viewer(store, NS, "sam@acme.com")
    hits = store.read(
        "CALL db.index.fulltext.queryNodes('knowledgeSearch', $q) YIELD node, score "
        f"WHERE node.ns = $ns AND {READABLE.format(n='node')} "
        "RETURN node.id AS id, node.title AS title, node.ref AS ref, node.url AS url, score "
        "ORDER BY score DESC LIMIT 10",
        q="oidc", ns=NS, member=v["member"], principals=v["principals"])
```

Every query must filter every node with `READABLE` and every edge with `EDGE_READABLE`, because Neo4j has no row-level security. For code that already holds a node's properties, `readable(visibility, aclGroups, v)` is the same rule in Python. Keep these reads in one module of your own, shaped like the tools above, so moving to forge later changes one file.

### 2. Code against the tools, with a fake server

To build an agent or a front end against the real MCP shapes today, run a fake forge in process. The SDK's `Client` connects to an `MCPServer` instance directly, with no subprocess and no network:

```python
from typing import Any

from mcp import Client
from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent

fake = MCPServer("forge")
PR = {"id": "github:repo:7001:pr:412", "labels": ["Issue"], "title": "Switch staff login to OIDC",
      "ref": "forge://github/pull/acme/api/412", "url": "https://github.com/acme/api/pull/412",
      "source": "github", "sourceUpdatedAt": "2026-10-14T16:05:00Z"}


def error(code: str, message: str) -> CallToolResult:
    return CallToolResult(is_error=True, content=[TextContent(type="text", text=f"{code}: {message}")],
                          structured_content={"error": {"code": code, "message": message}})


def provenance(node: dict | None, origin: str = "index") -> dict[str, Any]:
    return {"node_id": node and node["id"], "ref": node and node["ref"], "url": node and node["url"],
            "fetched_at": "2026-11-02T09:00:00Z", "spec_version": None, "origin": origin}


@fake.tool()
def search_brain(query: str, labels: list[str] | None = None, sources: list[str] | None = None,
                 limit: int = 10) -> dict[str, Any]:
    hits = [{**PR, "snippet": "...", "score": 1.0}] if "oidc" in query.lower() else []
    return {"hits": hits, "trust": "untrusted", "provenance": provenance(None)}


@fake.tool()
def get_entity(id_or_ref: str) -> dict[str, Any]:
    if id_or_ref not in (PR["id"], PR["ref"]):
        return error("not_found", "no such node")
    return {"entity": PR, "stale": None, "live": None, "trust": "untrusted", "provenance": provenance(PR)}

# ... get_neighbors, read_content and describe_sources the same way, from your own fixtures.

# In a test:  async with Client(fake) as forge: await forge.call_tool("search_brain", {"query": "oidc"})
```

Return a typed value (`dict[str, Any]`, a `TypedDict` or a Pydantic model), not a bare `dict`. Only a typed return produces `structuredContent`. Return errors as a `CallToolResult`, as `error()` does, the way forge will. A raised exception reaches the client without the structured `code`.

When F8 merges, point the same client code at `StdioServerParameters(... "-m", "forge", "serve", "--stdio")`, or at the HTTP URL, and run it against the sample graph.

### 3. The sample graph

`samples/run-0` is a small company that the graph's tests and forge's tests both use. Build it into a local Neo4j with:

```bash
docker compose -f swarm/compose.yaml up -d neo4j
python -m graph.build_swarm --root samples/run-0
```

Facts you can rely on in it:

- PR `github:repo:7001:pr:412` (ref `forge://github/pull/acme/api/412`) closes issue `github:repo:7001:issue:398`.
- Jira issue AUTH-12 has ref `forge://jira/issue/AUTH-12`.
- A Slack thread decides to use OIDC instead of SAML for staff login.
- Everything in the sample is restricted. `sam@acme.com` and `priya@acme.com` can read the GitHub and Slack items. An email the sample does not know, such as `outsider@example.com`, can read nothing.

Write your tests from these facts, not from the sandbox.

## Stability

From the day F8 merges:

- **Tool names, argument names, the documented result fields and the error codes are the contract.** Changing or removing any of them is a breaking change. It needs a PR that updates this file, says "breaking" in its title, and tags the owners of every component that calls forge.
- **Adding** a tool, an optional argument or a result field is not breaking, and can happen in any PR. Ignore what you do not recognise.
- **Spec versions** (`github/v1`) change when a provider's spec changes. A change in `spec_version` does not change any tool's shape.

## What forge will not do

These are not planned now. Ask before building on any of them.

- Write back to sources (comment, update a ticket): phase 2, with approvals.
- Read Slack live, or serve Notion or Microsoft 365.
- Vector or semantic search.
- Check a source's permissions as the person, rather than as forge's service credential.
- Trigger swarm runs. Freshness stays with the swarm's nightly batch for now.

## Who to ask

Forge: @dhruvpendharkar. The graph, refs and the access rule: @ishan-sinha-ucb. The swarm, source credentials and redaction: @kar722.
