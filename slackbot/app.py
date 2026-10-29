"""
app.py - the Slack app (Bolt, Socket Mode: no public URL, runs next to the graph on a laptop).

    DM a question           answer card, as the asker: graph reads checked against their access
    @Tesseract in a channel the same card, ephemeral; Send to thread posts it when everyone there can read it
    Request access          review card: the one approval, over an exact-payload hash
    Send request            the owner gets Grant / Decline in their DM
    Grant                   Drive sharing changed as the owner (drive.py); the asker gets the answer re-read
    draft a review request  from the person's thread (core/threads.py): the exact text, posted on approval
    make a ticket           the thread's brief, handed to the ticketing bot on approval

Who someone is comes from Slack: their email (users:read.email) is the viewer graph/swarm_queries.py
checks every read against. Every read goes through the Tesseract core (core/service.py), which runs
in this process; the thread a person is working in is context for their asks, never a source.
Nothing is posted anywhere a person did not click to post.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import threading
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from slack_sdk.errors import SlackApiError
from slack_sdk.webhook import WebhookClient

from core.answer import Answer
from core.service import REVIEW_INTENT, TICKET_INTENT, Core
from core.threads import Denied

from . import cards
from .drive import Drive, DriveError, serve

log = logging.getLogger("slackbot")

CONTEXT_MESSAGES = 8          # messages above a top-level mention read as its context
CONTEXT_CHARS = 4000
# A question that names nothing and points at the conversation instead.
POINTING = re.compile(r"\b(this|that|these|those|it|above|here|thoughts?|think|thread|idea|plan)\b", re.I)
ABOUT_THIS = "What do our records say about this?"
HELP = ("Ask me anything about the company's work, e.g. `/tesseract has anyone built a retraining pipeline?`. "
        "I answer from what you're allowed to read, quoted, and only you see the answer.")


def clock(dt: datetime | None = None) -> str:
    return (dt or datetime.now()).strftime("%I:%M %p").lstrip("0")


class Bot:
    def __init__(self, core: Core, meeting: bool = False):
        self.core = core
        self.days = int(os.environ.get("SLACKBOT_ACCESS_DAYS", "30"))
        self.graph = core.graph          # the core's reads: items for owner cards, viewers
        self.ledger = core.ledger
        self.drive = Drive(os.environ.get("SLACKBOT_GOOGLE_CLIENT", "data/gdrive/oauth-client.json"),
                           int(os.environ.get("SLACKBOT_OAUTH_PORT", "8765")))
        self.app = App(token=os.environ["SLACKBOT_BOT_TOKEN"])
        self.client = self.app.client
        auth = self.client.auth_test()
        self.bot_user = auth["user_id"]
        core.slack = {"team": auth["team_id"], "bot_user": self.bot_user, "url": auth.get("url")}
        core.subscribe(self.on_core)
        self.answers: OrderedDict[str, dict] = OrderedDict()   # card key -> what its buttons act on
        self.emails: dict[str, str | None] = {}
        self.names: dict[str, str] = {}
        self.pending: dict[str, dict] = {}     # Ask Tesseract modals waiting for their question
        self.lock = threading.Lock()
        self.meeting = None
        if meeting:
            from .meeting import Meeting

            self.people = self.meeting_people()
            self.host = os.environ.get("SLACKBOT_MEETING_HOST", "").lower() or self.people[0]
            try:
                host_name = self.name(self.client.users_lookupByEmail(email=self.host)["user"]["id"])
            except SlackApiError:
                host_name = self.host.split("@")[0]
            self.meeting = Meeting(host_name, os.environ.get("SLACKBOT_MEETING_DIR", "data/meetings"))
        self._routes()

    # -- who is asking ------------------------------------------------------------------------- #

    def email(self, user: str) -> str | None:
        if user not in self.emails:
            u = self.client.users_info(user=user)["user"]
            self.emails[user] = None if u.get("is_bot") else (u.get("profile", {}).get("email") or "").lower() or None
        return self.emails[user]

    def name(self, user: str) -> str:
        if user not in self.names:
            u = self.client.users_info(user=user)["user"]
            self.names[user] = u.get("real_name") or u.get("profile", {}).get("real_name") or u.get("name") or user
        return self.names[user]

    def conversation(self, channel: str, ts: str, thread_ts: str | None = None) -> str | None:
        """What a question pointed at, read at the moment it was asked and never stored: the thread up to
        the question, or the last few messages above it. Bot messages are left out."""
        try:
            if thread_ts:
                msgs = self.client.conversations_replies(channel=channel, ts=thread_ts, latest=ts, inclusive=False,
                                                         limit=CONTEXT_MESSAGES * 3)["messages"]
            else:
                msgs = self.client.conversations_history(channel=channel, latest=ts, inclusive=False,
                                                         limit=CONTEXT_MESSAGES)["messages"][::-1]
        except SlackApiError:
            return None      # not in the channel, or no history scope: answer the question as asked
        return self.render([m for m in msgs if m.get("ts") != ts][-CONTEXT_MESSAGES * 3:])

    def render(self, msgs: list[dict]) -> str | None:
        lines = []
        for m in msgs:
            # A person's message posted through an app (a user token) carries a bot_id too, so the test is
            # whether its author is a bot: email() is None for bot users, this one included.
            if m.get("subtype") or not m.get("user") or self.email(m["user"]) is None:
                continue
            text = re.sub(r"<@([A-Z0-9]+)>", lambda x: "@" + self.name(x.group(1)), m.get("text") or "")
            lines.append(f"{self.name(m['user'])}: {text}")
        return "\n".join(lines)[-CONTEXT_CHARS:] or None

    def dm(self, user: str) -> str:
        return self.client.conversations_open(users=user)["channel"]["id"]

    # -- answering ----------------------------------------------------------------------------- #

    def ask(self, user: str, question: str, granted_extra: list[str] | None = None,
            context: str | None = None) -> Answer:
        """As the asker. Context, never quoted: what the question pointed at; else, mid-call, the last
        minutes of the call; and the thread they are working in, if any."""
        email = self.email(user)
        if context is None and self.meeting and self.meeting.live:
            context = self.meeting.transcript.recent()      # asked mid-call: the call is what "this" is
        working = self.core.working(email)
        context = "\n\n".join(c for c in (context, working) if c) or None
        return self.core.answer(email, question, context=context, granted_extra=granted_extra)

    # -- a live call (meeting.py) -------------------------------------------------------------- #

    def meeting_people(self) -> list[str]:
        """Who is on the call: SLACKBOT_MEETING_PEOPLE, or every person in the workspace."""
        named = [e.strip().lower() for e in os.environ.get("SLACKBOT_MEETING_PEOPLE", "").split(",") if e.strip()]
        if named:
            return named
        people, cursor = [], None
        while True:
            page = self.client.users_list(cursor=cursor, limit=200)
            for u in page["members"]:
                email = (u.get("profile", {}).get("email") or "").lower()
                if email and not u.get("is_bot") and not u.get("deleted") and u.get("id") != "USLACKBOT":
                    people.append(email)
            cursor = page.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                return people

    def meeting_ask(self, question: str, thread: str | None = None) -> Answer:
        """An ask from the shared screen: read against the last minutes of the call (and the call's
        thread), quoting only what everyone on the call may read, since everyone on the call sees it."""
        return self.core.answer(self.host, question, context=self.meeting.transcript.recent(),
                                thread=thread, audience=self.people)

    def shareable(self, channel: str, a: Answer) -> tuple[bool, str | None]:
        if a.status != "INFO":
            return False, None
        if channel.startswith("D"):
            return False, "Send to thread works in channels"
        return self.everyone_can_read(channel, a)

    def remember(self, a: Answer, **ctx) -> str:
        key = hashlib.sha256((a.question + a.headline + "".join(c.quote for c in a.claims) +
                              str(len(self.answers)) + str(datetime.now())).encode()).hexdigest()[:10]
        with self.lock:
            self.answers[key] = {"answer": a, **ctx}
            while len(self.answers) > 500:
                self.answers.popitem(last=False)
        return key

    @staticmethod
    def answer_hash(a: Answer) -> str:
        return hashlib.sha256((a.headline + "|".join(c.hit.id + c.quote for c in a.claims)).encode()).hexdigest()[:6]

    def everyone_can_read(self, channel: str, a: Answer) -> tuple[bool, str | None]:
        """Send to thread only when every human in the channel may read every quoted item."""
        ids = [h.id for h in a.cited]
        members, cursor = [], None
        try:
            while True:
                page = self.client.conversations_members(channel=channel, cursor=cursor, limit=200)
                members += page["members"]
                cursor = page.get("response_metadata", {}).get("next_cursor")
                if not cursor:
                    break
        except SlackApiError:     # a private channel it was not invited to: it cannot see who is there
            return False, "Invite @Tesseract here so it can check who can read the sources"
        emails = [e for e in (self.email(m) for m in members) if e]      # bots, this one included, are None
        if not self.core.everyone_can_read(emails, ids):
            return False, "Someone in this channel can't read every source"
        return True, None

    # -- the thread a person is working in (core/threads.py) ----------------------------------- #

    def thread_action(self, user: str, question: str, channel: str) -> list[dict] | None:
        """"draft a review request for my doc" and "make a ticket" act on the person's thread: each
        becomes an approval showing the exact payload. None when the question is neither."""
        review, ticket = REVIEW_INTENT.search(question), TICKET_INTENT.search(question)
        if not (review or ticket):
            return None
        email = self.email(user)
        t = self.core.threads.active(email)
        if t is None:
            return cards.no_thread()
        try:
            if review:
                if channel.startswith("D"):
                    return cards.notice("Run this in the channel the review request should go to.")
                return cards.review_draft(self.core.draft_review(t["id"], email, channel), t)
            return cards.ticket_draft(self.core.propose_ticket(t["id"], email), t)
        except Denied as e:
            return cards.notice(str(e))

    def on_core(self, event: dict) -> None:
        """What the core announces that someone should hear about in Slack: a thread handed to Slack,
        and a ticket the ticketing bot made."""
        try:
            if event["type"] == "handoff" and event["to"] == "slack":
                user = self.client.users_lookupByEmail(email=event["by"])["user"]["id"]
                t = self.core.threads.get(event["thread"], event["by"], proposed=False)
                self.client.chat_postMessage(channel=self.dm(user), text=f"Continuing: {t['title']}",
                                             blocks=cards.handoff(t, event["from"]))
            elif event["type"] == "done" and event["approval"]["surface"] == "tickets":
                a = event["approval"]
                user = self.client.users_lookupByEmail(email=a["approved_by"])["user"]["id"]
                self.client.chat_postMessage(channel=self.dm(user), text="Ticket created",
                                             blocks=cards.ticket_done(a))
        except Exception:
            log.exception("could not tell Slack about %s", event.get("type"))

    # -- routes -------------------------------------------------------------------------------- #

    def _routes(self):
        app = self.app

        @app.event("message")
        def on_dm(event, say):
            if event.get("channel_type") != "im" or event.get("subtype") or event.get("bot_id"):
                return
            question = (event.get("text") or "").strip()
            if not question:
                return
            user = event["user"]
            if not self.email(user):
                say("I can't see your email in Slack, so I can't tell what you're allowed to read.")
                return
            acted = self.thread_action(user, question, event["channel"])
            if acted is not None:
                say(text="Tesseract", blocks=acted)
                return
            placeholder = say(text="Reading what you can see…")
            a = self.ask(user, question)
            key = self.remember(a, user=user)
            self.client.chat_update(channel=placeholder["channel"], ts=placeholder["ts"], text=cards.fallback(a),
                                    blocks=cards.answer(a, key))

        @app.event("app_mention")
        def on_mention(event):
            user, channel = event["user"], event["channel"]
            question = re.sub(r"<@[A-Z0-9]+>", "", event.get("text") or "").strip()
            thread = event.get("thread_ts") or event["ts"]
            if not self.email(user):
                return
            # In a thread, the thread is what the question is about. At the top of a channel, only a
            # question that points at something ("what do you think of this?") reads the messages above.
            context = None
            if event.get("thread_ts") or not question or POINTING.search(question):
                context = self.conversation(channel, event["ts"], event.get("thread_ts"))
            if not question:
                if not context:
                    return
                question = ABOUT_THIS
            try:
                self.client.reactions_add(channel=channel, timestamp=event["ts"], name="eyes")
            except Exception:
                pass
            a = self.ask(user, question, context=context)
            ok, why = self.shareable(channel, a)
            key = self.remember(a, user=user, channel=channel, thread=thread, hash=self.answer_hash(a))
            self.client.chat_postEphemeral(channel=channel, user=user, thread_ts=event.get("thread_ts"),
                                           text=cards.fallback(a),
                                           blocks=cards.answer(a, key, channel=True, can_thread=ok, thread_reason=why))
            try:
                self.client.reactions_remove(channel=channel, timestamp=event["ts"], name="eyes")
            except Exception:
                pass

        @app.command("/tesseract")
        def slash(ack, command, respond):
            ack()
            user, channel = command["user_id"], command["channel_id"]
            question = (command.get("text") or "").strip()
            if not question:
                respond(response_type="ephemeral", text=HELP)
                return
            if not self.email(user):
                respond(response_type="ephemeral", text="I can't see your email in Slack, so I can't tell what "
                                                        "you're allowed to read.")
                return
            acted = self.thread_action(user, question, channel)
            if acted is not None:
                respond(response_type="ephemeral", text="Tesseract", blocks=acted)
                return
            a = self.ask(user, question)
            ok, why = self.shareable(channel, a)
            key = self.remember(a, user=user, channel=channel, thread=None, hash=self.answer_hash(a))
            respond(response_type="ephemeral", text=cards.fallback(a),
                    blocks=cards.answer(a, key, channel=True, can_thread=ok, thread_reason=why,
                                        intro=f"_/tesseract {cards.esc(question)}_"))

        @app.shortcut("ask_tesseract")
        def ask_shortcut(ack, shortcut, client):
            ack()
            msg, user = shortcut["message"], shortcut["user"]["id"]
            pkey = hashlib.sha256(f"{user}{msg['ts']}{datetime.now()}".encode()).hexdigest()[:12]
            context = self.render([msg]) or msg.get("text") or ""
            with self.lock:
                self.pending[pkey] = {"user": user, "channel": shortcut["channel"]["id"],
                                      "thread": msg.get("thread_ts") or msg["ts"], "context": context,
                                      "response_url": shortcut["response_url"]}
            snippet = context if len(context) <= 280 else context[:277] + "…"
            client.views_open(trigger_id=shortcut["trigger_id"], view={
                "type": "modal", "callback_id": "ask_tesseract_submit", "private_metadata": pkey,
                "title": {"type": "plain_text", "text": "Ask Tesseract"},
                "submit": {"type": "plain_text", "text": "Ask"}, "close": {"type": "plain_text", "text": "Cancel"},
                "blocks": [
                    {"type": "section", "text": {"type": "mrkdwn", "text": ">" + cards.esc(snippet)}},
                    {"type": "input", "block_id": "q", "optional": True,
                     "label": {"type": "plain_text", "text": "Your question"},
                     "element": {"type": "plain_text_input", "action_id": "v",
                                 "placeholder": {"type": "plain_text", "text": "What do we already know about this?"}}},
                    cards._context("Only you see the answer. The message is used to understand your question, "
                                   "never quoted as a source.")]})

        @app.view("ask_tesseract_submit")
        def ask_submit(ack, body, view):
            ack()
            with self.lock:
                p = self.pending.pop(view["private_metadata"], None)
            if not p or p["user"] != body["user"]["id"] or not self.email(p["user"]):
                return
            question = (view["state"]["values"]["q"]["v"].get("value") or "").strip() or ABOUT_THIS
            a = self.ask(p["user"], question, context=p["context"])
            ok, why = self.shareable(p["channel"], a)
            key = self.remember(a, user=p["user"], channel=p["channel"], thread=p["thread"], hash=self.answer_hash(a))
            WebhookClient(p["response_url"]).send(
                response_type="ephemeral", text=cards.fallback(a),
                blocks=cards.answer(a, key, channel=True, can_thread=ok, thread_reason=why,
                                    intro=f"_Ask Tesseract: {cards.esc(question)}_"))

        @app.action("not_this")
        def not_this(ack, body, respond):
            ack()
            if body.get("container", {}).get("is_ephemeral"):
                respond(delete_original=True)
            else:
                respond(replace_original=True, text="Dismissed.",
                        blocks=cards.closed("Dismissed", "Ask again with other words, or name the system or ticket."))

        @app.action("send_thread")
        def send_thread(ack, body, respond):
            ack()
            ctx = self.answers.get(body["actions"][0]["value"])
            if not ctx or ctx["user"] != body["user"]["id"]:
                respond(text="This card has expired. Ask again.", replace_original=False)
                return
            a = ctx["answer"]
            try:
                self.client.chat_postMessage(channel=ctx["channel"], thread_ts=ctx["thread"], text=cards.fallback(a),
                                             blocks=cards.thread_post(a, ctx["user"], ctx["hash"]),
                                             unfurl_links=False)
            except SlackApiError:
                respond(replace_original=False, response_type="ephemeral",
                        text="I can't post here. Invite @Tesseract to this channel first.")
                return
            self.ledger.log("sent to thread", None, ctx["user"], ctx["hash"])
            respond(delete_original=True)

        @app.action("request_access")
        def request_access(ack, body, respond):
            ack()
            key, i = body["actions"][0]["value"].split("|")
            ctx = self.answers.get(key)
            if not ctx or ctx["user"] != body["user"]["id"]:
                respond(text="This card has expired. Ask again.", replace_original=False)
                return
            a = ctx["answer"]
            groups = [(o, hs) for o, hs in cards.by_owner(a.locked) if o]
            owner, items = groups[int(i)]
            user = body["user"]["id"]
            me = self.name(user)
            message = (f"{me} requests read access to {cards.plural(len(items), 'item')} you own, to answer: "
                       f"“{a.question}”")
            r = self.ledger.draft(requester_slack=user, requester_email=self.email(user), requester_name=me,
                                  owner_slack=owner.slack_id, owner_email=owner.email, owner_name=owner.name,
                                  item_ids=[h.id for h in items], sources=sorted({h.source for h in items}),
                                  message=message, days=self.days, question=a.question)
            channel = self.dm(user)
            posted = self.client.chat_postMessage(channel=channel, text="Access request, review before sending",
                                                  blocks=cards.review(r, owner.name, items))
            self.ledger.set(r.id, dm_channel=channel, dm_ts=posted["ts"])
            if body.get("container", {}).get("is_ephemeral"):
                respond(replace_original=False, response_type="ephemeral",
                        text="I've put the access request in our DM for you to review. Nothing is sent yet.")

        def review_again(rid: str):
            r = self.ledger.get(rid)
            items = self.graph.items(r.item_ids)
            self.client.chat_update(channel=r.dm_channel, ts=r.dm_ts, text="Access request, review before sending",
                                    blocks=cards.review(r, r.owner_name or "the owner", items))

        @app.action("edit_request")
        def edit_request(ack, body, client):
            ack()
            r = self.ledger.get(body["actions"][0]["value"])
            if not r or r.status != "draft" or r.requester_slack != body["user"]["id"]:
                return
            client.views_open(trigger_id=body["trigger_id"], view={
                "type": "modal", "callback_id": "edit_request_submit", "private_metadata": r.id,
                "title": {"type": "plain_text", "text": "Edit request"},
                "submit": {"type": "plain_text", "text": "Save"}, "close": {"type": "plain_text", "text": "Back"},
                "blocks": [{"type": "input", "block_id": "msg", "label": {"type": "plain_text", "text": "Message"},
                            "element": {"type": "plain_text_input", "action_id": "v", "multiline": True,
                                        "initial_value": r.message}},
                           cards._context("Saving makes a new payload. You approve it again before it is sent.")]})

        @app.view("edit_request_submit")
        def edit_submit(ack, body, view):
            ack()
            rid = view["private_metadata"]
            r = self.ledger.get(rid)
            if r and r.status == "draft" and r.requester_slack == body["user"]["id"]:
                self.ledger.edit(rid, view["state"]["values"]["msg"]["v"]["value"].strip())
                review_again(rid)

        @app.action("cancel_request")
        def cancel_request(ack, body):
            ack()
            r = self.ledger.get(body["actions"][0]["value"])
            if r and r.requester_slack == body["user"]["id"] and self.ledger.transition(r.id, "draft", "cancelled"):
                self.ledger.log("cancelled", r.id, r.requester_slack, r.hash)
                self.client.chat_update(channel=r.dm_channel, ts=r.dm_ts, text="Request cancelled",
                                        blocks=cards.closed("Request cancelled", "Nothing was sent.", r.hash))

        @app.action("send_request")
        def send_request(ack, body, respond):
            ack()
            rid, h = body["actions"][0]["value"].split("|")
            r = self.ledger.get(rid)
            if not r or r.requester_slack != body["user"]["id"]:
                return
            owner_slack = r.owner_slack
            if not owner_slack and r.owner_email:
                try:
                    owner_slack = self.client.users_lookupByEmail(email=r.owner_email)["user"]["id"]
                except Exception:
                    owner_slack = None
            if not owner_slack:
                respond(replace_original=False, text=f"I can't find {r.owner_name} in Slack, so I can't deliver this.")
                return
            if not self.ledger.transition(rid, "draft", "sent", hash=h, owner_slack=owner_slack,
                                          decided_at=clock()):
                respond(replace_original=False, text="This card is out of date. Review the latest version.")
                return
            r = self.ledger.get(rid)
            self.ledger.log("approved and sent", rid, r.requester_slack, r.hash)
            items = self.graph.items(r.item_ids)
            expires = (datetime.now().astimezone() + timedelta(days=r.days)).date().isoformat()
            channel = self.dm(owner_slack)
            drive = r.sources == ["gdrive"] and self.drive.configured
            posted = self.client.chat_postMessage(channel=channel, text=f"{r.requester_name} requests read access",
                                                  blocks=cards.owner_request(r, items, expires, drive))
            self.ledger.set(rid, owner_channel=channel, owner_ts=posted["ts"])
            self.client.chat_update(channel=r.dm_channel, ts=r.dm_ts, text="Request delivered",
                                    blocks=cards.sent(r, r.owner_name, clock()))

        @app.action("grant")
        def grant(ack, body, respond):
            ack()
            rid, h = body["actions"][0]["value"].split("|")
            r = self.ledger.get(rid)
            if not r or r.owner_slack != body["user"]["id"] or r.status != "sent" or r.hash != h:
                respond(replace_original=False, text="Only the owner can grant this, once.")
                return
            if r.sources == ["gdrive"] and self.drive.configured:
                token = self.ledger.google_token(r.owner_email or "")
                if not token:
                    verifier = self.drive.verifier()
                    state = self.ledger.oauth_state(r.owner_email, rid, verifier)
                    respond(replace_original=False, response_type="ephemeral", text="Connect Google Drive",
                            blocks=cards.connect(self.drive.auth_url(state, verifier, r.owner_email), r.owner_email))
                    return
                self.finish_grant(rid)
            else:
                self.finish_grant(rid, manual=True)

        @app.action("decline")
        def decline(ack, body):
            ack()
            r = self.ledger.get(body["actions"][0]["value"])
            if not r or r.owner_slack != body["user"]["id"] or not self.ledger.transition(r.id, "sent", "declined"):
                return
            self.ledger.log("declined", r.id, r.owner_slack, r.hash)
            self.client.chat_update(channel=r.owner_channel, ts=r.owner_ts, text="Declined",
                                    blocks=cards.closed(f"Declined at {clock()}", "Nothing was shared.", r.hash))
            self.client.chat_postMessage(channel=r.dm_channel, text=f"{r.owner_name} declined",
                                         blocks=cards.closed(f"{r.owner_name} declined the request",
                                                             "Nothing was shared.", r.hash))

        @app.action("connect_google")
        def connect_google(ack):
            ack()

        @app.action("approve_post")
        def approve_post(ack, body, respond):
            """The thread's owner approves the drafted message by its hash; then it posts, labelled."""
            ack()
            aid, h = body["actions"][0]["value"].split("|")
            user = body["user"]["id"]
            try:
                a = self.core.approve(aid, self.email(user) or "", h)
            except Denied as e:
                respond(replace_original=False, response_type="ephemeral", text=str(e))
                return
            p = a["payload"]
            try:
                posted = self.client.chat_postMessage(channel=p["channel"], text=p["text"],
                                                      blocks=cards.posted_message(p["text"], user, a["hash"]))
                link = self.client.chat_getPermalink(channel=p["channel"], message_ts=posted["ts"])["permalink"]
            except SlackApiError as e:
                self.core.done(aid, "slack", {"text": f"Could not post: {e.response['error']}"}, ok=False)
                respond(replace_original=False, response_type="ephemeral",
                        text="I can't post here. Invite @Tesseract to this channel first.")
                return
            self.core.done(aid, "slack", {"text": "Review request posted", "url": link})
            respond(replace_original=True, text="Posted",
                    blocks=cards.posted(a, link, a["thread_id"]))

        @app.action("approve_ticket")
        def approve_ticket(ack, body, respond):
            ack()
            aid, h = body["actions"][0]["value"].split("|")
            try:
                a = self.core.approve(aid, self.email(body["user"]["id"]) or "", h)
            except Denied as e:
                respond(replace_original=False, response_type="ephemeral", text=str(e))
                return
            respond(replace_original=True, text="Sent to the ticketing bot",
                    blocks=cards.closed("Sent to the ticketing bot",
                                        "It creates the issue with this brief, and I'll DM you the link.", a["hash"]))

        @app.action("cancel_approval")
        def cancel_approval(ack, body, respond):
            ack()
            try:
                a = self.core.cancel(body["actions"][0]["value"], self.email(body["user"]["id"]) or "")
            except Denied as e:
                respond(replace_original=False, response_type="ephemeral", text=str(e))
                return
            respond(replace_original=True, text="Cancelled", blocks=cards.closed("Cancelled", "Nothing was sent.",
                                                                                 a["hash"]))

        @app.action("make_ticket")
        def make_ticket(ack, body, respond):
            ack()
            email = self.email(body["user"]["id"]) or ""
            try:
                a = self.core.propose_ticket(body["actions"][0]["value"], email)
                t = self.core.threads.get(a["thread_id"], email, proposed=False)
            except Denied as e:
                respond(replace_original=False, response_type="ephemeral", text=str(e))
                return
            respond(replace_original=False, response_type="ephemeral", text="Ticket brief",
                    blocks=cards.ticket_draft(a, t))

    # -- granting ------------------------------------------------------------------------------ #

    def on_google(self, state: str, code: str) -> str:
        s = self.ledger.take_oauth_state(state)
        if s is None:
            return "This sign-in link has expired. Click Grant again in Slack."
        verifier = s["verifier"]
        email, refresh = self.drive.exchange(code, verifier)
        if email != s["email"]:
            return f"You signed in as {email}, but these files belong to {s['email']}. Click Grant again and " \
                   f"sign in as {s['email']}."
        self.ledger.save_google(email, refresh)
        self.ledger.log("google connected", s["request_id"], email)
        self.finish_grant(s["request_id"])
        return "Connected. The grant went through; you can close this tab and go back to Slack."

    def finish_grant(self, rid: str, manual: bool = False):
        r = self.ledger.get(rid)
        if r.status != "sent":
            return
        expires = datetime.now(timezone.utc) + timedelta(days=r.days)
        if manual:
            ok_ids, note = r.item_ids, "You said you shared them."
        else:
            try:
                results = self.drive.share(self.ledger.google_token(r.owner_email), r.item_ids, r.requester_email,
                                           expires)
            except DriveError as e:
                self.ledger.drop_google(r.owner_email)
                self.client.chat_postMessage(channel=r.owner_channel, text=f"Grant failed: {e}")
                return
            ok_ids = [x["id"] for x in results if x["ok"]]
            failed = [x for x in results if not x["ok"]]
            if not ok_ids:
                self.client.chat_postMessage(channel=r.owner_channel,
                                             text="Drive refused every share: " + "; ".join(x["error"] for x in failed))
                return
            note = "Shared in Drive as you, read only."
            if failed:
                note += f" {len(failed)} could not be shared: " + "; ".join(x["error"] for x in failed) + "."
            if not all(x["expiry"] for x in results if x["ok"]):
                note += " Drive would not set an expiry on some files, so they stay shared until you remove them."
        if not self.ledger.transition(rid, "sent", "granted", note=note):
            return
        self.ledger.grant(r.requester_email, ok_ids, r.days, rid)
        self.ledger.log("granted", rid, r.owner_slack, r.hash)
        until = expires.astimezone().date().isoformat()
        self.client.chat_update(channel=r.owner_channel, ts=r.owner_ts, text="Granted",
                                blocks=cards.closed(f"Granted at {clock()}",
                                                    f"{r.requester_name} can read {cards.plural(len(ok_ids), 'item')} "
                                                    f"until {until}. {note}", r.hash))
        intro = (f"*{cards.esc(r.owner_name)} granted read, {r.days} days. "
                 f"{cards.plural(len(ok_ids), 'item')} read.*")
        if r.question:
            a = self.ask(r.requester_slack, r.question, granted_extra=ok_ids)
            key = self.remember(a, user=r.requester_slack)
            self.client.chat_postMessage(channel=r.dm_channel, text=cards.fallback(a),
                                         blocks=cards.answer(a, key, intro=intro))
        else:
            self.client.chat_postMessage(channel=r.dm_channel, text=intro)

    def run(self):
        if self.meeting:
            from .page import serve as serve_page

            port = int(os.environ.get("SLACKBOT_MEETING_PORT", "8766"))
            serve_page(self.meeting, self.meeting_ask, port, core=self.core, host=self.host, people=self.people)
            log.info("Tesseract Meeting on http://127.0.0.1:%d (host %s, %d people on the call)",
                     port, self.host, len(self.people))
        if self.drive.configured:
            serve(self.drive, self.on_google)
            log.info("Google sign-in callback on %s", self.drive.redirect)
        else:
            log.warning("No %s: Drive grants fall back to the owner sharing by hand", self.drive.client_file)
        SocketModeHandler(self.app, os.environ["SLACKBOT_APP_TOKEN"]).start()

