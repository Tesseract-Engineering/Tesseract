# Architecture

Tesseract has two paths. The **build** turns a company's exports into a graph. The **answer** turns a question into cited evidence and, when the situation calls for it, into drafted actions a person approves. The two share one rule: nothing becomes a fact, and nothing leaves the system, without a record of where it came from and who allowed it.

## The build: exports → graph

```
adapter ──▶ Bundle ──▶ corpus stats ──▶ identity ──▶ declared fields ──▶ extraction ──▶ the door ──▶ store
(layout)    (records,    (boilerplate,   (one Person   (ABOUT, OWNS,       (MENTIONS,     (validate,   (batched,
             people,      common tokens)  per human)    ASSIGNED_TO, …)     CREDITS)       dedupe,      namespaced
             teams, ACL)                                                                   govern)      upserts)
```

| Step | Module | What it guarantees |
|---|---|---|
| Read | `adapters/`, `connectors.py` | An adapter reads one data layout and renames fields into a `Bundle` (`bundle.py`). It decides nothing. The ACL of each record (private channel, Confidential file, mailbox) is attached here, at ingest, because nothing downstream could reconstruct it. |
| Mask | `corpus.py` | Template lines repeated across a source ("Sent from my iPhone", ticket boilerplate) are masked, and tokens too common to refer to anything are refused, so extraction does not link every record to everything. |
| Identity | `resolve.py`, `build.py` | One `Person` per human across Slack, email, Jira and git, by a ladder of evidence: same email, then same normalised name, then handle equals name. Fuzzy matches are **never merged**; they become `ReviewItem`s for a person to decide. A handle seen under several different names is treated as identifying nobody. |
| Parse | `build.py` | Facts a source states outright (a ticket's assignee, its declared service, the customer it was raised for) become edges with confidence 1.0 and the field as provenance. |
| Infer | `matching.py` | Names found in prose become `MENTIONS` with the quote and surface form. A document header ("**Owner:** Jane Doe") becomes `CREDITS {role}`. Speakers in a chat thread are not "mentioned" in their own lines. |
| Admit | `contract.py`, `ingest.py` | Every node and edge is a proposal. The door validates it against the ontology, requires provenance on every edge, skips content it has already seen (content hash), and supersedes changed facts rather than overwriting them. The receipt counts what landed and why anything did not. |
| Write | `store.py`, `load.py` | Batched, idempotent Cypher. Uniqueness is `(ns, id)`, so companies (namespaces) never collide, and a rebuild deletes only its own namespace. |

Run `python -m tesseract.build --dry-run` on new data first. It prints coverage (which folders were not read), identity conflicts, adapter problems and proposal counts, and writes nothing.

## The answer: question → verdict

```
question
  │
  ├─ build request? ("help me design X")  ──▶ ask instead: "has anyone here already built X?"
  │
  ▼
ground ─────────▶ search ─────────▶ gate ─────────▶ model layer ─────────▶ hands
(grounding.py)    (viewer's ACL)    (INFO or         (expand, select,       (owners →
 people, teams,    fulltext +        NOT_FOUND)       verify; optional)      drafts)
 services, docs,   coverage
 tickets, customers
```

1. **Ground** (`grounding.py`, `answer.ground_question`). Names in the question are resolved against this company's nodes: full names, @handles, unambiguous first or last names, services in any spelling ("billing", "BillingService"), document titles, ticket keys and customers. An ambiguous name is reported, not guessed.
2. **Search** (`answer._keyword`). Full-text search over records **the viewer may read**. Each hit is scored on rank, on coverage of what the question actually asks (idf-weighted, after the named entities are removed), and on whether it links to a resolved entity.
3. **Gate.** `INFO` needs a resolved entity *and* a linked hit that clears the threshold. Otherwise the answer is `NOT_FOUND`, with the sources searched and the nearest misses labelled "nearest, not an answer". Restricted records that match are counted, along with their ACL groups and who to ask, using only ids and term counts computed inside the database. Their text never leaves Neo4j.
4. **Model layer** (`search.py`, only when a key is configured). The model expands the question into search terms. It then selects evidence from readable candidates, quoting each verbatim; a quote that is not a substring of the record is replaced. A separate verify call sees only the question and the quotes and must confirm each one states the answer. The model may **add** evidence the keyword gate missed. It never removes the gate's claims, never sees an unreadable record, and never touches the graph. Any model failure falls back to the keyword result.
5. **Hands** (`hands.py`). See below.

Usage feeds back in: every question is logged (`querylog.py`) and written to the graph as `(:Question)-[:LED_TO]->(record)`. Ask counts may **reorder** answers that already passed the gate, but never admit one.

## The hands: answer → drafted action → approval

`hands.attach` reads the situation from the question and the evidence:

| Situation | Signal | Drafts |
|---|---|---|
| **access** | a restricted record matches what the viewer asked | email + Slack to that record's owners asking for access. The draft names the ACL group, never the record's title or text. |
| **access process** | "how do I get access to…" answered from readable docs | email + Slack to the people who approve it |
| **incident** | outage, stale, OOM, root cause… | Slack heads-up to the owners of what the evidence points at |
| **precedent** | "has anyone built…", or a build request | a coding-agent brief (when the evidence lives in a repo), plus email + Slack to the people who built it |

**Who** comes from the graph: document header owners (weight 3), header authors and done-ticket assignees (2), and SharePoint authors and people who answered in a thread (1). The asker is never their own contact. People who have left are skipped and named in the draft. For precedent, people *planning* the same thing (draft documents, open tickets) are not offered as the ones who built it. **How to reach them** comes from their accounts in the graph; the company's email domain is only a labelled fallback.

**The approval door** (`actions.py`): `propose` stores each action with the SHA-256 of its exact payload. `approve` pins it. A caller showing a different payload gets the action rejected, not run. `execute` re-checks the hash and writes the draft to the **outbox** (`TESSERACT_OUTBOX`). It never sends a message and never edits a source system. `reset` removes everything `execute` wrote.

**Design diagrams** (`design.py`, `POST /design`): for a build request that found precedent, the model draws the existing system (its inputs, pipeline stages with gates, outputs and feedback loop) from the cited records the viewer can read. Every component must name the record it came from. The one invented element is the asker's own use case, marked as the plug-in point.

## Trust model, in one place

| Risk | Control |
|---|---|
| Leaking restricted content | ACL attached at ingest; every read filters on the viewer's groups; restricted matches report counts and groups only; drafts about sealed records name the group, not the record. |
| Answering from nowhere | The gate requires grounding plus coverage; model quotes must be verbatim and are re-verified; `NOT_FOUND` is a first-class answer. |
| Silent identity corruption | Fuzzy merges become review items; contradictory handles are quarantined. |
| A model writing to the graph | Models get text in and text out: no database handle, no tools, no write path. Every write goes through the door. |
| Acting without consent | Every action is a proposal; approval pins an exact-payload hash; execution writes drafts only. |
| Runaway model spend | `LLM_MAX_PER_MINUTE` and `LLM_MAX_PER_DAY`; over the limit, answers degrade to keyword search. |

## Module map

| Module | Role |
|---|---|
| `config.py` | Environment settings and the company file |
| `graph.py` | Read connection; binds `$ns` on every query |
| `access.py` | Viewers, the readable rule, owner-of-group |
| `grounding.py` | Names in a question → nodes |
| `answer.py` | Search, scoring, the gate, the model layer, build requests |
| `search.py` | Model prompts and parsing: expand, select, verify, headline |
| `llm.py` | The only module that knows a model vendor; rate limits |
| `hands.py` | Owner resolution and drafting |
| `design.py` | System design diagrams |
| `actions.py` | The approval door and the outbox |
| `querylog.py` | Question log and usage graph |
| `api.py` | HTTP API |
| `build.py`, `bootstrap.py` | Graph build; container start-up build |
| `bundle.py`, `adapters/`, `connectors.py` | Reading data |
| `corpus.py`, `resolve.py`, `matching.py` | Masking, identity, extraction |
| `contract.py`, `ingest.py`, `load.py`, `store.py` | The ontology, the door, the writer |
