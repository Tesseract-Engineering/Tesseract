"""
meeting.py - a live call: its transcript, and asking Tesseract about it.

    audio.Capture ──▶ stt.Session (mic, speakers) ──▶ Transcript ──▶ the Meeting page (page.py), live
                                                         │
                     the page's ask box ─────────────────┤──▶ answer(context = the last few minutes,
                     Slack, while the call is live ──────┘            audience = everyone on the call)

Nothing is transcribed until the host confirms everyone on the call has been told, and the page
says it is transcribing for as long as it is. The transcript is kept under data/meetings/
(git-ignored), one line per finished segment.

Who sees an answer decides what it may quote. The shared screen is seen by everyone on the call,
so its answers quote only records every participant may read (answer's `audience`). A Slack ask
is private to the asker and keeps their own access; the call is only context for it.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

log = logging.getLogger("slackbot.meeting")

CONTEXT_SECONDS = 180          # how much of the call a question is read against
CONTEXT_CHARS = 4000


@dataclass
class Line:
    at: datetime
    speaker: str
    text: str


@dataclass
class Transcript:
    labels: dict[str, str]                       # stream -> who it is ("mic" -> "Ishan Sinha")
    path: Path | None = None
    lines: list[Line] = field(default_factory=list)
    partial: dict[str, str] = field(default_factory=dict)
    listeners: list[queue.Queue] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def heard(self, stream: str, text: str, final: bool) -> None:
        speaker = self.labels.get(stream, stream)
        with self.lock:
            if final:
                self.partial.pop(stream, None)
                if text.strip():
                    line = Line(datetime.now().astimezone(), speaker, text.strip())
                    self.lines.append(line)
                    self._save(line)
                    self._emit({"type": "line", "at": line.at.strftime("%H:%M:%S"), "speaker": speaker,
                                "stream": stream, "text": line.text})
                self._emit({"type": "partial", "stream": stream, "speaker": speaker, "text": ""})
            else:
                self.partial[stream] = text
                self._emit({"type": "partial", "stream": stream, "speaker": speaker, "text": text})

    def recent(self, seconds: int = CONTEXT_SECONDS) -> str | None:
        """The last few minutes as 'Name: text' lines, for a question to be read against."""
        now = datetime.now().astimezone()
        with self.lock:
            lines = [f"{ln.speaker}: {ln.text}" for ln in self.lines if (now - ln.at).total_seconds() <= seconds]
        return "\n".join(lines)[-CONTEXT_CHARS:] or None

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self.lock:
            self.listeners.append(q)
            for ln in self.lines[-200:]:                 # a page opened mid-call catches up
                q.put_nowait({"type": "line", "at": ln.at.strftime("%H:%M:%S"), "speaker": ln.speaker,
                              "stream": next((s for s, n in self.labels.items() if n == ln.speaker), ""),
                              "text": ln.text})
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.listeners:
                self.listeners.remove(q)

    def emit(self, event: dict) -> None:
        with self.lock:
            self._emit(event)

    def _emit(self, event: dict) -> None:
        for q in list(self.listeners):
            try:
                q.put_nowait(event)
            except queue.Full:
                pass

    def _save(self, line: Line) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"at": line.at.isoformat(), "speaker": line.speaker, "text": line.text}) + "\n")


class Meeting:
    """Owns the capture and the two transcription sessions; started and stopped from the page."""

    def __init__(self, host_name: str, folder: str | Path = "data/meetings"):
        self.host_name = host_name
        self.folder = Path(folder)
        self.transcript = Transcript(labels={"mic": host_name, "speakers": "Others"})
        self.capture = None
        self.sessions: dict = {}
        self.started_at: datetime | None = None
        self.lock = threading.Lock()

    @property
    def live(self) -> bool:
        return self.started_at is not None

    def status(self) -> dict:
        return {"live": self.live, "started": self.started_at.strftime("%H:%M") if self.started_at else None,
                "errors": {n: s.error for n, s in self.sessions.items() if s.error},
                "key": bool(os.environ.get("ELEVENLABS_API_KEY")), "host": self.host_name}

    def start(self) -> None:
        from .audio import Capture
        from .stt import Session

        with self.lock:
            if self.live:
                return
            key = os.environ.get("ELEVENLABS_API_KEY")
            if not key:
                raise RuntimeError("ELEVENLABS_API_KEY is not set in .env")
            self.started_at = datetime.now().astimezone()
            self.transcript.path = self.folder / f"{self.started_at:%Y-%m-%d_%H%M}.jsonl"
            self.capture = Capture(self._chunk)
            self.sessions = {s.name: Session(s.name, s.rate, key, self.transcript.heard)
                             for s in self.capture.sources}
            for s in self.sessions.values():
                s.start()
            self.capture.start()
            log.info("meeting started; transcript at %s", self.transcript.path)
        self.transcript.emit({"type": "status", **self.status()})

    def _chunk(self, stream: str, pcm: bytes) -> None:
        s = self.sessions.get(stream)
        if s:
            s.feed(pcm)

    def stop(self) -> None:
        with self.lock:
            if not self.live:
                return
            if self.capture:
                self.capture.stop()
            for s in self.sessions.values():
                s.stop()
            self.capture, self.started_at = None, None
            log.info("meeting stopped")
        self.transcript.emit({"type": "status", **self.status()})
