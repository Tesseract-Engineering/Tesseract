# docs_addin

A Google Docs sidebar that reads the company graph as the person writing. As they move through a design doc it flags, for the paragraph they are on, what that paragraph contradicts, what went wrong the last time someone did it, what was already decided, and who to talk to, every claim cited. It also answers the questions in a document (an open question in a design, an auditor's list), and counts what the reader cannot open without showing it.

```
Google Doc ── Apps Script sidebar (apps_script/) ──HTTPS──▶ api.py ──▶ retrieve.py ──▶ Neo4j (graph/)
                 the cursor's paragraph, questions, Insert         │          as the viewer: swarm_queries
                                                                    └──▶ answer.py ──▶ Claude (swarm/model.py)
```

| File | What it does |
|---|---|
| `retrieve.py` | Finds what bears on a question or paragraph, as one viewer: keyword search over items and over decisions and systems, then one hop along refs. Every read goes through `graph/swarm_queries.py`'s `READABLE`, `EDGE_READABLE` and `LIVE`. Items a readable source links to but the viewer cannot open come back as a count per source and the people who could share them, never by title. |
| `answer.py` | Two model calls over the same sources, keyed `S1..Sn`. `context`: up to three notes on a paragraph (`conflict`, `incident`, `decided`, `owner`), none when nothing bears on it. `answer`: a cited answer to a question with a status (`answered`, `partial`, `none`), the gaps and who to ask. Code keeps only markers that name a real source, renumbers them `[1]..[n]`, and drops a note that cites nothing. |
| `api.py` | `POST /context` and `POST /answer` behind a shared key, `GET /health`, and `GET /dev` (the demo doc and the sidebar in a browser tab, against this API, for development). |
| `apps_script/` | The add-in: `Code.gs` follows the cursor (paragraph, section, whether it is a question), calls the API as the signed-in user, and inserts an answer under its question; `Sidebar.html` is the pane. |
| `demo_doc.json` | The demo design doc's text, used by `/dev`, the eval, and to write the Google Doc. |
| `../eval/check_docs_addin.py` | The answer key: 11 checks on the design doc, 25 on the SOC2 questions for two viewers. |

## What the demo shows

The doc is "billing-service: verify OIDC tokens directly (retire the SBX-10 shim)" (`demo_doc.json`), written as Ishan. Click into each paragraph and pause; the sidebar's "This paragraph" card fills in.

| Paragraph | What the sidebar says | From |
|---|---|---|
| Context: billing reads `tenant_id`, a shim rewrites the claim | The payments team owns the webhooks and the shim waits on them; Hooli was hit by the same 401s | SBX-10, CS-2, #eng and #customer-success threads |
| Goals: "read the tenant and roles from the standard OIDC claim set" | **Conflicts**: there is no standard claim for either; SBX-4 namespaced them | SBX-4, SBX-7, PR #4 |
| Token validation: "fetch the JWKS once at startup" | **Happened before**: web-app did that and 15% of staging logins failed for 47 minutes; **Conflicts**: keys overlap only 24h in a rotation | the OPS-8 postmortem, WEB-3, the rotation runbook |
| Service-to-service: "get a token from /oauth/token" | **Conflicts**: SBX-5 is deprecating it before the Dec 15 cutover | SBX-5, PR #5, WEB-1 |
| Rollout: "production the week of December 14" | **Conflicts**: that is the auth cutover week; the last rotation broke staging | sync notes, OPS-8, OPS-1 |
| Review: "comment by Friday" | Nothing to flag | |

The two open questions ("an audit record of which tokens billing-service accepted?", "who owns billing-service?") have Answer buttons: the first finds SBX-20's audit log covers issuance only, the second names the payments team. Insert puts an answer under its question, shaded, with a "Sources:" line of links (Docs has no API for suggestions or anchored comments).

The first demo, the SOC2 auditor questions ("SOC2 Responses - Authentication (FY2026)" with the five questions from the planning folder's PDF), still works and is still in the eval. As Hamilton, who cannot read the auth-service repo, its answers come from Jira, Slack and Drive and each says how many linked items are locked and who can share them.

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

One answer or set of notes reads about 6k tokens and writes under 1k, about 10 seconds on `claude-opus-5-5` at medium effort. The sidebar asks for notes once the cursor has rested on a paragraph for two polls (about 4 seconds) and keeps them per paragraph text.
