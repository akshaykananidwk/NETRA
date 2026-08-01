"""Local speech-to-text — faster-whisper, loaded once and kept warm.

The initial prompt is seeded with office vocabulary and every registered
person's name, which measurably improves Gujarati name recognition.
"""
from __future__ import annotations

import logging
import math
import queue
import threading
import time
from typing import Optional

import numpy as np

from config import settings
from core import events as ev
from core.events import EventBus

log = logging.getLogger("krishna.stt")

OFFICE_WORDS = "ઓફિસ, ચા, કોફી, પાણી, મીટિંગ, કલેક્શન, ટાસ્ક, રિમાઇન્ડર, નમસ્તે, કૃષ્ણ"
PROMPT_TTL_SEC = 120


class STTWorker(threading.Thread):
    def __init__(self, bus: EventBus, speaker_id=None) -> None:
        super().__init__(daemon=True, name="stt-worker")
        self.bus = bus
        self.speaker_id = speaker_id
        self._queue: "queue.Queue[Optional[dict]]" = queue.Queue(maxsize=8)
        self._stop = threading.Event()
        self._model = None
        self.ready = False
        self.error: Optional[str] = None
        self._prompt = OFFICE_WORDS
        self._prompt_at = 0.0

    # ── public (called from audio worker thread) ──────────────────────────
    def submit(self, pcm: bytes, context: Optional[dict] = None) -> None:
        item = {"pcm": pcm, "context": context or {}, "ts": time.time()}
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            try:                      # drop oldest so fresh speech wins
                self._queue.get_nowait()
                self._queue.put_nowait(item)
            except queue.Empty:
                pass

    def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    # ── thread ────────────────────────────────────────────────────────────
    def _load(self) -> bool:
        from core.db import set_health
        try:
            set_health("stt", "degraded", f"loading {settings.whisper_model}…")
            from faster_whisper import WhisperModel
            device = "cuda" if settings.use_gpu else "cpu"
            compute = "float16" if settings.use_gpu else "int8"
            self._model = WhisperModel(settings.whisper_model, device=device,
                                       compute_type=compute,
                                       download_root=str(settings.models_dir))
            self.ready = True
            set_health("stt", "ok", f"{settings.whisper_model} ({device})")
            log.info("whisper %s ready on %s", settings.whisper_model, device)
            return True
        except Exception as e:
            self.error = str(e)
            set_health("stt", "down", str(e))
            log.error("whisper load failed: %s", e)
            return False

    def _initial_prompt(self) -> str:
        if time.time() - self._prompt_at < PROMPT_TTL_SEC:
            return self._prompt
        self._prompt_at = time.time()
        try:
            from core.db import Person, SessionLocal
            with SessionLocal() as s:
                names = [n for (n,) in s.query(Person.full_name).limit(100).all()]
                gu = [n for (n,) in s.query(Person.name_gu)
                      .filter(Person.name_gu.isnot(None)).limit(100).all()]
            self._prompt = OFFICE_WORDS + ", " + ", ".join(names + gu)
        except Exception:
            self._prompt = OFFICE_WORDS
        return self._prompt

    def run(self) -> None:
        if not self._load():
            # keep draining so the queue never blocks producers
            while not self._stop.is_set():
                try:
                    self._queue.get(timeout=1.0)
                except queue.Empty:
                    pass
            return

        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                break
            try:
                self._transcribe(item)
            except Exception:
                log.exception("transcription failed")

    def _transcribe(self, item: dict) -> None:
        pcm = item["pcm"]
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        duration = len(audio) / settings.sample_rate
        t0 = time.time()
        segments, _info = self._model.transcribe(
            audio, language=settings.whisper_language, beam_size=1,
            initial_prompt=self._initial_prompt(), vad_filter=False)
        texts, logprobs = [], []
        for seg in segments:
            if seg.no_speech_prob is not None and seg.no_speech_prob > 0.8:
                continue
            texts.append(seg.text.strip())
            if seg.avg_logprob is not None:
                logprobs.append(seg.avg_logprob)
        text = " ".join(t for t in texts if t).strip()
        if not text:
            return
        confidence = (round(min(math.exp(sum(logprobs) / len(logprobs)), 1.0), 3)
                      if logprobs else None)
        log.info("heard (%.1fs, %.0fms): %s", duration,
                 (time.time() - t0) * 1000, text)

        speaker_fields = {}
        if self.speaker_id is not None and self.speaker_id.ready:
            try:
                match = self.speaker_id.identify(pcm)
                if match:
                    pid, name, is_admin, score = match
                    speaker_fields = {"speaker_person_id": pid,
                                      "speaker_name": name,
                                      "speaker_is_admin": is_admin,
                                      "speaker_score": round(score, 3)}
            except Exception:
                log.exception("speaker identify failed")

        self.bus.publish(ev.SPEECH_TRANSCRIBED, text=text,
                         confidence=confidence, duration_sec=round(duration, 2),
                         context=item["context"], **speaker_fields)
