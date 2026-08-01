"""Greeting flows (Phase 2).

Flow B — known person:  greet by call-name, at most once per cooldown window
per camera (persisted via visits.greeted, so restarts don't re-greet).

Flow A — unknown person: welcome + ask their name (max N times per day),
listen for the reply, save what STT heard to unknown_faces.spoken_name so the
dashboard card comes pre-filled for the admin to confirm.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Optional

from config import settings
from core import events as ev
from core.db import (Camera, Person, SessionLocal, Setting, UnknownFace,
                     Visit, now_local, today_str)
from agent.names import extract_name, with_honorific

log = logging.getLogger("krishna.greeter")

WELCOME_UNKNOWN = ("નમસ્તે. હું કૃષ્ણ છું, AK Computer ની ઓફિસમાં આપનું સ્વાગત છે. "
                   "માફ કરશો, હું આપને ઓળખી શક્યો નથી. આપનું નામ જણાવશો?")
ASK_RESET_KEY = "unknown_ask_reset_date"


class Greeter:
    def __init__(self, tts, speaker, state, audio_worker) -> None:
        self.tts = tts
        self.speaker = speaker
        self.state = state
        self.audio = audio_worker
        self.orch = None                 # set by orchestrator.attach_greeter
        self.awaiting: Optional[dict] = None   # {"temp_uid", "camera_id"}
        self._timeout_task: Optional[asyncio.Task] = None

    # ── speaking helper ───────────────────────────────────────────────────
    async def say(self, text: str) -> bool:
        if self.state.state in ("paused", "mute_speaker"):
            return False
        path = await self.tts.synth(text)
        if path is None:
            log.warning("TTS failed (%s) — skipped: %s", self.tts.last_error, text)
            try:
                from core.db import set_health
                await asyncio.to_thread(set_health, "tts", "degraded",
                                        self.tts.last_error or "synth failed")
            except Exception:
                pass
            return False
        try:
            from core.db import set_health
            await asyncio.to_thread(set_health, "tts", "ok",
                                    self.tts.last_engine or "")
        except Exception:
            pass
        return self.speaker.enqueue(text, path)

    # ── Flow B: known face ────────────────────────────────────────────────
    async def on_known(self, d: dict) -> None:
        if self.state.state != "active":
            return

        def _check():
            with SessionLocal() as s:
                person = s.get(Person, d["person_id"])
                cam = s.get(Camera, d["camera_id"])
                if not person or not person.greeting_enabled:
                    return None
                if person.relation_type == "blacklist":
                    return None
                if cam is not None and not cam.greet_here:
                    return None
                cutoff = now_local() - timedelta(
                    hours=settings.greet_cooldown_hours)
                recent = (s.query(Visit)
                           .filter(Visit.person_id == person.id,
                                   Visit.camera_id == d["camera_id"],
                                   Visit.greeted == 1,
                                   Visit.last_seen >= cutoff).first())
                if recent is not None:
                    return None
                name = person.name_gu or person.call_name or person.full_name
                return {"name": name}

        info = await asyncio.to_thread(_check)
        if info is None:
            return

        spoken = await self.say(f"નમસ્તે {info['name']}, આપનું સ્વાગત છે.")
        if not spoken:
            return

        def _mark():
            with SessionLocal() as s:
                v = s.get(Visit, d.get("visit_id")) if d.get("visit_id") else None
                if v is not None:
                    v.greeted = 1
                    s.commit()
        await asyncio.to_thread(_mark)
        if self.orch:
            self.orch._log_activity("🙏", f"{info['name']} નું સ્વાગત કર્યું")
            await self.orch.hub.broadcast(ev.GREETING_SPOKEN,
                                          {"person_id": d["person_id"],
                                           "name": info["name"]})

    # ── Flow A: unknown face ──────────────────────────────────────────────
    async def on_unknown(self, d: dict) -> None:
        if self.state.state != "active" or self.awaiting is not None:
            return

        def _check():
            with SessionLocal() as s:
                # daily ask-count reset (persisted so restarts behave)
                marker = s.get(Setting, ASK_RESET_KEY)
                today = today_str()
                if marker is None or marker.value != today:
                    s.query(UnknownFace).update({UnknownFace.ask_count: 0})
                    if marker is None:
                        s.add(Setting(key=ASK_RESET_KEY, value=today))
                    else:
                        marker.value = today
                    s.commit()
                row = (s.query(UnknownFace)
                        .filter(UnknownFace.temp_uid == d["temp_uid"]).first())
                if row is None or row.status != "pending":
                    return False
                if row.ask_count >= settings.unknown_ask_max_per_day:
                    return False
                cam = s.get(Camera, d["camera_id"])
                if cam is not None and not cam.greet_here:
                    return False
                return True

        if not await asyncio.to_thread(_check):
            return

        spoken = await self.say(WELCOME_UNKNOWN)
        if not spoken:
            return

        def _bump():
            with SessionLocal() as s:
                row = (s.query(UnknownFace)
                        .filter(UnknownFace.temp_uid == d["temp_uid"]).first())
                if row is not None:
                    row.ask_count = (row.ask_count or 0) + 1
                    s.commit()
        await asyncio.to_thread(_bump)

        self.awaiting = {"temp_uid": d["temp_uid"], "camera_id": d["camera_id"]}
        if self.audio is not None:
            self.audio.open_reply_window(settings.reply_timeout_sec + 4,
                                         context={"purpose": "name_reply",
                                                  "temp_uid": d["temp_uid"]})
        if self._timeout_task:
            self._timeout_task.cancel()
        self._timeout_task = asyncio.create_task(
            self._reply_timeout(d["temp_uid"]))
        if self.orch:
            self.orch._log_activity("❓", f"{d['temp_uid']} ને નામ પૂછ્યું")

    async def _reply_timeout(self, temp_uid: str) -> None:
        try:
            await asyncio.sleep(settings.reply_timeout_sec)
        except asyncio.CancelledError:
            return
        if self.awaiting and self.awaiting.get("temp_uid") == temp_uid:
            self.awaiting = None
            if self.audio is not None:
                self.audio.close_reply_window()
            log.info("no reply from %s — logged silently", temp_uid)
            if self.orch:
                self.orch._log_activity("🤐", f"{temp_uid} એ જવાબ ન આપ્યો")

    # ── reply handling ────────────────────────────────────────────────────
    async def on_transcript(self, d: dict) -> None:
        if self.awaiting is None:
            return
        temp_uid = self.awaiting["temp_uid"]
        self.awaiting = None
        if self._timeout_task:
            self._timeout_task.cancel()
            self._timeout_task = None
        if self.audio is not None:
            self.audio.close_reply_window()

        name = extract_name(d.get("text", ""))
        if not name:
            if self.orch:
                self.orch._log_activity("🤔", f"{temp_uid} નો જવાબ સમજાયો નહીં")
            return

        def _save():
            with SessionLocal() as s:
                row = (s.query(UnknownFace)
                        .filter(UnknownFace.temp_uid == temp_uid).first())
                if row is not None:
                    row.spoken_name = name
                    s.commit()
                    return row.to_dict()
            return None
        row = await asyncio.to_thread(_save)

        await self.say(f"આભાર {with_honorific(name)}. બેસો, "
                       "હું સાહેબને જાણ કરું છું.")
        if self.orch:
            self.orch._log_activity("📝", f"{temp_uid} એ નામ કહ્યું: {name}")
            if row:
                await self.orch.hub.broadcast(ev.UNKNOWN_UPDATED, row)
