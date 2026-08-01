"""Shared singletons wired together at startup (imported by main.py and routes).

Import order matters: this module must not import api.routes_* (they import us).
"""
from __future__ import annotations

from api.ws import WSHub
from audio.stt import STTWorker
from audio.worker import AudioWorker
from core.events import EventBus
from orchestrator import Orchestrator
from speech.player import Speaker
from speech.tts import TTSEngine
from vision.detector import FaceDetector
from vision.matcher import FaceMatcher, UnknownRegistry
from vision.worker import VisionManager
from agent.greeter import Greeter

bus = EventBus()
hub = WSHub()
detector = FaceDetector()
matcher = FaceMatcher()
unknowns = UnknownRegistry()
orchestrator = Orchestrator(bus, hub)
vision_manager = VisionManager(
    bus, detector, matcher, unknowns,
    state_provider=lambda: orchestrator.state.vision_active,
)

tts = TTSEngine()
speaker = Speaker(bus, state_provider=lambda: orchestrator.state.state)
stt_worker = STTWorker(bus)
audio_worker = AudioWorker(
    bus, stt_worker,
    is_ducked=speaker.is_speaking,
    mic_active=lambda: orchestrator.state.mic_active,
)
greeter = Greeter(tts, speaker, orchestrator.state, audio_worker)
orchestrator.attach_greeter(greeter)

from core.updater import UpdateManager  # noqa: E402  (imports core.db lazily)

updater = UpdateManager(bus=bus)
