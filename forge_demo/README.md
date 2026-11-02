# forge_demo

Act 2 of the end-to-end demo: **"Where should Tesseract live?"** A Claude Code chat where the presenter says where the team works, and Tesseract's setup agent connects and checks each surface.

**Demo only.** Forge does not set up surfaces (see `docs/FORGE.md`: it serves the graph and live sources over MCP). The four surfaces are started before the take. The chat tells their story and checks them for real, so nothing on screen claims more than the checks show:

| Tool | On screen | Behind it |
|---|---|---|
| `describe_sources` | What the swarm connected: repos, PRs, Jira projects and issues, Slack channels and threads, Drive folders and documents, then people, teams, decisions, customers, connections | **Real**: counted from the graph |
| `plan_surfaces` | Per surface: what Tesseract does there, whose access it reads as, what needs approval | Written (`SURFACES` in `server.py`), true of each surface as built |
| `deploy_surface` | A setup log, then ✓ and where it lives | The log is written; the ✓ is a **real** check that the surface is up (the core, the meeting page, the desk; Slack's bot token when `SLACKBOT_BOT_TOKEN` is set). Down means ✗ with why |
| `smoke_test` | One question per surface and its cited answer | **Real**: Slack and Zoom ask the core; Docs runs a Check on §4.3 of the demo doc; the desk shows TD-104's saved verdict |

The smoke tests deliberately avoid Act 3's own questions (the token claims question, §2.2, TD-113), so nothing is spoiled.

## Run it

Start everything Act 3 needs first (`ticket_desk/DEMO.md`, `core/README.md`): Neo4j, `TESSERACT_NS=kg:sandbox python -m core --meeting`, the desk with `TICKET_DESK_NOW=2026-10-29T20:00:00Z python -m ticket_desk`. Then, in a clean terminal with a large font:

```bash
python -m forge_demo
```

and type:

> We work in Google Docs, Slack, Zoom calls, and a customer ticket desk. Put Tesseract in all four.

The chat is Claude Code with only these four tools (no built-in tools, no other MCP servers), the setup agent's prompt (`prompt.md`), and no permission prompts, so takes come out alike. It uses your Claude Code login; `.env` goes only to the tools. A run takes about a minute (the Docs and Slack checks are live model calls); speed it up in the edit.

Settings: `FORGE_GRAPH_NS` (`kg:sandbox`), `TESSERACT_URL` (`http://127.0.0.1:8787`), `MEETING_URL` (`http://127.0.0.1:8766`), `TICKET_DESK_URL` (`http://127.0.0.1:8790`), `FORGE_DEMO_AS` (whose access the smoke tests read as, `ishan.tesseract@gmail.com`), `FORGE_DEMO_CALL` (the people on the call, default that person and Karthik). Extra arguments go to `claude`, e.g. `python -m forge_demo --model opus`.
