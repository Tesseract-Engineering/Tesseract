# ticket_desk

A support inbox where an agent works each customer ticket with the company graph, and the same ticket without it, side by side. With the graph it finds the earlier ticket, incident or thread with the same symptom, checks whether the cause really matches, finds the PR or decision that fixed it and who owns it, and drafts a customer reply and an internal triage note with every claim cited to the graph nodes it came from. For a cause in auth-service it can go further: a branch with the change and a test, tests run, and a draft PR. Nothing is sent, posted or opened until a person approves it.

```
browser ── app.py (Starlette, SSE) ──▶ agent.py ──▶ Claude (tool-use loop)
  inbox, runs, proposals,  │              │
  approve / edit / reject  │              └── forge.py ──MCP stdio──▶ forge_stub/ ──▶ Neo4j (kg:sandbox)
                           │                                          (forge, when F8 ships)
                           └── fix.py ──▶ clone + branch + pytest ──(on approval)──▶ gh pr create --draft
```

| File | What it does |
|---|---|
| `tickets.json` | 13 seed tickets from customers of the Platform team (auth-service, the API gateway, the public API, exports): 7 repeats, 3 near-repeats, 3 new. Each maps to real history in the sandbox graph, or deliberately to none. `expected` is the answer key; only the eval reads it. |
| `forge.py` | The desk's only way to the graph: an MCP client to forge. A `Session` remembers every node the tools returned in one run, with the call that found it, for checking citations and for the provenance panel. |
| `agent.py` | One prompt and answer schema for both modes. `graph`: forge's tools on, about 14 calls, then a reply, a triage note (classification, root cause, history, who to ping, next steps), a known fix (steps that resolved an earlier report, ready to paste) and whether a code fix applies, each claim with `sources`. `baseline`: the same model, prompt and schema, no tools. Cited ids no tool returned are dropped. |
| `fix.py` | The code-fix path: clone the repo onto `ticket-desk/<ticket>`, let Claude read and write `src/` and `tests/` and run pytest (with forge's tools for history), re-run the tests itself, commit locally only if they pass. `open_pr` pushes and opens a draft PR, and only runs on approval. |
| `app.py` | The API and the approval gate: runs are background tasks the browser follows over SSE; they only create proposals. Approve sends a reply to the local outbox, posts a triage note to the action log, or opens the PR. Status and assignee are set by a person (`POST /api/tickets/{id}`); an approved reply moves a ticket to Waiting on customer. State persists in `var/ticket_desk/state.json`. |
| `static/index.html` | The UI, laid out like a support desk: queues with SLA, priority and status; a request view with the conversation thread, a composer whose Reply / Internal note / Code fix tabs hold Tesseract's drafts for Approve / Edit / Discard, a details sidebar (status and assignee are editable), live investigation progress, the side-by-side baseline, and a source drawer for every citation. |
| `../forge_stub/` | A stand-in for forge's MCP server with docs/FORGE.md's contract (`search_brain`, `get_entity`, `get_neighbors`, `read_content`, `describe_sources`), read from Neo4j as one viewer through `graph/swarm_queries.py`. |
| `../eval/check_ticket_desk.py` | Scores the saved runs of both modes against `tickets.json`'s answer key. No model calls. |

## Run it

```bash
docker compose -f swarm/compose.yaml up -d neo4j          # with the sandbox graph built into kg:sandbox
pip install -e '.[desk]'
python -m ticket_desk                                      # http://127.0.0.1:8790
```

`ANTHROPIC_API_KEY` and `NEO4J_PASSWORD` come from the environment or `.env` (Neo4j defaults to compose's `neo4j-local-dev`). `gh` must be logged in, as someone with write access to `tesseract-sandbox/auth-service`, for an approved fix to open its PR. Settings: `TICKET_DESK_VIEWER` (default `ishan.tesseract@gmail.com`), `TICKET_DESK_NS` (`kg:sandbox`), `TICKET_DESK_MODEL` (`claude-opus-5-5`), `TICKET_DESK_EFFORT` (`medium`), `FORGE_COMMAND` (default `python -m forge_stub serve --stdio`; point it at forge when F8 merges), `TICKET_DESK_PORT` (8790).

Finished runs are saved, so reopening a ticket shows its last run at once; **Re-run** works it again live. To start a demo from a clean slate, stop the server and delete `var/ticket_desk/state.json`.

## The demo

The presenter's guide, with the hero tickets, prep and talking points, is [DEMO.md](DEMO.md).

The desk opens on the **All open** queue: key, requester, priority, status, Tesseract's classification, assignee and first-response SLA, sortable, with saved queues on the left (Needs approval, Assigned to me, Waiting on customer, Resolved) and keyboard navigation (`/` search, `j`/`k`, Enter, Esc, `c` to compare).

1. Open **TD-101** (Initech: 401 "no key for kid" for ten minutes after a key rotation). The customer's message heads the thread; while Tesseract works, a status bar shows each graph call (Show steps for the full timeline). It calls this a **near-repeat**: same symptom as the staging rotation incident, but this time the cause is auth-service's own 10-minute JWKS cache (PR #7), not web-app's boot-time fetch, which is why the outage lasted exactly ten minutes. The sidebar names Karthik (PR #7) and Dhruv (rotation, runbook) and lists the linked Jira and GitHub history.
2. Press `c` (**Compare without graph**). Both answers side by side with a scorecard: the baseline reasons well from the symptom, but calls it new, cites nothing, names no one by name, and has no runbook or history.
3. Click any numbered citation: a drawer shows the Slack thread, Jira issue, PR or Drive doc, the forge calls that found it, where the answer uses it, its text read through forge, and a link to open it.
4. In the composer, **Reply to customer** holds Tesseract's draft: **Approve & send**, **Edit** or **Discard**. An approved reply joins the thread, goes to the outbox (Activity & outbox), and moves the ticket to Waiting on customer. **Internal note** holds the triage note the same way.
5. **Code fix** → **Generate fix**: Tesseract clones auth-service, reads PR #7 through forge, adds a rate-limited refetch-on-unknown-kid to `jwks.py` with tests, and the desk reruns the tests. The tab shows the PR title (editable), description with citations, test results and the diff; **Approve: push & open draft PR** pushes `ticket-desk/td-101` and opens the draft PR.

6. **TD-113** (Hooli: billing webhooks returning 401 *again*): a regression. Tesseract links it to Hooli's own CS-2 from earlier this month and the compatibility shim that fixed it (SBX-10, released as v2.15.1), notices the shim is not in auth-service's code, and plans to restore it for the billing audience with a regression test; **Generate fix** builds that PR.
7. **TD-103** (Pied Piper: Okta login loop): a **Fix that worked before** card above the composer, traced to Northwind's CS-6: the five Okta steps and the exact claim name, with **Copy** and **Insert into reply**. The card appears only when the graph shows an earlier report resolved by the customer's own action, and cites it; TD-104 (rate-limit tier) has one too, new tickets never do.

Other good tickets: **TD-105** (the agent cites the current Export Migration Note and flags the Customer FAQ as out of date), **TD-107** (looks like the old Okta claim-stripping bug; the graph shows it is SCIM users missing `email_verified`), **TD-110** to **TD-112** (new: nothing in the graph, and the agent says so).

## What the graph tools are missing

Forge's server (F8) is not built yet, so all five tools are served by `forge_stub` with the documented shapes; switching is `FORGE_COMMAND`. Beyond that, for this desk:

- **Similar-ticket search.** `search_brain` is keyword-only. The agent compensates by searching several wordings, which works on the sandbox but would miss reports phrased differently. A `search_similar` (vector) tool is the gap that matters most here.
- **Owners of a service.** There is no `owners_of(system)`. The agent derives owners from `get_neighbors` (AUTHORED, ASSIGNED_TO, OWNS) and from who resolved earlier tickets.
- **Source code.** The graph holds a repo's docs, not its code, and live reads (F6) are not built. The fix path reads code from its own clone, which is the hands' workspace, not graph context.
- **Refs.** `ref` is null on every node until #4 merges; citations use graph ids and each node's `url`.
- **Writes.** Forge does not write to sources. Replies (to a local outbox) and PRs (with `gh`) belong to the desk, behind its approval gate.
