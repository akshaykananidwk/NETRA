"""Flow C — voice commands.

Trigger: the wake word "કૃષ્ણ" in a transcript (openWakeWord has no Gujarati
model, so Phase 3 detects the wake word in the Whisper text — reliable
because STT runs continuously). After the agent answers, a short follow-up
window lets the admin continue without repeating the wake word.

Speaker identity: ECAPA voice print when available; otherwise a speaker is
treated as admin if the admin's face was on camera in the last N seconds.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Optional

from config import settings
from core import events as ev

log = logging.getLogger("krishna.commander")

FOLLOW_UP_SEC = 15.0


def strip_wake_word(text: str) -> Optional[str]:
    """Returns the command with the wake word removed, or None if no wake."""
    low = text.lower()
    hit = None
    for w in settings.wake_words:
        idx = low.find(w)
        if idx >= 0 and (hit is None or idx < hit[0]):
            hit = (idx, w)
    if hit is None:
        return None
    idx, w = hit
    prefix = text[:idx].strip(" ,։:.!?-")
    suffix = text[idx + len(w):]
    # a short prefix is an address particle ("એ કૃષ્ણ...", "ok krishna...")
    if len(prefix) <= 4:
        prefix = ""
    command = (prefix + " " + suffix).strip()
    return re.sub(r"^[\s,։:.!?-]+", "", command).strip()


class Commander:
    def __init__(self, brain, greeter, state) -> None:
        self.brain = brain
        self.greeter = greeter          # reused for say()
        self.state = state
        self.orch = None                # set via attach
        self._follow_up_until = 0.0
        self._follow_up_speaker: dict = {}

    def attach(self, orchestrator) -> None:
        self.orch = orchestrator

    # ── speaker resolution ────────────────────────────────────────────────
    def resolve_speaker(self, d: dict) -> dict:
        """Voice print result (from STT) wins; admin-face fallback second."""
        if d.get("speaker_person_id"):
            return {"person_id": d["speaker_person_id"],
                    "name": d.get("speaker_name"),
                    "role": "admin" if d.get("speaker_is_admin") else
                            (d.get("speaker_role") or "staff")}
        if self.orch is not None and self.orch.admin_recently_seen():
            return {"person_id": self.orch.last_admin_person_id,
                    "name": self.orch.last_admin_name,
                    "role": "admin"}
        return {"person_id": None, "name": None, "role": "unknown"}

    # ── voice transcripts ─────────────────────────────────────────────────
    async def on_transcript(self, d: dict) -> None:
        if self.state.state == "paused":
            # only the resume command works while paused
            cmd = strip_wake_word(d.get("text", ""))
            if cmd and re.search(r"(ચાલુ\s*થા|જાગ|start)", cmd.lower()):
                self.state.set("active")
                await self.greeter.say("હું પાછો હાજર છું!")
            return

        text = d.get("text", "").strip()
        if not text:
            return
        command = strip_wake_word(text)
        speaker = None

        if command is None:
            # follow-up window: same speaker may continue without wake word
            if time.time() < self._follow_up_until:
                command = text
                speaker = self._follow_up_speaker or None
            else:
                return
        if speaker is None:
            speaker = self.resolve_speaker(d)

        if not command:
            await self.greeter.say("હા, બોલો?")
            self._open_follow_up(speaker)
            return

        await self._handle(command, speaker, channel="voice", speak=True)

    async def _handle(self, command: str, speaker: dict, channel: str,
                      speak: bool) -> dict:
        if self.orch is not None:
            await self.orch.hub.broadcast("agent.thinking",
                                          {"text": command,
                                           "speaker": speaker.get("name")})
        result = await self.brain.process(command, speaker, channel=channel)
        if self.orch is not None:
            self.orch._log_activity("🤖", f"{speaker.get('name') or 'કોઈ'}: "
                                          f"{command[:60]}")
            await self.orch.hub.broadcast(ev.AGENT_DONE + ".reply", {
                "command": command, "reply": result["reply"],
                "provider": result["provider"],
                "tool_calls": [t["name"] for t in result["tool_calls"]],
            })
        if speak:
            await self.greeter.say(result["reply"])
            self._open_follow_up(speaker)
        return result

    def _open_follow_up(self, speaker: dict) -> None:
        self._follow_up_until = time.time() + FOLLOW_UP_SEC
        self._follow_up_speaker = speaker

    # ── typed commands from the web UI (behind login ⇒ admin) ─────────────
    async def on_web_command(self, text: str) -> dict:
        speaker = {"person_id": None, "name": "સાહેબ (વેબ)", "role": "admin"}
        return await self._handle(text.strip(), speaker, channel="web",
                                  speak=False)
