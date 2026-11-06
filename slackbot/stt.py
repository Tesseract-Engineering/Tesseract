"""
stt.py - one live ElevenLabs Scribe v2 Realtime session per audio stream.

    wss://api.elevenlabs.io/v1/speech-to-text/realtime   xi-api-key header
    -> input_audio_chunk {audio_base_64, commit, sample_rate}
    <- partial_transcript {text}      words still settling: shown greyed, replaced
    <- committed_transcript {text}    a finished segment: becomes a transcript line

The service decides where a segment ends (commit_strategy=vad). Realtime has no speaker labels;
the stream a session listens to is the label (audio.py says why). A dropped connection is
reopened, and audio that arrived meanwhile is kept, up to a few seconds.
"""

from __future__ import annotations

import base64
import json
import logging
import queue
import threading
import time
from typing import Callable
from urllib.parse import urlencode

log = logging.getLogger("slackbot.stt")

URL = "wss://api.elevenlabs.io/v1/speech-to-text/realtime"
BACKLOG_CHUNKS = 50          # 5 s of 100 ms chunks kept while reconnecting


class Session:
    def __init__(self, name: str, rate: int, api_key: str, on_text: Callable[[str, str, bool], None],
                 language: str = "en"):
        self.name, self.rate, self.api_key, self.on_text = name, rate, api_key, on_text
        self.language = language
        self.chunks: queue.Queue[bytes | None] = queue.Queue(maxsize=BACKLOG_CHUNKS)
        self.stopped = threading.Event()
        self.thread = threading.Thread(target=self._run, name=f"stt-{name}", daemon=True)
        self.error: str | None = None

    def start(self) -> None:
        self.thread.start()

    def feed(self, pcm: bytes) -> None:
        try:
            self.chunks.put_nowait(pcm)
        except queue.Full:          # still reconnecting: drop the oldest audio, keep the newest
            try:
                self.chunks.get_nowait()
                self.chunks.put_nowait(pcm)
            except queue.Empty:
                pass

    def stop(self) -> None:
        self.stopped.set()
        self.feed(b"")

    def url(self) -> str:
        return URL + "?" + urlencode({"model_id": "scribe_v2_realtime", "audio_format": f"pcm_{self.rate}",
                                      "commit_strategy": "vad", "language_code": self.language})

    def _run(self) -> None:
        from websockets.sync.client import connect

        delay = 1.0
        while not self.stopped.is_set():
            try:
                with connect(self.url(), additional_headers={"xi-api-key": self.api_key},
                             open_timeout=10, max_size=2**22) as ws:
                    delay = 1.0
                    self.error = None
                    sender = threading.Thread(target=self._send, args=(ws,), daemon=True)
                    sender.start()
                    for raw in ws:
                        self._message(json.loads(raw))
                        if self.stopped.is_set():
                            break
            except Exception as e:     # network, auth, session limits: say so on the page and retry
                if self.stopped.is_set():
                    break
                self.error = f"{type(e).__name__}: {e}"[:200]
                log.warning("stt %s: %s; reconnecting in %.0fs", self.name, self.error, delay)
                self.on_text(self.name, "", False)
                time.sleep(delay)
                delay = min(delay * 2, 30)

    def _send(self, ws) -> None:
        while not self.stopped.is_set():
            pcm = self.chunks.get()
            if not pcm:
                break
            try:
                ws.send(json.dumps({"message_type": "input_audio_chunk", "commit": False, "sample_rate": self.rate,
                                    "audio_base_64": base64.b64encode(pcm).decode()}))
            except Exception:
                return              # the receiving loop sees the close and reconnects
        try:
            ws.close()
        except Exception:
            pass

    def _message(self, m: dict) -> None:
        kind = m.get("message_type")
        if kind == "partial_transcript":
            self.on_text(self.name, m.get("text") or "", False)
        elif kind == "committed_transcript":
            self.on_text(self.name, m.get("text") or "", True)
        elif kind == "session_started":
            log.info("stt %s: session %s", self.name, m.get("session_id"))
        elif kind and kind not in ("committed_transcript_with_timestamps", "committed_transcript_entities"):
            if "error" in kind or kind in ("auth_error", "quota_exceeded", "rate_limited", "unaccepted_terms"):
                self.error = f"{kind}: {m.get('error') or m.get('warning') or ''}"[:200]
                log.warning("stt %s: %s", self.name, self.error)
