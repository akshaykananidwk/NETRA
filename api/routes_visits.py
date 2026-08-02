"""Visitor log — filterable list + CSV export."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

from core.db import Camera, Person, SessionLocal, UnknownFace, Visit, today_str

router = APIRouter(prefix="/api")


def _query(s, date: Optional[str], person_id: Optional[int]):
    q = (s.query(Visit, Person, UnknownFace, Camera)
          .outerjoin(Person, Person.id == Visit.person_id)
          .outerjoin(UnknownFace, UnknownFace.id == Visit.unknown_id)
          .outerjoin(Camera, Camera.id == Visit.camera_id))
    if date:
        try:
            day = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            from fastapi import HTTPException
            raise HTTPException(422, f"ખોટી તારીખ: {date} (YYYY-MM-DD જોઈએ)")
        q = q.filter(Visit.first_seen >= day,
                     Visit.first_seen < day + timedelta(days=1))
    if person_id:
        q = q.filter(Visit.person_id == person_id)
    return q.order_by(Visit.first_seen.desc())


def _row(v: Visit, p: Optional[Person], u: Optional[UnknownFace],
         c: Optional[Camera]) -> dict:
    return {
        "id": v.id,
        "person_id": v.person_id,
        "name": (p.full_name if p else (u.temp_uid if u else "?")),
        "call_name": p.call_name if p else None,
        "relation_type": p.relation_type if p else "unknown",
        "camera": c.name if c else "",
        "first_seen": str(v.first_seen),
        "last_seen": str(v.last_seen),
        "confidence": v.confidence,
        "snapshot_path": v.snapshot_path,
        "greeted": bool(v.greeted),
        "drink_served": v.drink_served,
        "status": v.status,
    }


@router.get("/visits")
def list_visits(date: Optional[str] = None, person_id: Optional[int] = None,
                limit: int = 200):
    date = date or today_str()
    if date == "all":
        date = None
    with SessionLocal() as s:
        rows = _query(s, date, person_id).limit(min(limit, 1000)).all()
        return [_row(*r) for r in rows]


@router.get("/visits/export.csv")
def export_csv(date: Optional[str] = None, person_id: Optional[int] = None):
    with SessionLocal() as s:
        rows = _query(s, None if date == "all" else (date or None),
                      person_id).limit(10000).all()
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["id", "name", "relation", "camera", "first_seen",
                    "last_seen", "confidence", "status"])
        for v, p, u, c in rows:
            r = _row(v, p, u, c)
            w.writerow([r["id"], r["name"], r["relation_type"], r["camera"],
                        r["first_seen"], r["last_seen"], r["confidence"],
                        r["status"]])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition":
                 f"attachment; filename=visits_{date or 'all'}.csv"})
