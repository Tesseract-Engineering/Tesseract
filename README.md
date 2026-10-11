# Tesseract

**A company brain with hands.** Tesseract connects what a company already knows (Slack, Jira, SharePoint, Outlook, Teams, docs) into one knowledge graph. It answers questions with that full context, each claim quoted from a record the person asking is allowed to read. It can also act on what it finds: looping in the right person, asking an owner for access, or briefing a coding agent to build on something another team already runs. It only acts with your approval.

**Who it's for:** Teams that waste time rebuilding things that already exist, tracking down the source of truth, or sitting through meetings to learn why something was built a certain way. That includes engineering orgs, and also the people we interviewed in banking, compliance, and law. In each of those fields, the knowledge exists but nobody can reach it when they need it.

**Demo:** link

```
  exports ──▶ adapter ──▶ identity + extraction ──▶ the door ──▶ Neo4j graph
                                                                    │
  question ──▶ ground ──▶ search (viewer's ACL) ──▶ gate ──▶ model (optional) ──▶ cited answer
                                                                    │
                                              owners from the graph ──▶ drafts ──▶ approval ──▶ outbox
```

## How it works

1. **Ingest.** An adapter per data layout reads each source's exports and pulls out the important parts: people and their accounts, teams, tickets, threads, documents and who owns them. Every record keeps the access controls it had at the source.
2. **Link.** Identity resolution joins one person's Slack handle, email and Jira name into a single node. Extraction links records to the services, people and customers they name, each with the quote that says so. The separate islands of knowledge become one connected graph.
3. **Answer and act.** You sign in as yourself, so you only see what you're allowed to see. Ask things like "Has anyone here already built a retraining pipeline?" or "How do I get production database access?" Tesseract answers from the graph with citations and proposes actions for you to approve.

## What it does

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
  "suggestions": ["Who owns the billing service?"]
}
```

A **viewer** is a set of ACL groups, and every answer is computed for one. `users` lists who the UI offers to sign in as; it stands in for single sign-on.

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
