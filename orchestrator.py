"""Orchestrator — consumes the event bus, owns DB writes for sightings,
maintains "who is in the office now", and relays everything to the Web UI.

Runs as a single asyncio task on the main loop. SQLite writes here are
millisecond-scale and keep the vision/audio threads completely non-blocking.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Dict, Optional

from config import settings
from core import events as ev
from core.db import (Camera, SessionLocal, SystemHealth, UnknownFace, Visit,
                     now_local, set_health)
from core.events import EventBus
from core.state import SystemState

log = logging.getLogger("krishna.orchestrator")

VISIT_DB_UPDATE_SEC = 10          # throttle per-visit last_seen writes


class Orchestrator:
    def __init__(self, bus: EventBus, hub) -> None:
        self.bus = bus
        self.hub = hub
        self.state = SystemState()
        self.activity: deque = deque(maxlen=100)
        self.mic_status = {"status": "down", "detail": "starting"}
        self.mic_level = 0
        self.camera_status: Dict[int, dict] = {}
        self.detector_ready = False
        self.detector_error: Optional[str] = None
        self.last_transcript: Optional[dict] = None
        self.greeter = None
        # (kind, id, camera_id) -> {"visit_id", "last_seen", "last_db_write", "name"}
        self._active: Dict[tuple, dict] = {}

    def attach_greeter(self, greeter) -> None:
        self.greeter = greeter
        greeter.orch = self

    # ── main loop ─────────────────────────────────────────────────────────
    async def run(self) -> None:
        log.info("orchestrator running")
        set_health("db", "ok", "")
        while True:
            evt = await self.bus.get()
            try:
                await self._handle(evt)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("orchestrator failed on %s", evt.type)

    async def _handle(self, evt) -> None:
        d = evt.data
        if evt.type == ev.CAMERA_STATUS:
            await self._on_camera_status(d)
        elif evt.type == ev.MIC_STATUS:
            if d != self.mic_status:
                self.mic_status = d
                await asyncio.to_thread(set_health, "mic", d["status"], d.get("detail", ""))
            await self.hub.broadcast(ev.MIC_STATUS, d)
        elif evt.type == ev.MIC_LEVEL:
            self.mic_level = d.get("level", 0)
            await self.hub.broadcast(ev.MIC_LEVEL, d)
        elif evt.type == ev.FACE_RECOGNIZED:
            await self._on_recognized(d)
        elif evt.type == ev.FACE_UNCERTAIN:
            await self.hub.broadcast(ev.FACE_UNCERTAIN, d)
        elif evt.type == ev.FACE_UNKNOWN:
            await self._on_unknown(d)
        elif evt.type == ev.FACE_SEEN:
            await self._on_seen(d)
        elif evt.type == ev.TRACK_LOST:
            await self._on_track_lost(d)
        elif evt.type == ev.SPEECH_TRANSCRIBED:
            await self._on_transcript(d)
        else:
            await self.hub.broadcast(evt.type, d)

    # ── handlers ──────────────────────────────────────────────────────────
    async def _on_camera_status(self, d: dict) -> None:
        cam_id = d["camera_id"]
        self.detector_ready = d.get("detector_ready", False)
        self.detector_error = d.get("detector_error")
        prev = self.camera_status.get(cam_id, {}).get("status")
        self.camera_status[cam_id] = {"status": d["status"], "detail": d.get("detail", ""),
                                      "name": d.get("camera_name", "")}

        def _persist():
            with SessionLocal() as s:
                cam = s.get(Camera, cam_id)
                if cam:
                    cam.status = "online" if d["status"] == "ok" else "offline"
                    if d["status"] == "ok":
                        cam.last_frame_at = now_local()
                    s.commit()
            set_health(f"camera_{cam_id}", d["status"], d.get("detail", ""))
            ai_status = "ok" if self.detector_ready else "down"
            set_health("ai", ai_status, self.detector_error or "")

        await asyncio.to_thread(_persist)
        if prev != d["status"]:
            await self.hub.broadcast(ev.CAMERA_STATUS, d)

    async def _on_recognized(self, d: dict) -> None:
        if not self.state.vision_active:
            return

        def _write() -> dict:
            return self._upsert_visit(kind="known", ident_id=d["person_id"],
                                      camera_id=d["camera_id"],
                                      confidence=d.get("confidence"),
                                      snapshot=d.get("snapshot_path"))
        info = await asyncio.to_thread(_write)
        if info.get("new_visit"):
            self._log_activity("👤", f"{d['name']} ઓફિસમાં આવ્યા "
                                     f"({d.get('camera_name', '')})")
        await self.hub.broadcast(ev.FACE_RECOGNIZED, {**d, **info})
        if self.greeter:
            await self.greeter.on_known({**d, **info})

    async def _on_unknown(self, d: dict) -> None:
        if not self.state.vision_active:
            return

        def _write() -> dict:
            unknown_id = None
            with SessionLocal() as s:
                row = (s.query(UnknownFace)
                        .filter(UnknownFace.temp_uid == d["temp_uid"]).first())
                if row is None and d.get("is_new") and d.get("embedding"):
                    row = UnknownFace(temp_uid=d["temp_uid"],
                                      embedding=d["embedding"],
                                      snapshot_path=d.get("snapshot_path"),
                                      first_seen=now_local(),
                                      last_seen=now_local(),
                                      status="pending")
                    s.add(row)
                    s.commit()
                elif row is not None:
                    row.last_seen = now_local()
                    if d.get("snapshot_path"):
                        row.snapshot_path = row.snapshot_path or d["snapshot_path"]
                    s.commit()
                unknown_id = row.id if row else None
            if unknown_id is None:
                return {}
            return self._upsert_visit(kind="unknown", ident_id=unknown_id,
                                      camera_id=d["camera_id"],
                                      snapshot=d.get("snapshot_path"))

        info = await asyncio.to_thread(_write)
        if info.get("visit_id"):
            self._active[("unknown_uid", d["temp_uid"], d["camera_id"])] = {
                "visit_id": info["visit_id"], "last_seen": time.time(),
                "last_db_write": time.time()}
        if d.get("is_new"):
            self._log_activity("❓", f"નવો ચહેરો દેખાયો ({d['temp_uid']})")
        await self.hub.broadcast(ev.FACE_UNKNOWN, {**d, "embedding": None, **info})
        if self.greeter:
            await self.greeter.on_unknown(d)

    async def _on_seen(self, d: dict) -> None:
        ident = d.get("identity") or {}
        kind = ident.get("kind")
        if kind == "known":
            key = ("known", ident.get("person_id"), d["camera_id"])
        elif kind == "unknown":
            key = ("unknown_uid", ident.get("temp_uid"), d["camera_id"])
        else:
            return
        entry = self._active.get(key)
        now = time.time()
        if entry:
            entry["last_seen"] = now
            if now - entry.get("last_db_write", 0) >= VISIT_DB_UPDATE_SEC:
                entry["last_db_write"] = now
                visit_id = entry.get("visit_id")
                if visit_id:
                    def _touch():
                        with SessionLocal() as s:
                            v = s.get(Visit, visit_id)
                            if v:
                                v.last_seen = now_local()
                                s.commit()
                    await asyncio.to_thread(_touch)

    async def _on_track_lost(self, d: dict) -> None:
        ident = d.get("identity") or {}
        kind = ident.get("kind")
        if kind == "known":
            key = ("known", ident.get("person_id"), d["camera_id"])
        elif kind == "unknown":
            key = ("unknown_uid", ident.get("temp_uid"), d["camera_id"])
        else:
            return
        entry = self._active.get(key)
        if entry and entry.get("visit_id"):
            visit_id = entry["visit_id"]

            def _final():
                with SessionLocal() as s:
                    v = s.get(Visit, visit_id)
                    if v:
                        v.last_seen = now_local()
                        s.commit()
            await asyncio.to_thread(_final)

    # ── visit bookkeeping (runs in thread) ────────────────────────────────
    def _upsert_visit(self, kind: str, ident_id, camera_id: int,
                      confidence=None, snapshot=None) -> dict:
        """Merge sightings within VISIT_MERGE_MINUTES into one visit row."""
        merge_window = settings.visit_merge_minutes * 60
        now_ts = time.time()

        with SessionLocal() as s:
            q = s.query(Visit).filter(Visit.camera_id == camera_id)
            if kind == "known":
                q = q.filter(Visit.person_id == ident_id)
            else:
                q = q.filter(Visit.unknown_id == ident_id)
            recent = q.order_by(Visit.last_seen.desc()).first()

            new_visit = False
            if recent is not None:
                age = (now_local() - recent.last_seen).total_seconds()
                if age <= merge_window:
                    recent.last_seen = now_local()
                    if confidence:
                        recent.confidence = confidence
                    s.commit()
                    visit_id = recent.id
                else:
                    recent = None
            if recent is None:
                v = Visit(person_id=ident_id if kind == "known" else None,
                          unknown_id=ident_id if kind == "unknown" else None,
                          camera_id=camera_id, first_seen=now_local(),
                          last_seen=now_local(), confidence=confidence,
                          snapshot_path=snapshot,
                          status="known" if kind == "known" else "unknown")
                s.add(v)
                s.commit()
                visit_id = v.id
                new_visit = True

        if kind == "known":
            self._active[("known", ident_id, camera_id)] = {
                "visit_id": visit_id, "last_seen": now_ts,
                "last_db_write": now_ts}
        return {"visit_id": visit_id, "new_visit": new_visit}

    async def _on_transcript(self, d: dict) -> None:
        self.last_transcript = {"text": d.get("text"),
                                "confidence": d.get("confidence"),
                                "time": now_local().strftime("%H:%M")}
        await self.hub.broadcast(ev.SPEECH_TRANSCRIBED, d)
        if self.greeter:
            await self.greeter.on_transcript(d)

    # ── UI helpers ────────────────────────────────────────────────────────
    def _log_activity(self, icon: str, text: str) -> None:
        self.activity.appendleft({"icon": icon, "text": text,
                                  "time": now_local().strftime("%H:%M")})

    def snapshot(self) -> dict:
        """Initial state pushed to a newly connected WebSocket client."""
        return {
            "state": self.state.state,
            "uptime_sec": self.state.uptime_sec,
            "mic": self.mic_status,
            "cameras": self.camera_status,
            "detector_ready": self.detector_ready,
            "detector_error": self.detector_error,
            "last_transcript": self.last_transcript,
            "activity": list(self.activity),
        }
