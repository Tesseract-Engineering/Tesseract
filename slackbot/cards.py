"""
cards.py - the Slack messages, as Block Kit. Plain functions of their inputs; app.py sends them.

    answer          the cited answer; the lock line and Request access when matching items are locked
    review          the one approval: to, items, scope, message, hash
    sent            delivered, waiting on the owner
    owner_request   the owner's side: Grant / Decline against the exact payload
    connect         an owner's one-time Google connection, so Grant can share as them
    thread_post     what Send to thread posts: the claims and sources, never the lock line
"""

from __future__ import annotations

from collections import OrderedDict

from core.answer import Answer
from core.graph import Hit, Who

SOURCE = {"gdrive": "Drive", "slack": "Slack", "jira": "Jira", "github": "GitHub"}
KIND = {"document": "doc", "thread": "thread", "issue": "ticket", "pull_request": "PR", "commit": "commit"}


def esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def where(h: Hit) -> str:
    kind = KIND.get(h.kind, h.kind)
    if h.source == "jira" and h.kind == "issue":
        kind = "ticket"
    return f"{SOURCE.get(h.source, h.source)} {kind}".strip()


def link(h: Hit) -> str:
    return f"<{h.url}|{esc(h.title)}>" if h.url else esc(h.title)


def cite(h: Hit) -> str:
    bits = [link(h), where(h)]
    if h.when:
        bits.append(h.when.date().isoformat())
    if h.authors:
        bits.append("by " + esc(", ".join(h.authors[:2])))
    return "— " + " · ".join(bits)


def plural(n: int, one: str, many: str | None = None) -> str:
    return f"{n} {one if n == 1 else (many or one + 's')}"


def kinds(hits: list[Hit]) -> str:
    """'2 Drive docs and 1 Slack thread' - what is locked, without saying which."""
    counts: OrderedDict[str, int] = OrderedDict()
    for h in hits:
        counts[where(h)] = counts.get(where(h), 0) + 1
    parts = [plural(n, w) for w, n in counts.items()]
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def by_owner(locked: list[Hit]) -> list[tuple[Who | None, list[Hit]]]:
    groups: OrderedDict[str, tuple[Who | None, list[Hit]]] = OrderedDict()
    for h in locked:
        o = h.owners[0] if h.owners else None
        key = (o.person_id or o.name) if o else ""
        groups.setdefault(key, (o, []))[1].append(h)
    return list(groups.values())


def _context(*texts: str) -> dict:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": t} for t in texts]}


def _button(text: str, action: str, value: str, style: str | None = None, url: str | None = None) -> dict:
    b = {"type": "button", "text": {"type": "plain_text", "text": text, "emoji": True}, "action_id": action,
         "value": value}
    if style:
        b["style"] = style
    if url:
        b["url"] = url
    return b


def _fields(pairs: list[tuple[str, str]]) -> dict:
    return {"type": "section", "fields": [{"type": "mrkdwn", "text": f"*{k}*\n{v}"} for k, v in pairs][:10]}


def _claims(a: Answer) -> list[dict]:
    blocks = []
    for c in a.claims:
        text = esc(c.text) + "\n" if c.text and c.text.strip() != c.quote else ""
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"{text}>{esc(c.quote)}"}})
        blocks.append(_context(cite(c.hit)))
    return blocks


def _chips(hits: list[Hit]) -> dict:
    return _context(*[f"{SOURCE.get(h.source, h.source)} · {link(h)}" for h in hits[:8]])


def lock_line(a: Answer) -> str | None:
    if not a.locked:
        return None
    if a.for_group:     # seen by everyone: how many, never what or whose
        return f"🔒 {plural(len(a.locked), 'related item')} hidden: not everyone here can read " \
               f"{'it' if len(a.locked) == 1 else 'them'}."
    owners = [o.name for o, _ in by_owner(a.locked) if o]
    who = (" Owner: *" + esc(", ".join(owners)) + "*.") if owners else " Owner unknown."
    return f"🔒 {plural(len(a.locked), 'related item')} ({kinds(a.locked)}) {'is' if len(a.locked) == 1 else 'are'} " \
           f"restricted. Not read.{who}"


def answer(a: Answer, key: str, *, channel: bool = False, can_thread: bool = False,
           thread_reason: str | None = None, intro: str | None = None) -> list[dict]:
    blocks: list[dict] = []
    if intro:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": intro}})
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*{esc(a.headline)}*"}})
    if a.status == "INFO":
        blocks += _claims(a)
        blocks.append(_chips(a.cited))
    else:
        blocks.append(_context("Searched for " + ", ".join(f"`{esc(t)}`" for t in a.searched[:8])))
        if a.misses:
            blocks.append(_context("Nearest misses, not answers: " + " · ".join(link(h) for h in a.misses)))
    line = lock_line(a)
    if line:
        blocks.append(_context(line))
    if a.context:
        blocks.append(_context("Read with the conversation you pointed at as context. It's never quoted as a source."))

    buttons = []
    if channel:
        if a.status == "INFO" and can_thread:
            buttons.append(_button("Send to thread", "send_thread", key, "primary"))
    groups = [(o, hs) for o, hs in by_owner(a.locked) if o]
    for i, (o, _items) in enumerate(groups[:3]):
        label = "Request access" if len(groups) == 1 else f"Ask {o.name.split()[0]}"
        buttons.append(_button(label, "request_access", f"{key}|{i}", None if channel or i else "primary"))
    buttons.append(_button("Not this", "not_this", key))
    blocks.append({"type": "actions", "elements": buttons})

    if channel:
        note = "Only you can see this. Send to thread posts the answer and its sources, not the lock line."
        if a.status == "INFO" and not can_thread:
            note = f"Only you can see this. {thread_reason or 'Not everyone here can read these sources'}, " \
                   "so Send to thread is off."
        blocks.append(_context(note))
    else:
        blocks.append(_context("Read only what you can see. Request access shows exactly what would be sent "
                               "before anything is sent."))
    return blocks


def review(r, owner: str, locked: list[Hit]) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": "*Access request, review before sending*"}},
        _fields([("To", f"{esc(owner)} (owner)"),
                 ("Items", f"{kinds(locked)}. Titles are shown to {esc(owner.split()[0])}, and to you once granted"),
                 ("Scope", f"Read only · {r.days} days"), ("Message", f"“{esc(r.message)}”")]),
        {"type": "actions", "elements": [
            _button("Send request", "send_request", f"{r.id}|{r.hash}", "primary"),
            _button("Edit", "edit_request", r.id), _button("Cancel", "cancel_request", r.id)]},
        _context(f"Tesseract delivers this to {esc(owner)} in your name. Edits need a new approval. `{r.hash}`"),
    ]


def sent(r, owner: str, at: str) -> list[dict]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text": f"*Request delivered to {esc(owner)}, {at}*"}},
            _context(f"You approved this request; sent by Tesseract. Waiting on {esc(owner)}. `{r.hash}`")]


def closed(title: str, note: str, h: str | None = None) -> list[dict]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{esc(title)}*"}},
            _context(esc(note) + (f" `{h}`" if h else ""))]


def owner_request(r, items: list[Hit], expires: str, drive: bool) -> list[dict]:
    listing = "\n".join(f"• {link(h)} ({where(h)})" for h in items)
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
                                     "text": f"*{esc(r.requester_name or r.requester_email)} requests read access "
                                             f"to {plural(len(items), 'item')}*"}},
        _fields([("From", f"<@{r.requester_slack}>, approved {r.decided_at or ''}".strip(", ")),
                 ("Scope", f"Read only · {r.days} days · expires {expires}"),
                 ("Why", f"“{esc(r.message)}”"),
                 ("They asked", f"“{esc(r.question or '')}”")]),
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Items*\n{listing}"}},
    ]
    if drive:
        blocks.append({"type": "actions", "elements": [
            _button("Grant", "grant", f"{r.id}|{r.hash}", "primary"), _button("Decline", "decline", r.id)]})
        blocks.append(_context("You own these, so Grant shares them in Drive as you. Logged with the request. "
                               f"`{r.hash}`"))
    else:
        src = ", ".join(sorted({SOURCE.get(h.source, h.source) for h in items}))
        blocks.append({"type": "actions", "elements": [
            _button("I've shared them", "grant", f"{r.id}|{r.hash}", "primary"), _button("Decline", "decline", r.id)]})
        blocks.append(_context(f"Tesseract can't change access in {src}. Share them there, then tell "
                               f"{esc(r.requester_name or 'them')} here. `{r.hash}`"))
    return blocks


def connect(url: str, owner_email: str) -> list[dict]:
    return [
        {"type": "section", "text": {"type": "mrkdwn",
                                     "text": "*Connect Google Drive once, so Grant can share as you*\n"
                                             f"Sign in as *{esc(owner_email)}*. Tesseract only shares the files you "
                                             "grant, read only, and the grant goes through as soon as you finish."}},
        {"type": "actions", "elements": [_button("Connect Google Drive", "connect_google", "x", "primary", url=url)]},
    ]


def thread_post(a: Answer, asker: str, h: str) -> list[dict]:
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"*{esc(a.headline)}*"}}]
    blocks += _claims(a)
    blocks.append(_context(f"Shared by <@{asker}> via Tesseract · `{h}`"))
    return blocks


def fallback(a: Answer) -> str:
    """Notification text for a card."""
    if a.status == "INFO" and a.claims:
        return f"{a.headline}: {a.claims[0].quote[:200]}"
    return a.headline


# -- the thread a person is working in (core/threads.py) ---------------------------------------- #

SURFACE = {"zoom": "the call", "docs": "Docs", "slack": "Slack", "tickets": "ticketing"}


def notice(text: str) -> list[dict]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text": esc(text)}}]


def no_thread() -> list[dict]:
    return notice("You're not working in a thread yet. Start one on the call page, or pick one in the Docs "
                  "sidebar, then ask again.")


def review_draft(a: dict, t: dict) -> list[dict]:
    """The exact message that would post, and its hash: the one approval."""
    p = a["payload"]
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Review request, review before posting*\n"
                                                              f"From your thread _{esc(t['title'])}_"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": ">" + p["text"].replace("\n", "\n>")}},
        {"type": "actions", "elements": [
            _button("Post", "approve_post", f"{a['id']}|{a['hash']}", "primary"),
            _button("Cancel", "cancel_approval", a["id"])]},
        _context(f"Posts exactly this in <#{p['channel']}>, labelled as sent through Tesseract. Only the thread's "
                 f"owner can approve it. `{a['hash']}`"),
    ]


def posted_message(text: str, user: str, h: str) -> list[dict]:
    return [{"type": "section", "text": {"type": "mrkdwn", "text": text}},
            _context(f"Sent by <@{user}> via Tesseract · `{h}`")]


def posted(a: dict, link: str, thread: str | None) -> list[dict]:
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"*Posted* · <{link}|view message>"}}]
    if thread:
        blocks.append({"type": "actions", "elements": [_button("Make a ticket", "make_ticket", thread, "primary")]})
    blocks.append(_context(f"You approved this text. Logged on the thread. `{a['hash']}`"))
    return blocks


def ticket_draft(a: dict, t: dict) -> list[dict]:
    p = a["payload"]
    brief = p["description"] if len(p["description"]) <= 2800 else p["description"][:2797] + "…"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Ticket, review before creating*\n*{esc(p['summary'])}*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "```" + brief.replace("```", "'''") + "```"}},
        {"type": "actions", "elements": [
            _button("Create ticket", "approve_ticket", f"{a['id']}|{a['hash']}", "primary"),
            _button("Cancel", "cancel_approval", a["id"])]},
        _context("The ticketing bot creates the issue with exactly this brief, for a coding agent to start from. "
                 f"Only the thread's owner can approve it. `{a['hash']}`"),
    ]


def ticket_done(a: dict) -> list[dict]:
    r = a.get("result") or {}
    what = f"<{r['url']}|{esc(r.get('text') or r['url'])}>" if r.get("url") else esc(r.get("text") or "Created")
    return [{"type": "section", "text": {"type": "mrkdwn", "text": f"*Ticket created* · {what}"}},
            _context(f"From your thread, with the brief you approved. `{a['hash']}`")]


def handoff(t: dict, came_from: str) -> list[dict]:
    decided = [e.get("text", "") for e in t["entries"] if e["kind"] == "decision"]
    open_q = [e.get("text", "") for e in t["entries"] if e["kind"] == "question"]
    docs = [e for e in t["entries"] if e["kind"] == "link" and e.get("url")]
    lines = [f"*Continuing in Slack:* {esc(t['title'])}", f"_from {SURFACE.get(came_from, came_from)}_"]
    lines += [f"• Decided: {esc(d)}" for d in decided] + [f"• Open: {esc(q)}" for q in open_q]
    lines += [f"• Doc: <{d['url']}|{esc(d.get('title') or 'design doc')}>" for d in docs[-1:]]
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        {"type": "actions", "elements": [_button("Make a ticket", "make_ticket", t["id"])]},
        _context("Your asks here use this thread as context. In a channel, `/tesseract draft a review request "
                 "for my doc` drafts the message for you to approve."),
    ]
