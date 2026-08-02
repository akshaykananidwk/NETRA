"""Meetings REST API — list, detail, transcript, start/end."""
from __future__ import annotations

import json
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.db import (Meeting, MeetingParticipant, Person, SessionLocal,
                     TranscriptSegment)

router = APIRouter(prefix="/api")


def _meeting_dict(m: Meeting, s=None) -> dict:
    d = {
        "id": m.id, "title": m.title,
        "started_at": str(m.started_at or ""),
        "ended_at": str(m.ended_at or ""),
        "audio_path": m.audio_path, "language": m.language,
        "summary": m.summary,
        "key_points": json.loads(m.key_points) if m.key_points else [],
        "decisions": json.loads(m.decisions) if m.decisions else [],
        "status": m.status,
    }
    if s is not None:
        d["segments"] = (s.query(TranscriptSegment)
                          .filter(TranscriptSegment.meeting_id == m.id)
                          .count())
        rows = (s.query(MeetingParticipant, Person)
                 .outerjoin(Person, Person.id == MeetingParticipant.person_id)
                 .filter(MeetingParticipant.meeting_id == m.id).all())
        d["participants"] = [{"name": p.full_name if p else "?",
                              "detected_by": mp.detected_by}
                             for mp, p in rows]
    return d


class StartIn(BaseModel):
    title: Optional[str] = None


@router.get("/meetings")
def list_meetings(limit: int = 50):
    with SessionLocal() as s:
        rows = (s.query(Meeting).order_by(Meeting.started_at.desc())
                 .limit(min(limit, 200)).all())
        return [_meeting_dict(m, s) for m in rows]


@router.get("/meetings/{meeting_id}")
def get_meeting(meeting_id: int):
    with SessionLocal() as s:
        m = s.get(Meeting, meeting_id)
        if not m:
            raise HTTPException(404, "meeting not found")
        return _meeting_dict(m, s)


@router.get("/meetings/{meeting_id}/transcript")
def get_transcript(meeting_id: int):
    with SessionLocal() as s:
        rows = (s.query(TranscriptSegment)
                 .filter(TranscriptSegment.meeting_id == meeting_id)
                 .order_by(TranscriptSegment.start_ms).all())
        return [{"speaker_label": r.speaker_label, "person_id": r.person_id,
                 "start_ms": r.start_ms, "end_ms": r.end_ms, "text": r.text,
                 "confidence": r.confidence} for r in rows]


@router.post("/meetings/start")
async def start_meeting(body: StartIn):
    from core.runtime import meetings
    reply = await meetings.start(body.title)
    return {"reply": reply, "meeting_id": meetings.meeting_id}


@router.post("/meetings/{meeting_id}/end")
async def end_meeting(meeting_id: int):
    from core.runtime import meetings
    reply = await meetings.end(meeting_id)
    return {"reply": reply}
