"""
server.py - the "where should Tesseract live?" step of the end-to-end demo, as MCP tools for a
Claude Code chat. Demo only: forge does not set up surfaces. The four surfaces (Docs sidebar, Slack
app, meeting page, Ticket Desk) are started before the take; these tools tell their story and check
them, and nothing they print claims more than the checks show.

    describe_sources()          real: what the swarm connected, counted from the graph
    plan_surfaces(surfaces)     written: what Tesseract does on each surface, whose access, what needs approval
    deploy_surface(surface)     written log lines, then a real check that the surface is up (✓ or ✗ with why);
                                for Slack, with SLACKBOT_BOT_TOKEN set, also that Slack accepts the bot's token
    smoke_test(surface)         real: one question through that surface's route, with the first cited claim

Scripted mode (FORGE_DEMO_SCRIPTED=1) needs nothing running: every surface comes back ✓, and
describe_sources and smoke_test replay what a real run returned, saved in scripted.json by
`python -m forge_demo record`. Its ✓ marks are not checked; use it to record anywhere.

    FORGE_GRAPH_NS (kg:sandbox), TESSERACT_URL (http://127.0.0.1:8787), MEETING_URL (http://127.0.0.1:8766),
    TICKET_DESK_URL (http://127.0.0.1:8790), FORGE_DEMO_AS (ishan.tesseract@gmail.com),
    FORGE_DEMO_CALL (the people on the call; default the asker and karthik.tesseract@gmail.com)
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from mcp.types import CallToolResult, TextContent

ROOT = Path(__file__).resolve().parent.parent
NS = os.environ.get("FORGE_GRAPH_NS", "kg:sandbox")
CORE = os.environ.get("TESSERACT_URL", "http://127.0.0.1:8787").rstrip("/")
MEETING = os.environ.get("MEETING_URL", "http://127.0.0.1:8766").rstrip("/")
DESK = os.environ.get("TICKET_DESK_URL", "http://127.0.0.1:8790").rstrip("/")
ASKER = os.environ.get("FORGE_DEMO_AS", "ishan.tesseract@gmail.com")
CALL = [e.strip() for e in os.environ.get("FORGE_DEMO_CALL", f"{ASKER},karthik.tesseract@gmail.com").split(",") if e.strip()]

SAVED = Path(__file__).resolve().parent / "scripted.json"

Result = Annotated[CallToolResult, dict[str, Any]]

mcp = MCPServer("forge", instructions="Set up Tesseract on the surfaces a team works in, and check each one.")

# What Tesseract does on each surface. Each line is true of the surface as built (core/, slackbot/,
# docs_addin/, ticket_desk/); `steps` is the log deploy_surface prints before its real check.
SURFACES: dict[str, dict] = {
    "docs": {
        "name": "Google Docs sidebar",
        "does": ["Check a passage against what the company already knows: conflicts, past incidents, decisions, owners",
                 "Answer the doc's open questions, and draft cited sections from a thread"],
        "reads_as": "the writer",
        "approval": "Nothing goes into the doc until the thread's owner approves the exact text",
        "steps": ["Registering the Editor add-on “Tesseract for Docs”", "Binding the docs key to the core",
                  "Routing Check and Answer through the core's /docs routes", "Listing the writer's open threads in the sidebar"],
        "where": "Extensions › Tesseract for Docs",
    },
    "slack": {
        "name": "Slack app",
        "does": ["Cited answers in DMs, @mentions and /tesseract, quoting each record word for word",
                 "Draft a review request for the doc, or a ticket from the thread"],
        "reads_as": "the person asking; restricted items are counted, never shown",
        "approval": "Posts and tickets wait for the thread's owner to approve the exact text",
        "steps": ["Installing the Tesseract app (Socket Mode)", "Registering /tesseract and Ask Tesseract",
                  "Mapping Slack users to their company identity", "Subscribing to thread updates"],
        "where": "Slack › Apps › Tesseract",
    },
    "zoom": {
        "name": "Meeting page beside Zoom",
        "does": ["A live transcript, and answers on the shared screen with the records they cite",
                 "Start a thread from the call, and propose its decisions and open questions"],
        "reads_as": "everyone on the call: it quotes only what all of them can read",
        "approval": "Transcription starts only once the host confirms everyone agreed; the host accepts each decision",
        "steps": ["Starting the meeting page", "Connecting live transcription", "Setting the host and the people on the call",
                  "Linking call threads to Docs and Slack"],
        "where": MEETING,
    },
    "tickets": {
        "name": "Ticket Desk",
        "does": ["Works each ticket against the graph: earlier reports, the fix that worked, who owns it",
                 "Drafts the reply, the internal note and, for code, a tested draft PR; tickets arrive from Slack threads"],
        "reads_as": "the support engineer, through forge's MCP tools",
        "approval": "Every reply, note and PR waits for a person: Approve, Edit or Discard",
        "steps": ["Connecting the desk to forge over MCP", "Loading the support queue",
                  "Subscribing to tickets approved in Slack", "Enabling draft PRs on auth-service"],
        "where": DESK,
    },
}
ALIASES = {"google docs": "docs", "doc": "docs", "gdocs": "docs", "zoom meetings": "zoom", "meetings": "zoom",
           "meeting": "zoom", "calls": "zoom", "ticket desk": "tickets", "customer ticket desk": "tickets",
           "ticketing": "tickets", "support": "tickets", "ticket": "tickets"}

# One question per surface, none of them the live demo's own questions, each checked to answer from the
# sandbox graph.
SMOKE = {
    "slack": "Who owns the API gateway key rotation runbook?",
    "zoom": "What is the default rate limit on the public API?",
    "docs": ("4.3 Signing keys", "billing-service will fetch auth-service's JWKS once at startup and cache the keys in "
             "memory for the life of the process. To keep the dependency simple, it will call auth-service's internal "
             "/.well-known/jwks.json endpoint directly rather than going through api-gateway."),
    "tickets": "TD-104",
}


def _ok(data: dict, text: str) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], structured_content=data)


def _scripted() -> bool:
    return os.environ.get("FORGE_DEMO_SCRIPTED", "").strip() == "1"


def _replay(tool: str, surface: str | None = None) -> CallToolResult:
    """What a real run of this tool returned (scripted.json), after about as long as it took."""
    saved = json.loads(SAVED.read_text(encoding="utf-8"))[tool]
    out = saved[surface] if surface else saved
    time.sleep(min(out["data"].get("seconds", 1), 4))
    return _ok(out["data"], out["text"])


def _surface(name: str) -> str | None:
    key = name.strip().lower()
    key = ALIASES.get(key, key)
    return key if key in SURFACES else None


def _keys() -> dict[str, str]:
    path = Path(os.environ.get("TESSERACT_KEYS", ROOT / "data" / "core-keys.json"))
    keys = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    addin = os.environ.get("ADDIN_KEY") or os.environ.get("TESSERACT_API_KEY")
    if not addin and (ROOT / "data" / "addin-key.txt").exists():
        addin = (ROOT / "data" / "addin-key.txt").read_text().strip()
    if addin:                                   # the core takes the add-in's key as the docs key
        keys["docs"] = addin
    return keys


def _http(url: str, body: dict | None = None, key: str | None = None, timeout: float = 90) -> Any:
    headers = {"Content-Type": "application/json"} | ({"X-Tesseract-Key": key} if key else {})
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")


def _up(url: str) -> str | None:
    """None if url answers, else why not."""
    try:
        _http(url, timeout=5)
        return None
    except urllib.error.HTTPError as e:
        return None if e.code < 500 else f"HTTP {e.code}"
    except (urllib.error.URLError, OSError) as e:
        return f"not reachable ({getattr(e, 'reason', e)})"


def _slack() -> str | None:
    """None if Slack accepts the bot's token, else why not."""
    req = urllib.request.Request("https://slack.com/api/auth.test", data=b"",
                                 headers={"Authorization": f"Bearer {os.environ['SLACKBOT_BOT_TOKEN']}"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            out = json.loads(r.read())
    except (urllib.error.URLError, OSError, ValueError) as e:
        return f"Slack not reachable ({e})"
    return None if out.get("ok") else f"Slack refused the bot token ({out.get('error')})"


# -- tools ------------------------------------------------------------------- #

@mcp.tool()
def describe_sources() -> Result:
    """What the swarm has connected: sources, how much of each, and what it extracted from them."""
    if _scripted():
        return _replay("describe_sources")
    from graph.store import Neo4jStore

    rows = Neo4jStore()._run(
        "MATCH (n {ns: $ns}) RETURN n.source AS source, labels(n)[0] AS label, n.kind AS kind, count(*) AS n", ns=NS)
    count = {(r["source"], r["label"], r["kind"]): r["n"] for r in rows}

    def c(source=None, label=None, kind=None) -> int:
        return sum(n for (s, lab, k), n in count.items()
                   if (source is None or s == source) and (label is None or lab == label) and (kind is None or k == kind))

    projects = Neo4jStore()._run(
        "MATCH (n:Issue {ns: $ns, source: 'jira'}) WHERE n.key IS NOT NULL "
        "RETURN count(DISTINCT split(n.key, '-')[0]) AS n", ns=NS)[0]["n"]
    edges = Neo4jStore()._run("MATCH (n {ns: $ns})-[r]->() RETURN count(r) AS n", ns=NS)[0]["n"]
    sources = {
        "github": {"repos": c("github", "Container", "repo"), "pull_requests": c("github", "Issue", "pull_request"),
                   "issues": c("github", "Issue", "issue"), "commits": c("github", "Commit")},
        "jira": {"projects": projects, "issues": c("jira", "Issue")},
        "slack": {"channels": c("slack", "Container", "channel"), "threads": c("slack", "Document", "thread")},
        "gdrive": {"folders": c("gdrive", "Container", "folder"), "documents": c("gdrive", "Document")},
    }
    extracted = {"people": c("swarm", "Person"), "teams": c("swarm", "Team"), "decisions": c("swarm", "Decision"),
                 "systems": c("swarm", "System"), "customers": c("swarm", "Customer"),
                 "action_items": c("swarm", "ActionItem"), "connections": edges}
    s = sources
    text = "\n".join([
        f"Connected by the swarm (graph {NS}):",
        f"  GitHub   {s['github']['repos']} repos · {s['github']['pull_requests']} PRs · {s['github']['issues']} issues · {s['github']['commits']} commits",
        f"  Jira     {s['jira']['projects']} projects · {s['jira']['issues']} issues",
        f"  Slack    {s['slack']['channels']} channels · {s['slack']['threads']} threads",
        f"  Drive    {s['gdrive']['folders']} folders · {s['gdrive']['documents']} documents",
        f"Extracted: {extracted['people']} people · {extracted['teams']} teams · {extracted['decisions']} decisions · "
        f"{extracted['customers']} customers · {extracted['connections']:,} connections",
        "Every read is checked against the asker's own access in each source."])
    return _ok({"ns": NS, "sources": sources, "extracted": extracted}, text)


@mcp.tool()
def plan_surfaces(surfaces: list[str]) -> Result:
    """Plan Tesseract on the surfaces a team works in (e.g. docs, slack, zoom, tickets): what it does on
    each, whose access it reads as, and what needs a person's approval."""
    plan, unknown, lines = [], [], []
    for name in surfaces:
        key = _surface(name)
        if key is None:
            unknown.append(name)
            continue
        s = SURFACES[key]
        plan.append({"surface": key, "name": s["name"], "does": s["does"], "reads_as": s["reads_as"], "approval": s["approval"]})
        lines += [f"{s['name']}", *(f"  • {d}" for d in s["does"]), f"  reads as: {s['reads_as']}", f"  approval: {s['approval']}", ""]
    lines.append("One core holds the shared thread, so work follows a person from a call to a doc to Slack to a ticket.")
    if unknown:
        lines.append(f"No template yet for: {', '.join(unknown)}")
    return _ok({"plan": plan, "unknown": unknown}, "\n".join(lines))


@mcp.tool()
def deploy_surface(surface: str) -> Result:
    """Set Tesseract up on one surface (docs, slack, zoom or tickets) and check it is live."""
    key = _surface(surface)
    if key is None:
        return CallToolResult(is_error=True, content=[TextContent(type="text", text=f"no template for {surface!r}")],
                              structured_content={"error": f"no template for {surface!r}"})
    s = SURFACES[key]
    log = [f"→ {step}" for step in s["steps"]]
    if _scripted():
        time.sleep(1)
        log.append(f"✓ {s['name']} is live · {s['where']}")
        return _ok({"surface": key, "live": True, "where": s["where"], "error": None}, "\n".join(log))
    why = _up(f"{CORE}/health")
    if why is None and key == "zoom":
        why = _up(MEETING)
    if why is None and key == "tickets":
        why = _up(f"{DESK}/api/state")
    if why is None and key in ("docs", "slack", "zoom") and key not in _keys():
        why = f"no {key} key in data/core-keys.json"
    if why is None and key == "slack" and os.environ.get("SLACKBOT_BOT_TOKEN"):
        why = _slack()
    log.append(f"✓ {s['name']} is live · {s['where']}" if why is None else f"✗ {s['name']}: {why}")
    return _ok({"surface": key, "live": why is None, "where": s["where"], "error": why}, "\n".join(log))


@mcp.tool()
def smoke_test(surface: str) -> Result:
    """Ask one real question through a surface (docs, slack, zoom or tickets) and show the cited answer."""
    key = _surface(surface)
    if key is None:
        return _ok({"surface": surface, "passed": False}, f"no template for {surface!r}")
    if _scripted():
        return _replay("smoke_test", key)
    keys, t0 = _keys(), time.time()
    down = _up(MEETING) if key == "zoom" else None
    if down:
        return _ok({"surface": key, "passed": False, "error": down}, f"✗ {SURFACES[key]['name']}: the meeting page is {down}")
    try:
        if key in ("slack", "zoom"):
            body = {"as": ASKER, "question": SMOKE[key]} | ({"audience": CALL} if key == "zoom" else {})
            a = _http(f"{CORE}/answer", body, keys.get(key))
            claims = a.get("claims") or []
            passed = a.get("status") == "INFO" and bool(claims)
            first = claims[0] if claims else {}
            detail = {"question": SMOKE[key], "answer": a.get("headline"), "cites": first.get("title"), "url": first.get("url")}
            text = f"Q: {SMOKE[key]}\nA: {a.get('headline')}" + (f"\n   cites: {first.get('title')}" if first else "")
        elif key == "docs":
            section, paragraph = SMOKE["docs"]
            r = _http(f"{CORE}/docs/context", {"paragraph": paragraph, "section": section, "viewer": ASKER, "by": ASKER,
                                               "doc": "Design: billing-service verifies OIDC tokens directly"}, keys.get("docs"))
            notes = r.get("notes") or []
            passed = bool(notes)
            note = notes[0] if notes else {}
            detail = {"passage": paragraph[:80], "note": note.get("kind"), "text": note.get("text")}
            text = f"Check on §{section}: “{paragraph[:70]}…”\n→ {note.get('kind', 'no notes')}: {note.get('text') or ''}"
        else:
            state = _http(f"{DESK}/api/state")
            run = (state.get("runs", {}).get(SMOKE["tickets"], {}).get("graph") or {}).get("result") or {}
            triage = (run.get("answer") or {}).get("triage") or {}
            ticket = next((t for t in state.get("tickets", []) if t["id"] == SMOKE["tickets"]), {})
            passed = bool(triage) and bool(run.get("sources"))
            detail = {"ticket": SMOKE["tickets"], "subject": ticket.get("subject"), "classification": triage.get("classification"),
                      "sources": len(run.get("sources") or {})}
            known = (run.get("answer") or {}).get("known_fix") or {}
            detail["fix_that_worked"] = known.get("resolved_before") if known.get("found") else None
            text = (f"{SMOKE['tickets']} “{ticket.get('subject')}”\n→ {triage.get('classification', 'not worked yet')}, "
                    f"{len(run.get('sources') or {})} cited sources: {(triage.get('summary') or '')[:200]}"
                    + (f"\n→ fix that worked before: {known['resolved_before']}" if known.get("found") else ""))
    except (urllib.error.URLError, OSError, ValueError, AttributeError) as e:
        return _ok({"surface": key, "passed": False, "error": str(e)}, f"✗ {SURFACES[key]['name']}: {e}")
    took = time.time() - t0
    mark = "✓" if passed else "✗"
    return _ok({"surface": key, "passed": passed, "seconds": round(took, 1), **detail},
               f"{mark} {SURFACES[key]['name']} ({took:.0f}s)\n{text}")


def record() -> dict:
    """Run describe_sources and every smoke test for real, and save what they returned for scripted
    mode. Zoom's question is answered by the core, so it is recorded without the meeting page."""
    global MEETING
    meeting, MEETING = MEETING, f"{CORE}/health"
    try:
        out = {"describe_sources": describe_sources(), "smoke_test": {k: smoke_test(k) for k in SMOKE}}
    finally:
        MEETING = meeting
    saved = {"describe_sources": {"data": out["describe_sources"].structured_content,
                                  "text": out["describe_sources"].content[0].text},
             "smoke_test": {k: {"data": r.structured_content, "text": r.content[0].text} for k, r in out["smoke_test"].items()}}
    failed = [k for k, r in saved["smoke_test"].items() if not r["data"].get("passed")]
    if failed:
        raise RuntimeError(f"not saved: the smoke test failed for {', '.join(failed)}")
    SAVED.write_text(json.dumps(saved, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    return saved
