# slackbot

Tesseract in Slack. Ask in a DM, or @mention it in a channel, and it answers from the swarm graph **as you**: every record it reads is checked against your access (`graph/swarm_queries.py`), and every claim quotes one of those records word for word. Matching records you may not read are counted and their owner named, so you can ask for access in one approval.

```
DM / @mention ──▶ your Slack email ──▶ viewer() ──▶ search (knowledgeSearch, entitySearch)
                                                      ├─ readable ──▶ model picks quotes ──▶ code checks each quote ──▶ answer card
                                                      └─ locked   ──▶ count + owner ──▶ 🔒 line ──▶ Request access
Request access ──▶ review card (to, items, scope, message, hash) ──▶ Send request ──▶ owner's Grant / Decline
Grant ──▶ Drive shares as the owner, read only, 30 days ──▶ your question re-answered with those items
```

| File | What it does |
|---|---|
| `graph.py` | Every graph read: search as a viewer, owners and authors from role edges, items by id. |
| `answer.py` | Question to `INFO` (claims, each with a checked quote) or `NOT_FOUND` (what was searched, nearest misses). Locked items are counted; the model never sees their text. |
| `cards.py` | The Block Kit cards, after the playable mock: answer, review, sent, owner request, connect, thread post. |
| `ledger.py` | SQLite under `data/slackbot/`: requests with their payload hash, grants with expiry, owners' Google tokens, an event log. |
| `drive.py` | An owner's one-time Google sign-in (desktop OAuth, loopback, PKCE) and the Drive share run as them. |
| `app.py` | The Bolt app, Socket Mode: no public URL needed. |

## Ways to ask

| Where | How | Who sees the answer |
|---|---|---|
| Its DM | Message **Tesseract** under Apps | You |
| Any channel | `/tesseract has anyone built a retraining pipeline?` (no invite needed) | You; Send to thread shares it |
| On a message | ⋯ → Connect to apps → **Ask Tesseract**, optional question | You, in that channel or thread |
| In a channel it was invited to | `@Tesseract who owns the retrain pipeline?` | You; Send to thread shares it |
| About a conversation | `@Tesseract what do you think of this?` in a thread, or right under the messages | You |

"What do you think of this?" reads the conversation at the moment you ask: the whole thread when asked in one, otherwise the last 8 messages, and only when the question points at something ("this", "that", "above"...). Nothing is stored. The conversation only steers the search and the reading, and is never quoted as a source; a quote taken from it fails the check like any other. The answer says what the records say about the topic (prior decisions, related work, owners), never the bot's own opinion. Messages from bots are left out. A person's message posted through an app carries a `bot_id`, so the check is whether the author is a bot user.

## What each step does

- **DM a question.** The answer card quotes what you can read, cites each record (title, source, date, author) and lists the sources. If matching items are restricted, the 🔒 line says how many, what kind ("2 Drive docs") and who owns them. It never shows their titles or text.
- **Request access.** This shows the review card: to whom, how many items, read only for 30 days, and the message. Nothing is sent yet. **Send request** is the one approval and is tied to the hash shown on the card. **Edit** changes the message and makes a new hash, so it needs approving again. **Cancel** sends nothing.
- **The owner's side.** The owner gets the request in their DM, with item titles, the reason and the question asked. **Grant** shares the files in Drive *as the owner*. The first time, they connect Google once and the grant goes through as soon as they finish. Items outside Drive (Jira, Slack, GitHub) can't be shared by the bot: the owner shares them there and clicks **I've shared them**.
- **After a grant.** The question is answered again with the new items, in the asker's DM. The graph only sees the new Drive permission after the next swarm run and rebuild. Until then the bot's ledger vouches for the grant, and only until it expires.
- **@mention in a channel.** The card is ephemeral: only the asker sees it, lock line included. **Send to thread** posts the answer and its sources into the thread, labeled "Shared by @you", and never the lock line. It is offered only when every person in the channel can read every quoted record. Otherwise the card says why it is off.

## Set it up

1. **The graph.** Neo4j running with the swarm graph built (namespace `kg:swarm` unless `SLACKBOT_NS` says otherwise):
   ```bash
   docker compose -f swarm/compose.yaml up -d storage neo4j
   docker compose -f swarm/compose.yaml run --rm graph
   ```
2. **The Slack app.** api.slack.com/apps, then Create New App, From an app manifest, workspace *Tesseract Sandbox*, and paste `slackbot/app-manifest.yaml`.
   - Generate an app-level token with `connections:write`.
   - Install the app to the workspace.
   - Add both tokens to `.env`:
     ```
     SLACKBOT_BOT_TOKEN=xoxb-...
     SLACKBOT_APP_TOKEN=xapp-...
     ```
   - `/invite @Tesseract` in any channel it should answer @mentions in. `/tesseract` and Ask Tesseract work without it.
   - After changing scopes in the manifest, reinstall the app (Install App → Reinstall) so the token gets them.
3. **Drive grants (optional; without them, owners share by hand).**
   - In the swarm's Google Cloud project, open APIs & Services, then Credentials, then Create OAuth client ID, type **Desktop app**.
   - Download its JSON to `data/gdrive/oauth-client.json`.
   - On the OAuth consent screen, keep the app in Testing and add the four sandbox Google accounts as test users.
4. **Run it.**
   ```bash
   pip install -e '.[slackbot]'
   python -m slackbot                                  # reads .env
   python -m slackbot --ask hamilton.tesseract@gmail.com "has anyone built a retraining pipeline?"   # no Slack
   ```

Settings, all optional: `SLACKBOT_NS` (`kg:swarm`), `SLACKBOT_ACCESS_DAYS` (30), `SLACKBOT_MODEL_EFFORT` (`low`), `SLACKBOT_DB` (`data/slackbot/tesseract.db`), `SLACKBOT_GOOGLE_CLIENT` (`data/gdrive/oauth-client.json`), `SLACKBOT_OAUTH_PORT` (8765). The model is the swarm's own (`SWARM_MODEL`, `SWARM_MODEL_BACKEND`). With `SWARM_MODEL_BACKEND=off` the bot still answers, by quoting the sentence of the best record that shares the most words with the question.

Tests: `pytest tests/slackbot_tests` (no Slack, no database).
