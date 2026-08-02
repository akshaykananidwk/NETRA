"""Speaker — sequential playback queue with mic ducking.

While audio is playing (plus a short tail) is_speaking() is True; the audio
worker drops mic frames during that window so the agent never transcribes
its own voice.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable, Optional

from core import events as ev
from core.events import EventBus

log = logging.getLogger("krishna.player")

DUCK_TAIL_SEC = 0.35


class Speaker(threading.Thread):
    def __init__(self, bus: EventBus,
                 state_provider: Optional[Callable[[], str]] = None) -> None:
        super().__init__(daemon=True, name="speaker")
        self.bus = bus
        self.state_provider = state_provider   # () -> system state string
        self._queue: "queue.Queue[Optional[tuple]]" = queue.Queue(maxsize=20)
        self._stop = threading.Event()
        self._speaking_until = 0.0
        self._mixer_ok: Optional[bool] = None  # None = not tried yet
        self.detail = ""

    # ── public ────────────────────────────────────────────────────────────
    def enqueue(self, text: str, audio_path: str, force: bool = False) -> bool:
        """Queue an utterance. Returns False if the speaker is known-dead.

        force=True plays even in paused/mute_speaker state — reserved for
        the state-change confirmations themselves."""
        if self._mixer_ok is False:
            return False
        try:
            self._queue.put_nowait((text, audio_path, force))
            return True
        except queue.Full:
            log.warning("speaker queue full — dropping utterance")
            return False

    def is_speaking(self) -> bool:
        return time.time() < self._speaking_until

    @property
    def available(self) -> bool:
        return self._mixer_ok is not False

    def stop(self) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass

    # ── thread ────────────────────────────────────────────────────────────
    def _init_mixer(self) -> bool:
        if self._mixer_ok is not None:
            return self._mixer_ok
        try:
            import pygame
            pygame.mixer.init()
            self._mixer_ok = True
            self.detail = ""
            log.info("audio output ready")
        except Exception as e:
            self._mixer_ok = False
            self.detail = str(e)
            log.error("audio output unavailable: %s", e)
            try:
                from core.db import set_health
                set_health("tts", "down", f"speaker: {e}")
            except Exception:
                pass
        return self._mixer_ok

    def run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                break
            text, audio_path, force = item
            state = self.state_provider() if self.state_provider else "active"
            if not force and state in ("paused", "mute_speaker"):
                log.info("speaker muted (%s) — skipped: %s", state, text)
                continue
            if not self._init_mixer():
                continue
            try:
                self._play(text, audio_path)
            except Exception:
                log.exception("playback failed")

    def _play(self, text: str, audio_path: str) -> None:
        import pygame
        self.bus.publish(ev.AGENT_SPEAKING, text=text)
        self._speaking_until = time.time() + 3600  # until playback ends
        try:
            pygame.mixer.music.load(audio_path)
            pygame.mixer.music.play()
            while pygame.mixer.music.get_busy() and not self._stop.is_set():
                time.sleep(0.05)
        finally:
            self._speaking_until = time.time() + DUCK_TAIL_SEC
            self.bus.publish(ev.AGENT_DONE, text=text)
