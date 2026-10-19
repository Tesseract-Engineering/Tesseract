# Tesseract

**A company brain with hands.** Tesseract connects what a company already knows (Slack, Jira, SharePoint, Outlook, Teams, docs) into one knowledge graph. It answers questions with that full context, each claim quoted from a record the person asking is allowed to read. It can also act on what it finds: looping in the right person, asking an owner for access, or briefing a coding agent to build on something another team already runs. It only acts with your approval.

**Who it's for:** Teams that waste time rebuilding things that already exist, tracking down the source of truth, or sitting through meetings to learn why something was built a certain way. That includes engineering orgs, and also the people we interviewed in banking, compliance, and law. In each of those fields, the knowledge exists but nobody can reach it when they need it.

**Demo:** https://drive.google.com/drive/folders/1n6st9z9S7U43VDHhq-9CAqXOSqdBpIXu?usp=drive_link

```
  sources ──▶ agent fleet ──▶ linking layer ──▶ the door ──▶ Neo4j graph
                                                                    │
  question ──▶ ground ──▶ search (viewer's ACL) ──▶ gate ──▶ model (optional) ──▶ cited answer
                                                                    │
                                              owners from the graph ──▶ drafts ──▶ approval ──▶ outbox
```

## How it works

1. **Ingest.** A fleet of agents fans out across the tools a company already uses (GitHub, Notion, Slack, Jira and more). Each agent speaks its system's language and reads the code, docs, conversations and decisions there. It pulls out what matters (services, owners, decisions and dependencies) and turns it into nodes. At first these are fragments: one island of knowledge per source. Every record keeps the access controls it had at the source.
2. **Link.** A linking layer sweeps across every fragment and finds where they describe the same thing. The same service might show up in a pull request, a Slack thread and a design doc; it is stitched into one node. Identity resolution does the same for people, joining one person's Slack handle, email and Jira name. Piece by piece, the islands merge into a single living map of every service, owner and decision.
3. **Answer and act.** You sign in as yourself, so you only see what you're allowed to see. Ask things like "Has anyone here already built a retraining pipeline?" or "How do I get production database access?" Tesseract answers from the graph with citations and proposes actions for you to approve.

## What it does

- **Extracts with a swarm of agents.** One agent per source, each fluent in that system's format, extracts services, owners, decisions and dependencies in parallel. A linking layer then merges what they found into one graph, with provenance on every fact.
- **Answers with evidence.** Every answer is `INFO` (with claims that each quote a record) or `NOT_FOUND` (with what was searched and the nearest misses, labelled as misses). It never answers from nowhere.
- **Respects access.** Each record keeps the ACL of its source: Confidential SharePoint files, private Slack channels, mailboxes. A record the viewer may not read is counted and named ("2 matching documents are restricted to Security"), never quoted.
- **Finds the right person.** Owners come from the graph: document headers, ticket assignees, authors and thread participants. People who have left are skipped and named.
- **Drafts the next step, and waits.** Email drafts, Slack drafts, and a coding-agent brief for build requests are proposed with an exact-payload hash. Nothing runs until a person approves that payload, and execution only writes a draft to the outbox. Nothing is sent.
- **Says "someone already built this".** "Help me design X" is first asked as "has anyone here built X?". When they have, the answer leads with it, draws the existing system's design from the cited records, and offers to brief a coding agent to build on it.

See [docs/SCENARIOS.md](docs/SCENARIOS.md) for the three workflows it is built around.

## Quick start

You need Docker and a folder of company exports in the [workspace layout](docs/ADAPTERS.md).

```bash
cp .env.example .env          # set DATA_DIR, NEO4J_PASSWORD, and optionally ANTHROPIC_API_KEY
docker compose up --build
```

- UI: http://localhost:8080
- API: http://localhost:8091 ([reference](docs/API.md))
- Neo4j Browser: http://localhost:7474

The first start builds the graph from `DATA_DIR`. Later starts reuse it; set `FORCE_REBUILD=1` to rebuild.

Without a model key, Tesseract answers with deterministic keyword search alone. With one, a model expands the search, picks evidence, and a second pass verifies each quote. The model never touches the graph.

### Local development

```bash
python -m venv .venv && .venv/bin/pip install -e '.[dev]'
export NEO4J_PASSWORD=... TESSERACT_DATA=/path/to/exports TESSERACT_NS=tesseract:acme
.venv/bin/python -m tesseract.build            # build the graph
.venv/bin/python -m tesseract.api              # http://localhost:8091

cd ui && corepack pnpm install && corepack pnpm dev   # http://localhost:5173
```

## Configuration

Settings come from the environment (all listed in [.env.example](.env.example)). What is specific to one company lives in a **company file**, `company.json`, kept in the data folder or named by `TESSERACT_COMPANY`:

```json
{
  "name": "Example Co",
  "email_domain": "example.com",
  "default_viewer": "employee",
  "viewers": {
    "employee": {"label": "Employee", "acl_groups": []},
    "security": {"label": "Security", "acl_groups": ["sharepoint:security"]}
  },
  "users": [{"name": "Jane Doe", "team": "Payments", "viewer": "employee"}],
  "suggestions": ["Who owns the billing service?"],
  "jira_base_url": "https://example.atlassian.net"
}
```

A **viewer** is a set of ACL groups, and every answer is computed for one. `users` lists who the UI offers to sign in as; it stands in for single sign-on. `jira_base_url` gives Jira tickets a link (`<base>/browse/<KEY>`).

## Tests

```bash
.venv/bin/pytest                       # unit tests; Neo4j tests skip when it's unreachable
NEO4J_PASSWORD=... .venv/bin/pytest    # adds end-to-end tests in a throwaway namespace
.venv/bin/ruff check tesseract tests
cd ui && corepack pnpm test && corepack pnpm lint && corepack pnpm build
```

## Layout

```
tesseract/
  api.py          JSON API
  answer.py       grounding, search, the gate, and the model layer
  search.py       model prompts: expand, select, verify, headline
  grounding.py    names in a question -> nodes in the graph
  access.py       viewers and the ACL rule
  hands.py        owners from the graph -> drafted actions
  design.py       system design diagram of existing work
  actions.py      the approval door and the outbox
  querylog.py     questions asked, written back as usage structure
  build.py        exports -> graph (identity, extraction, provenance)
  adapters/       data layouts: workspace (standard), generic (fallback)
  connectors.py   readers for Slack, Jira, SharePoint, Outlook, Teams, git, docs
  contract.py     the ontology, and what the door admits
  ingest.py, load.py, store.py, resolve.py, matching.py, corpus.py
ui/               React + TypeScript web app
docs/             architecture, adapters, API, scenarios
tests/            pytest suite
```

Read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for how the pieces fit and why.

## License

MIT. See [LICENSE](LICENSE).
