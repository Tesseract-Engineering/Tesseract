# docs_addin

A Google Docs sidebar that answers the questions in a document from the company graph, as the person reading it. Built for the sandbox demo: the SOC2 auditor questions in the planning folder, answered from Jira, GitHub, Slack and Drive with every claim cited, the gaps named, and what the reader cannot open counted but never shown.

```
Google Doc ── Apps Script sidebar (apps_script/) ──HTTPS──▶ api.py ──▶ retrieve.py ──▶ Neo4j (graph/)
                 questions in the doc, Insert                 │          as the viewer: swarm_queries
                                                              └──▶ answer.py ──▶ Claude (swarm/model.py)
```

| File | What it does |
|---|---|
| `retrieve.py` | Finds what bears on a question, as one viewer: keyword search over items and over decisions and systems, then one hop along refs. Every read goes through `graph/swarm_queries.py`'s `READABLE`, `EDGE_READABLE` and `LIVE`. Items a readable source links to but the viewer cannot open come back as a count per source and the people who could share them, never by title. |
| `answer.py` | One model call: the question and the readable sources under keys `S1..Sn`, answered as JSON with `[S#]` after each claim, a status (`answered`, `partial`, `none`), the gaps, and who to ask. Code keeps only markers that name a real source and renumbers them `[1]..[n]`; an answer that cites nothing is shown as "nothing found". |
| `api.py` | `POST /answer` behind a shared key, `GET /health`, and `GET /dev` (the sidebar in a browser tab, against this API, for development). |
| `apps_script/` | The add-in: `Code.gs` finds the questions (paragraphs ending in `?`), calls the API as the signed-in user, and inserts an answer under its question; `Sidebar.html` is the pane. |
| `../eval/check_docs_addin.py` | The answer key: the five SOC2 questions for two viewers, 25 checks. |

## What the demo shows

The doc: "SOC2 Responses - Authentication (FY2026)" with the five questions from `SOC2 Auditor Questions - Authentication.pdf` pasted in. Open the sidebar, click Answer on each.

| # | Question | What the graph has | The moment |
|---|---|---|---|
| 1 | OIDC for federated login? | In progress: discovery endpoint shipped (SBX-2, PR #3), ID tokens underway (SBX-3), cutover blocked on the key rotation and web-app | It does not claim the migration is done |
| 2 | Key rotation, how often, evidence? | The runbook (24h overlap in the JWKS), OPS-1, the staging dry run and its postmortem (OPS-8), SBX-22 | **Partial**: no schedule anywhere, no completed production rotation on record; ask Dhruv |
| 3 | Custom claims, validated how? | `tenant_id` and `roles`, namespaced (option B, SBX-4, the #auth-migration thread); the billing webhook shim (SBX-10) | Pulls in a downstream consumer nobody asked about |
| 4 | Deprecated endpoints, removal date? | `/oauth/token` (SBX-5), its blockers (WEB-1, OPS-1), the Sunset header work | **Partial**: no calendar date; the removed CSV export is left out (not auth) |
| 5 | Access to signing material? | KMS via Terraform, and little else | **Partial**, says so, names Dhruv |

Who is asking matters. **Ishan** reads all three repos, so his answers cite the PRs. **Hamilton** is not a reader of the auth-service repo: his answers come from Jira, Slack and Drive, and each card says "3 linked items you can't open (3 GitHub). Ishan Sinha can share them. Draft a note", which opens a Gmail draft and sends nothing.

Insert puts the answer under its question, shaded, with a small "Sources:" line linking each cited item. Docs has no API for suggestions (tracked changes) or for comments anchored to text, so the shading marks what Tesseract wrote and the links carry the provenance; Ctrl/⌘+Z undoes it.

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

1. Signed in as the sandbox account that will demo, create the response doc in the sandbox Drive and paste in the five questions, one paragraph each.
2. Extensions > Apps Script. Replace `Code.gs` with `apps_script/Code.gs`, add an HTML file named `Sidebar` with `apps_script/Sidebar.html`, and in Project Settings tick "Show appsscript.json" and replace it with `apps_script/appsscript.json`.
3. Project Settings > Script Properties: `TESSERACT_API_URL` (the tunnel URL) and `TESSERACT_API_KEY` (the `ADDIN_KEY`). Optionally `TESSERACT_VIEWER` to answer as another sandbox person.
4. Reload the doc. Extensions > Tesseract > Open Tesseract, and authorize once.

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
| `NEO4J_URI`, `NEO4J_USER`, `NEO4J_PASSWORD` | as `graph/` | |

One answer reads about 6k tokens and writes under 1k, about 10 seconds on `claude-opus-5-5` at medium effort.
