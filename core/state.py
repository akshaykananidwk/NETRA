"""Global system state machine."""
from __future__ import annotations

import threading
import time

VALID_STATES = ("active", "paused", "mute_mic", "mute_speaker")


class SystemState:
    """Thread-safe holder for the global run state.

    active       — watching + (from Phase 2) listening and speaking
    paused       — vision events and audio are ignored (cameras keep streaming)
    mute_mic     — vision on, microphone ignored
    mute_speaker — everything on, no TTS output (Phase 2)
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = "active"
        self.started_at = time.time()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def set(self, state: str) -> str:
        if state not in VALID_STATES:
            raise ValueError(f"invalid state: {state}")
        with self._lock:
            self._state = state
        return state

    @property
    def vision_active(self) -> bool:
        return self.state != "paused"

    @property
    def mic_active(self) -> bool:
        # paused keeps listening so "કૃષ્ણ ચાલુ થા" can wake the agent by
        # voice (the commander ignores everything else while paused) —
        # only mute_mic truly stops the microphone
        return self.state != "mute_mic"

    @property
    def uptime_sec(self) -> int:
        return int(time.time() - self.started_at)
