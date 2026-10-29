"""
page.py - the Tesseract Meeting page, served on this PC only (127.0.0.1), for the host to share
on the call.

    GET  /          the page
    GET  /events    server-sent events: transcript lines, words still settling, answers, status
    POST /start     {"consent": true}: start transcribing (refused without the host's confirmation)
    POST /stop      stop transcribing; nothing more is sent to the transcription service
    POST /ask       {"question": "..."}: answered for everyone on the call, with the last minutes as context
"""

from __future__ import annotations

import json
import logging
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from . import cards
from .answer import Answer

log = logging.getLogger("slackbot.page")


def answer_json(a: Answer, question: str, asker: str) -> dict:
    return {
        "type": "answer", "question": question, "asker": asker, "status": a.status, "headline": a.headline,
        "claims": [{"text": c.text if c.text.strip() != c.quote else "", "quote": c.quote, "title": c.hit.title,
                    "url": c.hit.url, "where": cards.where(c.hit),
                    "when": c.hit.when.date().isoformat() if c.hit.when else None,
                    "authors": c.hit.authors[:2]} for c in a.claims],
        "locked": cards.lock_line(a), "searched": a.searched[:8],
        "misses": [{"title": h.title, "url": h.url} for h in a.misses], "context": bool(a.context),
    }


def serve(meeting, ask: Callable[[str], Answer], port: int) -> ThreadingHTTPServer:
    transcript = meeting.transcript

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def _json(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            try:
                return json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return {}

        def do_GET(self):
            if self.path == "/":
                data = PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif self.path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                q = transcript.subscribe()
                try:
                    self.wfile.write(f"data: {json.dumps({'type': 'status', **meeting.status()})}\n\n".encode())
                    self.wfile.flush()
                    while True:
                        try:
                            event = q.get(timeout=15)
                        except queue.Empty:
                            event = {"type": "status", **meeting.status()}     # keepalive, and errors surface
                        self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
                finally:
                    transcript.unsubscribe(q)
            else:
                self.send_error(404)

        def do_POST(self):
            body = self._body()
            if self.path == "/start":
                if body.get("consent") is not True:
                    self._json(400, {"error": "Confirm that everyone on the call has been told first."})
                    return
                try:
                    meeting.start()
                except Exception as e:
                    self._json(500, {"error": str(e)})
                    return
                self._json(200, meeting.status())
            elif self.path == "/stop":
                meeting.stop()
                self._json(200, meeting.status())
            elif self.path == "/ask":
                question = (body.get("question") or "").strip()
                if not question:
                    self._json(400, {"error": "Ask a question."})
                    return
                self._json(202, {"ok": True})
                transcript.emit({"type": "asking", "question": question, "asker": meeting.host_name})

                def work():
                    try:
                        transcript.emit(answer_json(ask(question), question, meeting.host_name))
                    except Exception as e:
                        log.exception("meeting ask failed")
                        transcript.emit({"type": "ask_error", "question": question, "error": str(e)[:200]})
                threading.Thread(target=work, daemon=True).start()
            else:
                self.send_error(404)

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Tesseract Meeting</title>
<style>
:root{--bg:#f5f4f1;--panel:#fff;--ink:#1d1c1d;--muted:#5f5e5b;--faint:#8d8c88;--line:#e4e2dd;--soft:#f1f0ec;
  --brand:#4b55c8;--brand-soft:#eef0ff;--me:#2f7a4f;--me-soft:#e6f3ea;--them:#8a5a12;--them-soft:#fbf1df;
  --live:#c62828;--warn-soft:#fff7d6;--warn:#8a6d00;--quote:#faf9f6}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#161616;--panel:#212121;--ink:#f1f0ec;--muted:#c4c2bd;
  --faint:#94928d;--line:#353533;--soft:#2a2a28;--brand:#9aa2ff;--brand-soft:#262a45;--me:#7cd69b;--me-soft:#1d3326;
  --them:#e9b56a;--them-soft:#3a2d17;--live:#ff6b6b;--warn-soft:#3a3214;--warn:#e6c54a;--quote:#1b1b1a}}
:root[data-theme="dark"]{--bg:#161616;--panel:#212121;--ink:#f1f0ec;--muted:#c4c2bd;--faint:#94928d;--line:#353533;
  --soft:#2a2a28;--brand:#9aa2ff;--brand-soft:#262a45;--me:#7cd69b;--me-soft:#1d3326;--them:#e9b56a;--them-soft:#3a2d17;
  --live:#ff6b6b;--warn-soft:#3a3214;--warn:#e6c54a;--quote:#1b1b1a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font:16px/1.5 "Segoe UI",system-ui,-apple-system,sans-serif;height:100vh;display:flex;flex-direction:column}
header{display:flex;align-items:center;gap:14px;padding:12px 20px;background:var(--panel);border-bottom:1px solid var(--line)}
header h1{font-size:19px;margin:0;font-weight:650;letter-spacing:-.01em}
.mark{width:26px;height:26px;color:var(--brand)}
.state{display:flex;align-items:center;gap:8px;font-size:14px;color:var(--muted);margin-left:auto}
.dot{width:10px;height:10px;border-radius:50%;background:var(--faint)}
.live .dot{background:var(--live);animation:pulse 1.4s ease-in-out infinite}
@keyframes pulse{50%{opacity:.35}}
button{font:inherit;font-size:14px;border-radius:8px;border:1px solid var(--line);background:var(--panel);color:var(--ink);padding:7px 14px;cursor:pointer}
button.primary{background:var(--brand);border-color:var(--brand);color:#fff}
button:disabled{opacity:.45;cursor:default}
.banner{padding:9px 20px;font-size:14px;background:var(--warn-soft);color:var(--ink);border-bottom:1px solid var(--line);display:flex;gap:12px;align-items:center;flex-wrap:wrap}
.banner b{color:var(--warn)}
.banner label{display:flex;gap:8px;align-items:center;cursor:pointer}
main{flex:1;display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1.1fr);min-height:0}
section{display:flex;flex-direction:column;min-height:0}
section+section{border-left:1px solid var(--line)}
.head{padding:12px 20px 8px;font-size:12px;letter-spacing:.07em;text-transform:uppercase;color:var(--faint);font-weight:600}
#lines{flex:1;overflow:auto;padding:0 20px 16px;display:flex;flex-direction:column;gap:10px}
.ln{display:grid;grid-template-columns:auto 1fr;gap:4px 10px;align-items:baseline}
.who{font-size:13px;font-weight:650;padding:1px 8px;border-radius:999px;white-space:nowrap}
.who.mic{background:var(--me-soft);color:var(--me)}.who.speakers{background:var(--them-soft);color:var(--them)}
.t{font-size:12px;color:var(--faint);grid-column:1}
.txt{grid-column:2;grid-row:1/3;font-size:17px}
.partial .txt{color:var(--faint);font-style:italic}
.empty{color:var(--faint);font-size:15px;padding:20px 0}
form{display:flex;gap:10px;padding:4px 20px 14px}
form input{flex:1;font:inherit;font-size:16px;padding:10px 14px;border-radius:10px;border:1px solid var(--line);background:var(--panel);color:var(--ink)}
form input:focus{outline:2px solid var(--brand-soft);border-color:var(--brand)}
#answers{flex:1;overflow:auto;padding:0 20px 20px;display:flex;flex-direction:column;gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-left:4px solid var(--brand);border-radius:10px;padding:14px 16px}
.q{font-size:13px;color:var(--muted);margin-bottom:6px}
.hl{font-weight:650;font-size:17px;margin-bottom:8px}
.claim{margin:10px 0 0}
.claim .say{font-size:15px;color:var(--muted)}
blockquote{margin:6px 0 4px;padding:6px 12px;border-left:3px solid var(--line);background:var(--quote);font-size:16px;border-radius:0 6px 6px 0}
.cite{font-size:13px;color:var(--faint)}
.cite a{color:var(--brand);text-decoration:none}
.lock{margin-top:10px;font-size:13px;color:var(--muted)}
.note{margin-top:6px;font-size:12px;color:var(--faint)}
.thinking{color:var(--faint);font-style:italic}
.err{color:var(--live);font-size:13px}
@media (max-width:860px){main{grid-template-columns:1fr}section+section{border-left:0;border-top:1px solid var(--line)}}
</style></head><body>
<header>
  <svg class="mark" viewBox="0 0 32 32" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="2.2" stroke-linejoin="round"><rect x="3" y="3" width="26" height="26" rx="2"/><rect x="10" y="10" width="12" height="12" rx="1"/><path d="M3 3l7 7M29 3l-7 7M3 29l7-7M29 29l-7-7"/></g></svg>
  <h1>Tesseract Meeting</h1>
  <div class="state" id="state"><span class="dot"></span><span id="statetext">Not transcribing</span></div>
  <button class="primary" id="start" disabled>Start transcribing</button>
  <button id="stop" hidden>Stop</button>
</header>
<div class="banner" id="banner">
  <span><b>Tell everyone first.</b> Tesseract transcribes this call (your mic and the call's audio) and keeps the transcript on this PC.</span>
  <label><input type="checkbox" id="consent"> Everyone on the call has been told and agrees</label>
  <span class="err" id="err"></span>
</div>
<main>
  <section>
    <div class="head">Live transcript</div>
    <div id="lines"><div class="empty" id="empty">The transcript appears here once transcribing starts.</div></div>
  </section>
  <section>
    <div class="head">Ask Tesseract</div>
    <form id="ask"><input id="question" autocomplete="off" placeholder="e.g. what do we already know about what Karthik just said?"><button class="primary">Ask</button></form>
    <div id="answers"><div class="empty">Answers quote only records everyone on this call can read, and use the last few minutes of the call as context. In Slack, anyone can ask <b>@Tesseract</b> or <b>/tesseract</b> privately.</div></div>
  </section>
</main>
<script>
const $=id=>document.getElementById(id);
const el=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e};
const partials={};
function status(s){
  document.body.classList.toggle('live',!!s.live);
  $('state').classList.toggle('live',!!s.live);
  $('statetext').textContent=s.live?('Transcribing since '+s.started):'Not transcribing';
  $('start').hidden=!!s.live;$('stop').hidden=!s.live;
  $('banner').querySelector('b').textContent=s.live?'Transcribing.':'Tell everyone first.';
  const errs=Object.entries(s.errors||{}).map(([k,v])=>k+': '+v);
  $('err').textContent=!s.key?'ELEVENLABS_API_KEY is not set in .env.':errs.join(' · ');
}
function addLine(e){
  $('empty')?.remove();
  const box=$('lines'),stick=box.scrollHeight-box.scrollTop-box.clientHeight<80;
  const ln=el('div','ln');
  ln.append(el('span','who '+(e.stream||''),e.speaker),el('span','txt',e.text),el('span','t',e.at||''));
  const p=partials[e.stream];box.insertBefore(ln,p||null);
  if(stick)box.scrollTop=box.scrollHeight;
}
function partial(e){
  let p=partials[e.stream];
  if(!e.text){if(p){p.remove();delete partials[e.stream]}return}
  $('empty')?.remove();
  if(!p){p=el('div','ln partial');p.append(el('span','who '+e.stream,e.speaker),el('span','txt',''));partials[e.stream]=p;$('lines').append(p)}
  p.querySelector('.txt').textContent=e.text;
  const box=$('lines');box.scrollTop=box.scrollHeight;
}
const pending={};
function asking(e){
  $('answers').querySelector('.empty')?.remove();
  const c=el('div','card');c.append(el('div','q','“'+e.question+'”'),el('div','thinking','Reading what everyone here can see…'));
  pending[e.question]=c;$('answers').prepend(c);
}
function answer(e){
  const c=pending[e.question]||el('div','card');delete pending[e.question];
  c.replaceChildren(el('div','q','“'+e.question+'”'),el('div','hl',e.headline));
  for(const cl of e.claims){
    const d=el('div','claim');if(cl.text)d.append(el('div','say',cl.text));
    d.append(el('blockquote',null,cl.quote));
    const ci=el('div','cite','— ');const a=el('a',null,cl.title);if(cl.url){a.href=cl.url;a.target='_blank'}
    ci.append(a,document.createTextNode(' · '+[cl.where,cl.when,cl.authors.length?'by '+cl.authors.join(', '):null].filter(Boolean).join(' · ')));
    d.append(ci);c.append(d);
  }
  if(e.status!=='INFO'){
    c.append(el('div','note','Searched for '+e.searched.join(', ')));
    if(e.misses.length)c.append(el('div','note','Nearest misses, not answers: '+e.misses.map(m=>m.title).join(' · ')));
  }
  if(e.locked)c.append(el('div','lock',e.locked));
  if(e.context)c.append(el('div','note','Read with the last few minutes of the call as context. The call is never quoted as a source.'));
  if(!c.isConnected)$('answers').prepend(c);
}
function askError(e){const c=pending[e.question];if(c){c.querySelector('.thinking').replaceWith(el('div','err','Could not answer: '+e.error));delete pending[e.question]}}
const es=new EventSource('/events');
es.onmessage=m=>{const e=JSON.parse(m.data);
  ({status,line:addLine,partial,asking,answer,ask_error:askError}[e.type]||(()=>{}))(e)};
$('consent').onchange=()=>{$('start').disabled=!$('consent').checked};
async function post(path,body){const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body||{})});const j=await r.json().catch(()=>({}));if(!r.ok)$('err').textContent=j.error||('HTTP '+r.status);else if(j.live!==undefined)status(j);return j}
$('start').onclick=()=>post('/start',{consent:$('consent').checked});
$('stop').onclick=()=>post('/stop');
$('ask').onsubmit=ev=>{ev.preventDefault();const q=$('question').value.trim();if(!q)return;$('question').value='';post('/ask',{question:q})};
</script></body></html>
"""
