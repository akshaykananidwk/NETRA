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

from audio.speaker_id import SpeakerID  # noqa: E402

speaker_id = SpeakerID()
stt_worker = STTWorker(bus, speaker_id=speaker_id)
audio_worker = AudioWorker(
    bus, stt_worker,
    is_ducked=speaker.is_speaking,
    mic_active=lambda: orchestrator.state.mic_active,
)
greeter = Greeter(tts, speaker, orchestrator.state, audio_worker)
orchestrator.attach_greeter(greeter)

from core.updater import UpdateManager  # noqa: E402  (imports core.db lazily)

updater = UpdateManager(bus=bus)

# ── Phase 3: brain + actions ─────────────────────────────────────────────
from actions.reminders import ReminderEngine  # noqa: E402
from actions.whatsapp import WhatsAppClient  # noqa: E402
from agent.brain import AgentBrain  # noqa: E402
from agent.commander import Commander  # noqa: E402
from agent.memory import ConversationMemory  # noqa: E402
from agent.tools import ToolExecutor  # noqa: E402

whatsapp = WhatsAppClient()
reminders = ReminderEngine(
    whatsapp=whatsapp, bus=bus,
    say_callback=lambda text: bus.run_coroutine(greeter.say(text)),
)
tool_executor = ToolExecutor(whatsapp=whatsapp, reminders=reminders,
                             state=orchestrator.state)
brain = AgentBrain(tool_executor, ConversationMemory(),
                   orchestrator=orchestrator)
commander = Commander(brain, greeter, orchestrator.state)
orchestrator.attach_commander(commander)

from agent.meeting import MeetingManager  # noqa: E402

meetings = MeetingManager(audio_worker=audio_worker, brain=brain,
                          whatsapp=whatsapp, reminders=reminders,
                          greeter=greeter, bus=bus)
orchestrator.attach_meetings(meetings)
tool_executor.meetings = meetings
