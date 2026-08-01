"""Voice-activity segmentation.

Primary backend is webrtcvad; if it's unavailable an int16 energy gate is
used so the pipeline still works. Feed 30 ms frames; a completed speech
segment (with ~300 ms pre-roll, closed by 300 ms trailing silence) is
returned as raw PCM bytes.
"""
from __future__ import annotations

import collections
import logging
from typing import Optional

import numpy as np

from config import settings

log = logging.getLogger("krishna.vad")

ENERGY_RMS_THRESHOLD = 500      # int16 RMS — fallback gate only


class VadSegmenter:
    def __init__(self, sample_rate: int = 16000, frame_ms: int = 30,
                 silence_ms: int = 300, min_speech_ms: int = 400,
                 max_segment_sec: float = 15.0, prebuffer_ms: int = 300,
                 use_webrtc: bool = True) -> None:
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.silence_frames = max(silence_ms // frame_ms, 1)
        self.min_speech_frames = max(min_speech_ms // frame_ms, 1)
        self.max_frames = int(max_segment_sec * 1000 // frame_ms)
        self._ring: collections.deque = collections.deque(
            maxlen=max(prebuffer_ms // frame_ms, 1))
        self._vad = None
        if use_webrtc:
            try:
                import webrtcvad
                self._vad = webrtcvad.Vad(settings.vad_aggressiveness)
            except Exception as e:
                log.warning("webrtcvad unavailable (%s) — using energy gate", e)
        self.backend = "webrtc" if self._vad else "energy"
        self._triggered = False
        self._voiced: list = []
        self._trailing_silence = 0

    def _is_speech(self, frame: bytes) -> bool:
        if self._vad is not None:
            try:
                return self._vad.is_speech(frame, self.sample_rate)
            except Exception:
                return False
        arr = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        return float(np.sqrt(np.mean(arr ** 2))) > ENERGY_RMS_THRESHOLD

    def reset(self) -> None:
        self._ring.clear()
        self._triggered = False
        self._voiced = []
        self._trailing_silence = 0

    def feed(self, frame: bytes) -> Optional[bytes]:
        """Push one frame; returns a finished segment's PCM or None."""
        speech = self._is_speech(frame)

        if not self._triggered:
            self._ring.append((frame, speech))
            voiced = sum(1 for _, s in self._ring if s)
            if self._ring.maxlen and len(self._ring) == self._ring.maxlen \
                    and voiced >= 0.6 * self._ring.maxlen:
                self._triggered = True
                self._voiced = [f for f, _ in self._ring]
                self._ring.clear()
                self._trailing_silence = 0
            return None

        self._voiced.append(frame)
        self._trailing_silence = 0 if speech else self._trailing_silence + 1

        if (self._trailing_silence >= self.silence_frames
                or len(self._voiced) >= self.max_frames):
            segment = b"".join(self._voiced)
            speech_frames = len(self._voiced) - self._trailing_silence
            self.reset()
            if speech_frames >= self.min_speech_frames:
                return segment
        return None
