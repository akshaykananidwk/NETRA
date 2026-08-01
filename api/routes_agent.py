"""Agent command box + WhatsApp send + voice-print enrollment."""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger("krishna.api.agent")
router = APIRouter(prefix="/api")


class CommandIn(BaseModel):
    text: str = Field(min_length=1, max_length=1000)


class WaSendIn(BaseModel):
    to: str = Field(min_length=6)
    message: str = Field(min_length=1)


@router.post("/agent/command")
async def agent_command(body: CommandIn):
    """Typed command — the web UI sits behind the admin login."""
    from core.runtime import commander
    return await commander.on_web_command(body.text)


@router.post("/whatsapp/send")
async def whatsapp_send(body: WaSendIn):
    from core.runtime import whatsapp
    if not whatsapp.configured:
        raise HTTPException(400, "WhatsApp .env માં configure નથી")
    ok = await asyncio.to_thread(whatsapp.send_text, body.to, body.message,
                                 "manual")
    return {"sent": ok, "queued": not ok}


@router.post("/persons/{person_id}/voice")
async def enroll_voice(person_id: int, seconds: int = 15):
    """Record N seconds from the office mic → ECAPA voice print."""
    from core.db import Person, SessionLocal
    from core.runtime import audio_worker, speaker_id
    with SessionLocal() as s:
        if s.get(Person, person_id) is None:
            raise HTTPException(404, "person not found")
    if not speaker_id.ready and not await asyncio.to_thread(speaker_id.load):
        raise HTTPException(
            501, "Voice-print માટે speechbrain install કરો: "
                 f"pip install speechbrain torch ({speaker_id.error})")
    if audio_worker.mic.status == "down":
        raise HTTPException(503, "માઇક કનેક્ટ નથી")
    seconds = max(5, min(seconds, 30))
    pcm = await asyncio.to_thread(audio_worker.record_seconds, seconds)
    if len(pcm) < 16000:
        raise HTTPException(503, "માઇકમાંથી અવાજ ન મળ્યો")
    ok = await asyncio.to_thread(speaker_id.enroll, person_id, pcm)
    if not ok:
        raise HTTPException(500, "voice print બની શકી નહીં")
    return {"enrolled": person_id, "seconds": seconds}
