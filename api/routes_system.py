"""System status, state control, health, runtime settings."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from config import settings
from core.db import (Person, SessionLocal, Setting, SystemHealth, UnknownFace,
                     Visit, audit, now_local, today_str)
from core.state import VALID_STATES

router = APIRouter(prefix="/api")

PRESENT_WINDOW_MIN = 3     # seen within N minutes == "in the office now"


class StateIn(BaseModel):
    state: str


class SettingsIn(BaseModel):
    values: Dict[str, str]


@router.get("/system/status")
def system_status():
    from core.runtime import orchestrator
    day_start = datetime.strptime(today_str(), "%Y-%m-%d")
    cutoff = now_local() - timedelta(minutes=PRESENT_WINDOW_MIN)
    with SessionLocal() as s:
        visits_today = (s.query(Visit)
                         .filter(Visit.first_seen >= day_start).count())
        known_today = (s.query(Visit.person_id)
                        .filter(Visit.first_seen >= day_start,
                                Visit.person_id.isnot(None))
                        .distinct().count())
        unknown_pending = (s.query(UnknownFace)
                            .filter(UnknownFace.status == "pending").count())
        persons_total = s.query(Person).count()
        present_rows = (s.query(Visit, Person)
                         .join(Person, Person.id == Visit.person_id)
                         .filter(Visit.last_seen >= cutoff).all())
        present = [{"person_id": p.id, "name": p.full_name,
                    "call_name": p.call_name, "relation_type": p.relation_type,
                    "photo_path": p.photo_path}
                   for _, p in {v.person_id: (v, p) for v, p in present_rows}.values()]
    return {
        "state": orchestrator.state.state,
        "uptime_sec": orchestrator.state.uptime_sec,
        "now": str(now_local()),
        "detector_ready": orchestrator.detector_ready,
        "detector_error": orchestrator.detector_error,
        "mic": orchestrator.mic_status,
        "cameras": orchestrator.camera_status,
        "last_transcript": orchestrator.last_transcript,
        "today": {"visits": visits_today, "known_visitors": known_today,
                  "unknown_pending": unknown_pending,
                  "persons_total": persons_total},
        "present": present,
        "activity": list(orchestrator.activity),
    }


@router.post("/system/state")
def set_state(body: StateIn):
    if body.state not in VALID_STATES:
        raise HTTPException(422, f"state must be one of {VALID_STATES}")
    from core.runtime import orchestrator
    orchestrator.state.set(body.state)
    audit("web", "system.state", body.state)
    return {"state": body.state}


@router.get("/system/health")
def system_health():
    with SessionLocal() as s:
        return [{"component": h.component, "status": h.status,
                 "detail": h.detail, "updated_at": str(h.updated_at)}
                for h in s.query(SystemHealth).all()]


@router.get("/settings")
def get_settings():
    editable = {}
    for key in settings.RUNTIME_EDITABLE:
        editable[key] = getattr(settings, key)
    return {"editable": editable}


@router.post("/settings")
def save_settings(body: SettingsIn):
    applied, rejected = {}, {}
    with SessionLocal() as s:
        for key, raw in body.values.items():
            key_l = key.lower()
            typ = settings.RUNTIME_EDITABLE.get(key_l)
            if typ is None:
                rejected[key] = "unknown key"
                continue
            try:
                val = typ(raw)
            except (TypeError, ValueError):
                rejected[key] = "bad value"
                continue
            setattr(settings, key_l, val)
            row = s.get(Setting, key_l)
            if row is None:
                s.add(Setting(key=key_l, value=str(val)))
            else:
                row.value = str(val)
            applied[key_l] = val
        s.commit()
    if applied:
        audit("web", "settings.save", str(applied))
    return {"applied": applied, "rejected": rejected}
