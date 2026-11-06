"""
audio.py - the call's sound on Windows, as two mono PCM16 streams.

    mic       the host's microphone: their own voice
    speakers  what the PC plays (WASAPI loopback): everyone else on the call

Two streams, not one mixdown, because the live transcription model cannot tell voices apart:
which stream a line came from is the only speaker label there is. The host should wear
headphones, or the mic hears the speakers too and every remote line is transcribed twice.

Loopback hears everything the PC plays, not only Zoom, so nothing else should be playing. WASAPI
loopback also delivers nothing while the PC is silent, which costs nothing either: the
transcription is billed per second sent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np

CHUNK_SECONDS = 0.1
RATES = (8000, 16000, 22050, 24000, 44100, 48000)     # what the transcription service accepts


@dataclass
class Source:
    name: str             # "mic" | "speakers"
    device: str
    rate: int


class Capture:
    """Opens both streams and hands each 100 ms of mono PCM16 to on_chunk(source, bytes)."""

    def __init__(self, on_chunk: Callable[[str, bytes], None]):
        import pyaudiowpatch as pa

        self.pa = pa
        self.audio = pa.PyAudio()
        self.on_chunk = on_chunk
        self.streams = []
        wasapi = self.audio.get_host_api_info_by_type(pa.paWASAPI)
        speakers = self.audio.get_device_info_by_index(wasapi["defaultOutputDevice"])
        loopback = next((d for d in self.audio.get_loopback_device_info_generator()
                         if speakers["name"] in d["name"]), None)
        if loopback is None:
            raise RuntimeError(f"no loopback device for {speakers['name']}")
        mic = self.audio.get_device_info_by_index(wasapi["defaultInputDevice"])
        self.devices = {"mic": mic, "speakers": loopback}
        for d in self.devices.values():
            if int(d["defaultSampleRate"]) not in RATES:
                raise RuntimeError(f"{d['name']} runs at {int(d['defaultSampleRate'])} Hz; set it to 48000 in Sound settings")

    @property
    def sources(self) -> list[Source]:
        return [Source(n, d["name"], int(d["defaultSampleRate"])) for n, d in self.devices.items()]

    def start(self) -> None:
        for name, d in self.devices.items():
            rate, channels = int(d["defaultSampleRate"]), max(1, int(d["maxInputChannels"]))
            self.streams.append(self.audio.open(
                format=self.pa.paInt16, channels=channels, rate=rate, input=True,
                input_device_index=d["index"], frames_per_buffer=int(rate * CHUNK_SECONDS),
                stream_callback=self._callback(name, channels)))

    def _callback(self, name: str, channels: int):
        def cb(data, frames, time_info, status):
            samples = np.frombuffer(data, dtype=np.int16)
            if channels > 1:
                samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)
            self.on_chunk(name, samples.tobytes())
            return (None, self.pa.paContinue)
        return cb

    def stop(self) -> None:
        for s in self.streams:
            try:
                s.stop_stream()
                s.close()
            except Exception:
                pass
        self.streams = []
        self.audio.terminate()
