"""Shared singletons wired together at startup (imported by main.py and routes).

Import order matters: this module must not import api.routes_* (they import us).
"""
from __future__ import annotations

from api.ws import WSHub
from audio.mic import MicMonitor
from core.events import EventBus
from orchestrator import Orchestrator
from vision.detector import FaceDetector
from vision.matcher import FaceMatcher, UnknownRegistry
from vision.worker import VisionManager

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
mic_monitor = MicMonitor(bus)
