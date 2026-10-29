# core

The Tesseract core: the one service that reads the graph, and holds **threads** (working context that follows a person from a call to a doc to Slack to a ticket) and **approvals** (an action's exact payload, approved by its hash before any bot performs it). Every bot talks to the core; only the core reads Neo4j, so the access rule (`graph/swarm_queries.py`) and the citation checks live in one place.

```
Zoom meeting page ─┐ (in process)                       ┌─▶ answers   core/answer.py (quoted claims, Slack and the call)
Slack bot ─────────┤                                     │             docs_addin/ (cited notes and answers, Docs)
Docs sidebar ──────┼─ HTTPS (tunnel) ─▶ core/api.py ─────┼─▶ threads   core/threads.py (SQLite, data/core/)
ticketing bot ─────┘ HTTP                                └─▶ approvals core/threads.py, logged on the thread
```

| File | What it does |
|---|---|
| `service.py` | `Core`: answers as a person or for an audience, the Docs engine with a thread as context, threads, approvals, call suggestions, the review-request draft, the ticket brief. Announces every change to subscribers (the meeting page, the Slack bot). |
| `threads.py` | The thread store and its rules: audience-only reads, owner-only accept/approve/close, per-person active thread, approvals by hash, 14-day idle close, 30-day delete. |
| `api.py` | The HTTP contract below, behind one key per bot. Also serves the Docs sidebar's `/dev` page. |
| `answer.py`, `graph.py`, `ledger.py` | Moved from `slackbot/`: the quoted-answer engine, graph reads as a viewer, the access-request ledger. |

## Run it

```bash
python -m core                      # the API on 127.0.0.1:8787
python -m core --slack              # and the Slack app (python -m slackbot is the same)
python -m core --meeting            # and the Tesseract Meeting page on 8766
cloudflared tunnel --url http://127.0.0.1:8787   # for the Docs sidebar (Apps Script runs on Google's servers)
```

Keys: on first run the core writes `data/core-keys.json` (git-ignored), one key per surface (`zoom`, `docs`, `slack`, `tickets`). `ADDIN_KEY` (or `TESSERACT_API_KEY`, the Script Property's name), when set, is the docs key, so the sidebar's existing `TESSERACT_API_KEY` keeps working. A bot sends its key as `X-Tesseract-Key` (the sidebar's `X-Addin-Key` works too); the key decides which surface is calling, so a bot can only add entries and finish approvals as itself.

**The call's host owns the call's thread**, and only a thread's owner accepts proposals and approves what leaves it (a doc insert, a Slack post, a ticket). Set `SLACKBOT_MEETING_HOST` to whoever drives the demo.

## The thread

```json
{"id": "thr_1dcef3", "title": "Per-model shadow gate thresholds", "owner": "ishan.tesseract@gmail.com",
 "audience": ["ishan…", "karthik…", "dhruv…", "hamilton…"], "status": "open",
 "entries": [
   {"surface": "zoom", "kind": "answer", "question": "what do we already know…", "records": ["slack:…"], "titles": ["…"]},
   {"surface": "zoom", "kind": "decision", "status": "proposed", "text": "Make the threshold per-model config", "said_by": "Karthik Valluri"},
   {"surface": "docs", "kind": "link", "url": "https://docs.google.com/…", "title": "Design: per-model thresholds"},
   {"surface": "docs", "kind": "action", "approval": "68f164", "text": "Inserted Background into …", "url": "…"},
   {"surface": "tickets", "kind": "action", "approval": "a19a5a", "text": "ML-21", "url": "https://…/browse/ML-21"}]}
```

Entry kinds: `transcript`, `decision`, `question`, `answer`, `note`, `link`, `action`. A `decision` or `question` a bot proposes counts only once the owner accepts it; until then it is not context anywhere. A thread is context for answers, never a source: nothing quotes or cites it.

## API

| Method | Path | Does | Body / returns |
|---|---|---|---|
| POST | `/answer` | Quoted answer as one person, or for an audience | `{as, question, context?, thread?, audience?}` → `{status, headline, claims[{text, quote, record, title, url}], locked, hidden}` |
| POST | `/docs/answer`, `/docs/context` | The Docs engine (`docs_addin/`) | Karthik's bodies, plus `thread?` |
| POST | `/docs/draft` | A cited Background section from a thread, as an approval | `{viewer, thread, url, doc}` → answer + `questions` + `approval` |
| POST | `/threads` | Open a thread | `{owner, title, audience?}` → thread |
| GET | `/threads?member=` / `?owner=` | A person's open threads, newest first | → `[thread]` |
| GET | `/threads/{id}?as=` | One thread, if `as` is in its audience | → thread |
| POST | `/threads/{id}/entries` | Append an entry | `{by, kind, ...}` → entry |
| POST | `/threads/{id}/entries/{entry}/accept` · `/dismiss` | The owner decides a proposal | `{by}` |
| POST | `/threads/{id}/active` | Work in this thread (Slack and Docs read it as context) | `{by}` → thread |
| POST | `/threads/{id}/handoff` | Continue in `docs`, `slack` or `tickets` | `{by, to}` → `{slack: {team, bot_user}?}` |
| POST | `/threads/{id}/close` | The owner closes it | `{by}` |
| GET | `/threads/{id}/brief?as=` | The thread condensed for a ticket | → `{title, owner, decisions, questions, docs, records, markdown}` |
| POST | `/threads/{id}/ticket` | Propose a ticket with the brief | `{by}` → approval (`surface: tickets`, `action: create_ticket`) |
| POST | `/approvals` | Propose an action with its exact payload | `{by, action, payload, thread?}` → `{id, hash, ...}` |
| GET | `/approvals?surface=&status=` | E.g. the ticketing bot's approved work | → `[approval]` |
| POST | `/approvals/{id}/approve` | The thread's owner approves that hash | `{by, hash}` → approval |
| POST | `/approvals/{id}/done` | The surface performed it (logged on the thread with the hash) | `{result: {url?, text?}, ok?}` |
| POST | `/approvals/{id}/cancel` | Withdraw it | `{by}` |

## For the ticketing bot

Nothing to import: poll (or call on a schedule) with the `tickets` key from `data/core-keys.json`.

1. `GET /approvals?surface=tickets&status=approved`: tickets an owner approved, in Slack (**Make a ticket**, or `/tesseract make a ticket`).
2. For each: create the Jira issue from `payload` exactly: `summary`, `description` (the brief, Markdown: decisions, open questions, the doc, the records it rests on, each a link), `owner` (an email), `records` (URLs).
3. `POST /approvals/{id}/done` with `{"result": {"url": "https://…/browse/ML-21", "text": "ML-21"}}`, or `{"ok": false, "result": {"text": "why"}}`. The core logs it on the thread, the call's screen shows it, and the Slack bot DMs the owner the link.

`done` works once, only for an approved approval, and only with the `tickets` key. Anything else the ticketing bot wants to add goes in as an entry: `POST /threads/{id}/entries {by, kind: "note", text}`.

## Settings

`TESSERACT_PORT` (8787; `ADDIN_PORT` also read), `TESSERACT_NS` (`SLACKBOT_NS`, else `kg:swarm`), `TESSERACT_THREADS_DB` (`data/core/threads.db`), `TESSERACT_KEYS` (`data/core-keys.json`), `ADDIN_KEY` (the docs key), `ADDIN_DEV=1` (the sidebar at `/dev`). The Docs engine keeps its own model settings (`ADDIN_MODEL`, `ADDIN_MODEL_REPLAY`, `ADDIN_TODAY`, see `docs_addin/README.md`); the quoted answers use the swarm's (`SWARM_MODEL`, `SLACKBOT_MODEL_EFFORT`).

Tests: `pytest tests/core_tests` (no database, no model).

## Not done yet

- **One answer engine.** Slack and the call use quoted claims; Docs uses `[S#]` citations with a one-hop retriever. Both read through `swarm_queries`, in this process, but they are two engines; merging them is the next step.
- **Jira identities** are still separate people in the graph (design doc, "Prerequisites"), so Jira tickets read as locked even to their authors.
- **Identity** is a shared key per bot; a real deployment verifies a Google ID token from the sidebar and gives each bot its own service identity.
