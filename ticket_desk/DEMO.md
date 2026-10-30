# Ticket Desk: demo guide

How to run the demo, which tickets to show, and what to say about how it works. For the code layout, see [README.md](README.md).

## The pitch in one line

The same model, prompt and answer format, with and without the company graph. With it, the agent recognises a ticket it has seen before, finds the fix and the people, and cites every claim. Without it, the agent reasons well but has nothing to go on. Nothing leaves the desk until a person approves it.

## Before you present

```bash
docker start swarm-neo4j-1               # Neo4j must be up, or source lookups hang
cd ~/Desktop/Tesseract-ticket-desk
.venv/bin/python -m ticket_desk          # http://127.0.0.1:8790
.venv/bin/python -m eval.check_ticket_desk   # optional: the scorecard over the saved runs
```

- **Use the saved runs.** Every ticket has a finished run of both modes, so it opens instantly. A live **Re-run** takes about a minute and the wording varies between runs. Show one live only if you want the audience to watch the agent work.
- **API credits** are needed for Re-run and Generate fix only. Saved runs work without them.
- **The PR moment.** TD-101 (PR #10) and TD-113 (PR #9) already have draft PRs on `tesseract-sandbox/auth-service`. To approve a fix live again, close the PR and delete its `ticket-desk/td-1xx` branch first, or show the existing PR.
- **Start clean.** To clear approvals and the outbox, stop the server and delete `var/ticket_desk/state.json`. That also deletes the saved runs, so every ticket must then be run again live.

## The hero tickets

Show them in this order, in about five minutes.

### 1. TD-104, Soylent: 429s on the public API (a quick, grounded answer)

*Repeat, with a paste-ready fix.*

- **What the customer says:** their nightly sync of about 300 requests a minute started getting 429s two weeks ago. Did we lower their limits?
- **What Tesseract finds:** rate limiter v2 shipped then, with a default of 100 a minute per key ("API Rate Limiting v2 - Design"). Globex hit the same thing in CS-3, and Karthik fixed it by moving them to the higher-tier key. Karthik is the person to ping.
- **What to show:** the **Fix that worked before** card, with the exact value to copy ("Higher tier: 1000 requests/minute per API key"). Then **Insert into reply** and **Approve & send**.
- **The point:** a ticket that would take an engineer half an hour of digging is answered in one click, with the source.

### 2. TD-101, Initech: 401 "no key for kid" after a key rotation (the comparison)

*Near-repeat: the same symptom as before, with a different cause.*

- **What the customer says:** for ten minutes after our key rotation, new tokens failed with `no key for kid 'k2'`.
- **What Tesseract finds:** it looks like last week's staging dry-run incident (OPS-8, the postmortem). But that incident was web-app fetching the JWKS once at boot. This one is auth-service's own 10-minute JWKS cache from PR #7, which is why it lasted exactly ten minutes. It names Dhruv (the rotation runbook) and Karthik (PR #7).
- **What to show:**
  - Press `c` to **Compare without graph**. The baseline calls it new, cites nothing and names no one.
  - Click a numbered citation. The drawer shows the source's text read through forge, the call that found it, where the answer uses it, and **Open in GitHub/Jira/Slack/Drive**.
  - Open the **Code fix** tab: a refetch of the JWKS on an unknown kid, rate-limited, with tests that pass, and its diff. That is draft PR #10.
- **The point:** the graph does not just match keywords. The agent checked whether the cause matched, and it did not.

### 3. TD-113, Hooli: billing webhooks returning 401 *again* (a regression that needs a code change)

*Repeat, and the old fix is gone.*

- **What the customer says:** this happened earlier this month, you patched it, and now it is back since your latest release.
- **What Tesseract finds:** Hooli's own CS-2. The cause then was the namespaced-claims change (SBX-4, PR #4), which dropped the plain `tenant_id` claim. The fix was a compatibility shim (SBX-10, v2.15.1). The agent checks auth-service's code, sees the shim is not there, and calls it a regression.
- **What to show:** the history in the sidebar, the internal note, then **Code fix**: restore the shim for the billing audience, with a regression test. That is draft PR #9.
- **The point:** the baseline also says "repeat", because the customer said so. But it cannot say what the fix was, whether it is still in place, or who did it.
- **Caveat:** PR #9 uses a placeholder audience name, `billing-webhooks`, which is flagged in the PR for review.

### 4. TD-103, Pied Piper: Okta login loop (the fix you can paste)

*Repeat, and the customer fixes it themselves.*

- **What the customer says:** Okta users loop between Okta and our login page. They set Okta up from the old guide.
- **What Tesseract finds:** Northwind's CS-6, from about a week ago, had the same cause. Their Okta app still mapped the old plain claim names. Ishan resolved it by changing their Okta mapping to the namespaced claims.
- **What to show:** the **Fix that worked before** card, with the Okta steps and the exact claim (`https://tesseract.example/claims/tenant_id`), **Copy**, and the trace to CS-6. Note that the agent says no code change is needed, because PR #4 is merged and there is no revert.
- **The point:** the agent separates "we need to change code" from "the customer needs to change a setting".

### 5. TD-110, Aperture: PKCE for a mobile app (it doesn't make things up)

*New.*

- **What Tesseract finds:** nothing. The graph has no earlier report, ticket, PR or doc about PKCE, and the agent says so. It shows no "fix that worked before" card, cites nothing, and gives low confidence.
- **The point:** grounding cuts both ways. When the graph has nothing, the agent does not invent history.

### Backups if there are questions

| Ticket | What it shows |
|---|---|
| **TD-105** Umbrella: CSV export returns 410 | It cites the current Export Migration Note and flags the Customer FAQ as out of date. |
| **TD-107** Hooli: `email` claim missing | The customer thinks it is the old Okta bug (SBX-7). The graph shows it is SCIM-provisioned users without `email_verified` (SBX-13, open). |
| **TD-106** Cyberdyne: discovery doc 403 in production | The `/.well-known/*` allowlist was applied on staging only. OPS-2 was closed after the staging change, and infra#1 is still open. It names Dhruv. |
| **TD-111**, **TD-112** | More new tickets, with nothing invented. |

## How it works (for the "how?" question)

```
ticket ──▶ agent (Claude) ──MCP──▶ forge tools ──▶ company graph (Neo4j)
             │                      search_brain, get_entity, get_neighbors,
             │                      read_content, describe_sources
             ▼
   reply · internal note · known fix · code-fix plan   (every claim cites graph ids)
             │
             ▼
   approval gate ──▶ outbox / action log / git push + draft PR
```

1. **The graph.** The sandbox company's Jira, GitHub, Slack and Drive are ingested into one graph: tickets, PRs, threads and docs, connected to the people, systems and decisions they mention. The desk reads it as one viewer (Ishan) and sees only what Ishan can see.
2. **MCP only.** The agent reaches the graph only through forge's five MCP tools, with the documented contract. Forge's server is not built yet, so `forge_stub` serves the same tools. Switching to the real one is a single setting, `FORGE_COMMAND`.
3. **Investigation.** The agent has about 14 tool calls. It searches for earlier reports with several wordings, walks from a ticket to its PR, people and docs, reads full threads, and checks whether the cause really matches and whether the old fix is still in place.
4. **Citations it cannot fake.** Every claim lists the graph ids it rests on. Any id that no tool returned in that run is dropped before anyone sees it. A "fix that worked before" card that cites nothing is not shown.
5. **Baseline.** The same model, prompt and answer format, with the tools turned off. The comparison is fair: the only difference is the graph.
6. **The code fix.** The desk clones auth-service onto `ticket-desk/<ticket>`. The agent reads and edits only `src/` and `tests/`, using the graph for history. The desk re-runs the tests itself and commits only if they pass. Pushing and opening the draft PR happen only when someone approves.
7. **Approval on everything.** Runs only create proposals. A person approves, edits or discards each reply, note and PR. Replies go to a local outbox; nothing is emailed.

## Numbers to quote

From `python -m eval.check_ticket_desk` over the saved runs, scored against the answer key in `tickets.json`:

| | With graph | Baseline |
|---|---|---|
| Classified right (repeat / near-repeat / new) | 13/13 | 6/13 |
| Expected past sources cited | 28/32 | 0/32 |
| Right people named | 16/16 | 0/16 |

**Honest caveats:**
- The answer key was written by us, and the sandbox is small (13 tickets).
- Search is keyword-only. A vector `search_similar` tool is the biggest gap for messier, real-world phrasing.
- PRs are opened with the `gh` login on this machine, but their description says "approved by Ishan Sinha", because the desk acts as Ishan.
