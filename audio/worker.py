"""Audio worker — mic frames → health badges → VAD → STT queue.

Mic ducking: while the Speaker is playing (plus a tail), frames are dropped
and the VAD state resets, so the agent never hears itself.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

from config import settings
from core import events as ev
from core.events import EventBus
from audio.mic import MicStream
from audio.stt import STTWorker
from audio.vad import VadSegmenter

log = logging.getLogger("krishna.audio")

REPORT_INTERVAL = 2.0


class AudioWorker(threading.Thread):
    def __init__(self, bus: EventBus, stt: STTWorker,
                 is_ducked: Callable[[], bool],
                 mic_active: Callable[[], bool]) -> None:
        super().__init__(daemon=True, name="audio-worker")
        self.bus = bus
        self.stt = stt
        self.is_ducked = is_ducked
        self.mic_active = mic_active
        self.mic = MicStream()
        self.vad = VadSegmenter(sample_rate=settings.sample_rate)
        self._stop = threading.Event()
        self._reply_until = 0.0
        self._reply_context: dict = {}
        self._lock = threading.Lock()
        self._record_buf: list = []
        self._record_until = 0.0

    # ── called by the Greeter (any thread) ────────────────────────────────
    def open_reply_window(self, seconds: float, context: Optional[dict] = None) -> None:
        with self._lock:
            self._reply_until = time.time() + seconds
            self._reply_context = context or {}

    def close_reply_window(self) -> None:
        with self._lock:
            self._reply_until = 0.0
            self._reply_context = {}

    def _reply_context_if_open(self) -> Optional[dict]:
        with self._lock:
            if time.time() < self._reply_until:
                return dict(self._reply_context)
        return None

    def record_seconds(self, seconds: float) -> bytes:
        """Blocking capture of raw PCM (voice-print enrollment). Bypasses VAD."""
        with self._lock:
            self._record_buf = []
            self._record_until = time.time() + seconds
        deadline = time.time() + seconds + 2.0
        while time.time() < deadline:
            with self._lock:
                if time.time() >= self._record_until:
                    break
            time.sleep(0.1)
        with self._lock:
            data = b"".join(self._record_buf)
            self._record_buf = []
            self._record_until = 0.0
        return data

    def stop(self) -> None:
        self._stop.set()
        self.mic.stop()

    # ── thread ────────────────────────────────────────────────────────────
    def run(self) -> None:
        self.mic.start()
        log.info("audio worker running (vad backend: %s)", self.vad.backend)
        last_report = 0.0
        while not self._stop.is_set():
            frame = self.mic.read(timeout=0.5)
            now = time.time()

            if now - last_report >= REPORT_INTERVAL:
                last_report = now
                self.bus.publish(ev.MIC_STATUS, status=self.mic.status,
                                 detail=self.mic.detail)
                self.bus.publish(ev.MIC_LEVEL, rms=round(self.mic.rms, 1),
                                 level=self.mic.level_pct)

            if frame is None:
                continue
            with self._lock:
                recording = time.time() < self._record_until
                if recording:
                    self._record_buf.append(frame)
            if recording:
                continue                      # enrollment capture, skip VAD
            if self.is_ducked() or not self.mic_active():
                self.vad.reset()
                continue

            segment = self.vad.feed(frame)
            if segment is None:
                continue
            reply_ctx = self._reply_context_if_open()
            if reply_ctx is None and not settings.stt_always_on:
                continue                      # nobody is waiting for speech
            self.stt.submit(segment, context=reply_ctx or {})
