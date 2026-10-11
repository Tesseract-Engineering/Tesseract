# API

`python -m tesseract.api` serves JSON on `API_HOST:API_PORT` (default `127.0.0.1:8091`; `0.0.0.0` in Docker). Allowed browser origins come from `API_CORS_ORIGINS` (default `*`). Errors are `{"error": code, "message": str}`:

| Code | Status |
|---|---|
| `bad_request`, `bad_action` | 400 |
| `forbidden` | 403 |
| `not_found` | 404 |
| `hash_mismatch`, `not_approved`, `rejected` | 409 |
| `internal` | 500 |

A **viewer** is either a preset name from the company file, or `{"name": "...", "acl_groups": ["..."]}`. When omitted, the company's `default_viewer` is used.

## Endpoints

| Endpoint | Body or query | Returns |
|---|---|---|
| `GET /health` | | `{ok, neo4j, time}` |
| `GET /company` | | `{name, viewers, default_viewer, users, suggestions}` |
| `GET /viewers` | | `{viewers: [{name, label, acl_groups}], default}` |
| `POST /ask` | `{question, viewer?, asker?}` | an answer (below). `asker` is the person's name; they are never proposed as their own contact. |
| `POST /design` | `{question, node_ids, viewer?}` | `{diagram}` or `{diagram: null, why}` |
| `GET /doc` | `?node_id=&viewer=` | one record's full text, `{node_id, label, title, text, source, date, author, visibility, access, path, state}`; **403** with the owner if the viewer can't read it |
| `GET /systems` | `?viewer=` | `{systems: [{source, documents}]}`, readable record counts per source |
| `POST /propose` | `{actions}` | `{proposal_id, actions: [{action_id, type, state, hash, action}]}` |
| `POST /approve` | `{proposal_id, action_id, approver, payload?}` | the action's state. A `payload` that does not hash-match the proposal rejects the action. |
| `POST /reject` | `{proposal_id, action_id, approver, reason?}` | the action's state |
| `POST /execute` | `{proposal_id, action_id}` | a receipt `{state: "executed", result_path, approver, executed_at, hash}`; 409 unless approved |
| `GET /proposal/<id>` | | the proposal and each action's state |
| `GET /receipts` | | approved, executed and rejected actions |
| `POST /reset` | | removes every executed draft and clears proposals |
| `GET /usage/top` | `?limit=&viewer=` | the most-asked records; titles of unreadable records are blanked |
| `GET /usage/co-asked` | `?node_id=&limit=&viewer=` | records the same questions led to |

## An answer

```json
{
  "verdict": "INFO",
  "headline": "Before you design this from scratch: the payments team runs a refund retry pipeline with idempotency keys.",
  "claims": [
    {"source": "docs", "title": "Refund retry pipeline", "node_id": "doc:payments/refund-retries.md",
     "quote": "The refund retry pipeline retries failed refunds with idempotency keys…",
     "date": "2026-09-01", "visibility": "internal", "askCount": 3,
     "why": {"selected_by": "llm", "verbatim_quote": true}}
  ],
  "owners": [{"role": "owner", "name": "Ada Lovelace", "source": "doc:payments/refund-retries.md"}],
  "hands": {"intent": "precedent",
            "owners": [{"name": "Ada Lovelace", "email": "ada@example.com", "slack": "@ada", "basis": ["header owner"]}],
            "skipped_alumni": [{"name": "Old Timer", "departed": "2025-06-30"}]},
  "approval_required": [{"who": "Alan Turing (sender)", "why": "Nothing is sent. Approving saves the draft for you to review and send."}],
  "proposed_actions": [
    {"id": "a1", "type": "agent.launch", "to": "services/payments", "payload": {"title": "...", "repo": "...", "steps": ["..."], "will_not": ["merge", "deploy", "touch production"]}},
    {"id": "a2", "type": "email.draft", "to": "ada@example.com", "payload": {"to": ["ada@example.com"], "to_names": ["Ada Lovelace"], "subject": "...", "body": "...", "citations": ["..."]}},
    {"id": "a3", "type": "slack.draft", "to": "@ada", "payload": {"to": ["@ada"], "to_names": ["Ada Lovelace"], "text": "..."}}
  ],
  "design_request": {"asked_as": "Has anyone here already built a refund retry pipeline?", "tried": ["..."]},
  "searched": {"sources": [{"source": "docs", "documents": 120}], "terms": ["..."], "resolved_entities": ["..."]},
  "restricted_matches": 0, "restricted_note": "", "restricted_groups": [],
  "entities": ["..."], "path": "search+llm", "llm": {"provider": "anthropic", "used": true},
  "viewer": "employee", "question": "...", "cypher": ["..."], "ms": 2140
}
```

| Field | Meaning |
|---|---|
| `verdict` | `INFO` (cited claims) or `NOT_FOUND` (with `reason` and `nearest`, each labelled "nearest, not an answer") |
| `claims[].why` | how it was admitted: `{rank, cover, linked, duplicate, usage_boost}` from the keyword gate, or `{selected_by: "llm", verbatim_quote}` |
| `restricted_*` | how many unreadable records matched, their ACL groups, and whom to ask; never their titles or text |
| `hands` | the situation (`access`, `access-process`, `incident`, `precedent`), the owners found and the alumni skipped |
| `proposed_actions` | pass them unchanged to `POST /propose` as `actions` |
| `path` | `search` (keyword only), `search+llm`, or `meta` (questions about what the viewer can access) |
| `cypher` | every query that ran, for audit |

## Scoring, briefly

Each readable hit scores `0.25·rank + 0.75·cover + 0.15 (linked) − 0.25 (duplicate title)`:
- `rank` is the full-text score relative to the top hit.
- `cover` is the idf-weighted share of the question's *ask terms* the record contains. Ask terms are what's left after named entities and framing words are removed.
- `linked` means the record mentions, is about, is assigned to, or is for a resolved entity.

`INFO` needs a resolved entity and a linked hit with `cover ≥ 0.8` and score `≥ 0.8`. Usage (how often a record has been asked about) adds at most 0.15, and only reorders hits that already passed.
