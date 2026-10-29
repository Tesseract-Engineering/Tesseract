# docs_addin

A Google Docs sidebar that checks a document against the company graph when the writer asks. Select a passage of a design doc (or click into a paragraph) and press Check: Tesseract says what it contradicts, what went wrong the last time someone did it, what was already decided, and who to talk to, every claim cited. The writer can also ask a question about the selection, or have the document's own questions answered and inserted. It reads only what the writer can open, counts what they cannot without showing it, and does nothing until asked.

```
Google Doc ── Apps Script sidebar (apps_script/) ──HTTPS──▶ api.py ──▶ retrieve.py ──▶ Neo4j (graph/)
                 selection, Check, questions, Insert                │          as the viewer: swarm_queries
                                                                    └──▶ answer.py ──▶ Claude (swarm/model.py)
```

| File | What it does |
|---|---|
| `retrieve.py` | Finds what bears on a question or passage, as one viewer: keyword search over items and over decisions and systems, then one hop along refs. Every read goes through `graph/swarm_queries.py`'s `READABLE`, `EDGE_READABLE` and `LIVE`. Items a readable source links to but the viewer cannot open come back as a count per source and the people who could share them, never by title. |
| `answer.py` | Two model calls over the same sources, keyed `S1..Sn`. `context`: up to three notes on a passage (`conflict`, `incident`, `decided`, `owner`), none when nothing bears on it. `answer`: a cited answer to a question, optionally about a selected passage, with a status (`answered`, `partial`, `none`), the gaps and who to ask. Code keeps only markers that name a real source, renumbers them `[1]..[n]`, and drops a note that cites nothing. |
| `api.py` | `POST /context` and `POST /answer` behind a shared key, `GET /health`, and `GET /dev` (the demo doc and the sidebar in a browser tab, against this API, for development). |
| `apps_script/` | The add-in: `Code.gs` reports what the writer is pointing at (the selection, or the cursor's paragraph, with its section), calls the API as the signed-in user, and inserts an answer under its question; `Sidebar.html` is the pane: the focus bar with Check, a feed of checks and questions, the document's questions. |
| `demo_doc.json` | The demo design doc's text, used by `/dev`, the eval, and to write the Google Doc. |
| `../eval/check_docs_addin.py` | The answer key: 15 checks on the design doc, 25 on the SOC2 questions for two viewers. |

## What the demo shows

The doc is "Design: billing-service verifies OIDC tokens directly" (`demo_doc.json`): a full design doc (summary, background, goals and non-goals, a six-part proposed design, alternatives, security, observability, rollout, testing, open questions), written as Ishan. Most of it is sound. Eight places walk into something the company already knows; select one and Check:

| Passage | What Tesseract says | From |
|---|---|---|
| 2. "billing-service is the only consumer" of the billing audience | Hooli's webhooks broke on the same claims change (CS-2); removing the shim breaks them too | CS-2, #customer-success |
| 5.2 "fetch the JWKS once at startup", "call auth-service directly rather than going through api-gateway" | **Happened before**: web-app did that and staging logins failed for 47 minutes; **Conflicts**: the JWKS is routed through the gateway | OPS-8, WEB-3, the gateway routing commit, OPS-2 |
| 5.3 "read the top-level tenant_id and roles" | **Conflicts**: SBX-4 namespaced them | SBX-4, PR #4 |
| 5.4 "300 webhooks per minute, all under one API key" | **Conflicts**: rate limiter v2 allows 100 a minute by default; Globex hit 429s | API Rate Limiting v2, CS-3 |
| 5.5 "a token from /oauth/token" | **Conflicts**: SBX-5 is deprecating it before the Dec 15 cutover | SBX-5, PR #5 |
| 5.6 "apply the change from their laptop" | **Conflicts**: Terraform is applied from CI only | Terraform Conventions |
| 9. "production the week of December 14, delete the shim the same day" | **Conflicts**: the auth cutover week; the shim still serves payments and Hooli | sync notes, SBX-10, CS-2 |
| 4. Non-goal "migrating other services off the legacy token format" | Legacy tokens go away with SBX-5 anyway | SBX-5, SBX-3 |

The header, the rejected payload alternative and the review note come back "nothing bears on this". The two open questions have Answer buttons (SBX-20's audit log covers issuance only; the payments team owns billing). Selecting a passage and typing in the ask box asks about that passage ("is this safe during a key rotation?").

The first demo, the SOC2 auditor questions ("SOC2 Responses - Authentication (FY2026)"), still works and is still in the eval. As Hamilton, who cannot read the auth-service repo, its answers come from Jira, Slack and Drive and each says how many linked items are locked and who can share them.

## Run it

Needs the sandbox graph in Neo4j (`graph/README.md`): `python -m graph.build_swarm --root s3 --ns kg:sandbox` with `S3_BUCKET=sandbox2` and the storage credentials in the environment. Note that `--env .env` would override `S3_BUCKET` with the file's value.

```bash
pip install -e '.[dev,addin]'

# the API; ADDIN_MODEL_REPLAY records each answer, so a repeat question is instant and free
ADDIN_KEY=$(openssl rand -hex 24) NEO4J_PASSWORD=neo4j-local-dev ANTHROPIC_API_KEY=... \
  ADDIN_MODEL_REPLAY=data/replay-addin python -m docs_addin.api

# the sidebar in a browser, no Google involved (add ADDIN_DEV=1 to the line above)
open 'http://127.0.0.1:8787/dev?viewer=hamilton.tesseract@gmail.com'

# the answer key
NEO4J_PASSWORD=neo4j-local-dev ADDIN_MODEL_REPLAY=data/replay-addin python -m eval.check_docs_addin
```

### In Google Docs

Apps Script runs on Google's servers, so the API needs a public URL. A quick tunnel does it: `cloudflared tunnel --url http://127.0.0.1:8787` prints one.

Use a Chrome profile signed in to the demo account alone. With several Google accounts in one profile, Apps Script opens the bound script editor as "unable to open the file" and the sidebar's calls fail with `PERMISSION_DENIED`: they run as the profile's default account, not the one viewing the doc.

1. Create the doc in the demo account's My Drive (outside the folders the swarm reads, so the sandbox answer key does not change) with the text of `demo_doc.json`.
2. At script.google.com, New project. Replace `Code.gs` with `apps_script/Code.gs`, add an HTML file named `Sidebar` with `apps_script/Sidebar.html`, and in Project Settings tick "Show appsscript.json" and replace it with `apps_script/appsscript.json`. (Extensions > Apps Script on the doc also works, as a bound script.)
3. Project Settings > Script Properties: `TESSERACT_API_URL` (the tunnel URL) and `TESSERACT_API_KEY` (the `ADDIN_KEY`). Optionally `TESSERACT_VIEWER` to answer as another sandbox person.
4. Deploy > Test deployments > Editor Add-on > Create new test: Latest Code, Enabled in current document, the doc. Select it and Execute; in the doc, Extensions > the project > Open Tesseract, and authorize once (the app is unverified: Advanced, then go to it).

A quick tunnel's URL changes every time it starts; update `TESSERACT_API_URL` when it does.

The viewer is the email the script reports; the key is what makes that trustworthy, so it stays in Script Properties and on the server. For real users the script would send `ScriptApp.getIdentityToken()` and the API would verify it with Google.

## Configuration

| Variable | Default | |
|---|---|---|
| `ADDIN_KEY` | none, required | shared with the script's `TESSERACT_API_KEY` |
| `ADDIN_NS` | `kg:sandbox` | the graph namespace to read |
| `ADDIN_PORT` | `8787` | listens on 127.0.0.1 only |
| `ADDIN_DEV` | off | `1` serves `/dev` and `/dev/answer` (no key) |
| `ADDIN_MODEL`, `ADDIN_MODEL_EFFORT` | `claude-opus-5-5`, `medium` | |
| `ADDIN_MODEL_REPLAY` | off | a directory: answers are recorded there and replayed (`data/` is git-ignored) |
| `ADDIN_MODEL_BACKEND` | `anthropic` | `replay` answers only from recordings |
| `ADDIN_TODAY` | the real date | the date the prompts carry; the recordings in `data/replay-addin` were made with `2026-10-28`, so pin that to replay them on a later day |
| `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD` | as `graph/` | |

One answer or check reads about 6k tokens and writes under 1k, about 10 seconds on `claude-opus-5-5` at medium effort. Nothing is sent until the writer clicks Check, Answer or Ask; a check is kept per passage, so checking the same text again is free.
