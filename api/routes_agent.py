"""Agent command box + phone voice + WhatsApp send + voice-print enrollment."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import numpy as np
from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

log = logging.getLogger("krishna.api.agent")
router = APIRouter(prefix="/api")


def decode_audio_to_pcm16k(data: bytes) -> bytes:
    """Browser audio (webm/opus/ogg/mp4/wav) → raw 16 kHz mono int16 PCM.

    Uses PyAV (already installed with faster-whisper)."""
    import io

    import av
    from av.audio.resampler import AudioResampler

    container = av.open(io.BytesIO(data))
    resampler = AudioResampler(format="s16", layout="mono", rate=16000)
    chunks = []
    for frame in container.decode(audio=0):
        for out in resampler.resample(frame):
            chunks.append(bytes(out.planes[0]))
    container.close()
    return b"".join(chunks)


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


@router.post("/agent/voice")
async def agent_voice(audio: UploadFile = File(...)):
    """Phone-mic voice command: browser audio → Whisper → agent → TTS reply."""
    from agent.commander import strip_wake_word
    from core.runtime import commander, stt_worker, tts

    if not stt_worker.ready:
        raise HTTPException(
            503, "કાન (Whisper) હજી તૈયાર નથી — Settings માં stt જુઓ")
    data = await audio.read()
    if len(data) < 200:
        raise HTTPException(422, "ઓડિયો ખાલી છે")
    try:
        pcm = await asyncio.to_thread(decode_audio_to_pcm16k, data)
    except Exception as e:
        log.warning("audio decode failed: %s", str(e)[:150])
        raise HTTPException(422, "ઓડિયો સમજાયો નહીં — ફરી પ્રયત્ન કરો")
    if len(pcm) < 16000:                       # < 0.5 s
        raise HTTPException(422, "બહુ ટૂંકું બોલાયું — ફરી બોલો")

    text, confidence = await asyncio.to_thread(stt_worker.transcribe_bytes, pcm)
    if not text:
        raise HTTPException(422, "કંઈ સંભળાયું નહીં — ફરી બોલો")

    # phone button is an explicit trigger — wake word optional
    command = strip_wake_word(text)
    if command is None:
        command = text
    result = await commander.on_web_command(command)

    # reply audio so the phone speaks Krishna's answer
    audio_url = None
    try:
        path = await tts.synth(result["reply"])
        if path:
            audio_url = f"/media/tts/{Path(path).name}"
    except Exception:
        log.exception("reply tts failed")

    return {"heard": text, "confidence": confidence, **result,
            "audio_url": audio_url}


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
