"""Person directory + enrollment + pending-unknown registration."""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field

from config import settings
from core.db import (FaceEmbedding, Person, SessionLocal, UnknownFace, Visit,
                     audit, now_local)
from vision.embedder import best_face_from_image, to_blob

log = logging.getLogger("krishna.api.persons")
router = APIRouter(prefix="/api")

RELATION_TYPES = ("admin", "staff", "visitor", "vip", "vendor", "blacklist")


class PersonUpdate(BaseModel):
    full_name: Optional[str] = None
    name_gu: Optional[str] = None
    call_name: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None
    designation: Optional[str] = None
    relation_type: Optional[str] = None
    is_admin: Optional[bool] = None
    preferred_drink: Optional[str] = None
    notes: Optional[str] = None
    greeting_enabled: Optional[bool] = None


class UnknownRegister(BaseModel):
    full_name: str = Field(min_length=1)
    name_gu: Optional[str] = None
    call_name: Optional[str] = None
    phone: Optional[str] = None
    company: Optional[str] = None
    relation_type: str = "visitor"
    preferred_drink: Optional[str] = None


def _face_counts(s) -> dict:
    counts = {}
    for pid, in s.query(FaceEmbedding.person_id).all():
        counts[pid] = counts.get(pid, 0) + 1
    return counts


def _enroll_photos(person: Person, photos: List[UploadFile]) -> tuple[int, list[str]]:
    """Extract embeddings from uploaded photos. Returns (added, warnings)."""
    import cv2
    from core.runtime import detector
    added, warnings = 0, []
    person_dir = settings.faces_dir / str(person.id)
    person_dir.mkdir(parents=True, exist_ok=True)

    with SessionLocal() as s:
        for photo in photos:
            data = photo.file.read()
            if not data:
                continue
            if not detector.ready and not detector.load():
                warnings.append(f"AI મોડેલ તૈયાર નથી — {photo.filename} છોડ્યો "
                                f"({detector.error})")
                break
            face, img = best_face_from_image(detector, data)
            if img is None:
                warnings.append(f"{photo.filename}: ફોટો વાંચી શકાયો નહીં")
                continue
            fname = f"enroll_{int(time.time() * 1000)}_{added}.jpg"
            fpath = person_dir / fname
            cv2.imwrite(str(fpath), img, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
            rel = f"faces/{person.id}/{fname}"
            if face is None:
                warnings.append(f"{photo.filename}: ચહેરો મળ્યો નહીં")
                continue
            s.add(FaceEmbedding(person_id=person.id, embedding=to_blob(face.embedding),
                                quality=face.score, source_path=rel))
            added += 1
            if not person.photo_path:
                p = s.get(Person, person.id)
                p.photo_path = rel
                person.photo_path = rel
        s.commit()

    if added:
        from core.runtime import matcher
        matcher.reload()
    return added, warnings


# ── persons CRUD ──────────────────────────────────────────────────────────

@router.get("/persons")
def list_persons():
    with SessionLocal() as s:
        counts = _face_counts(s)
        return [p.to_dict(face_count=counts.get(p.id, 0))
                for p in s.query(Person).order_by(Person.full_name).all()]


@router.post("/persons")
def create_person(
    full_name: str = Form(...),
    name_gu: str = Form(""),
    call_name: str = Form(""),
    phone: str = Form(""),
    company: str = Form(""),
    designation: str = Form(""),
    relation_type: str = Form("visitor"),
    is_admin: bool = Form(False),
    preferred_drink: str = Form(""),
    notes: str = Form(""),
    greeting_enabled: bool = Form(True),
    photos: List[UploadFile] = File(default=[]),
):
    if relation_type not in RELATION_TYPES:
        raise HTTPException(422, f"relation_type must be one of {RELATION_TYPES}")
    with SessionLocal() as s:
        person = Person(full_name=full_name.strip(), name_gu=name_gu or None,
                        call_name=call_name or None, phone=phone or None,
                        company=company or None, designation=designation or None,
                        relation_type=relation_type,
                        is_admin=1 if (is_admin or relation_type == "admin") else 0,
                        preferred_drink=preferred_drink or None, notes=notes or None,
                        greeting_enabled=1 if greeting_enabled else 0)
        s.add(person)
        s.commit()
        s.refresh(person)

    added, warnings = _enroll_photos(person, photos or [])
    audit("web", "person.create", f"id={person.id} name={person.full_name} faces={added}")
    with SessionLocal() as s:
        p = s.get(Person, person.id)
        return {"person": p.to_dict(face_count=added), "faces_added": added,
                "warnings": warnings}


@router.get("/persons/{person_id}")
def get_person(person_id: int):
    with SessionLocal() as s:
        p = s.get(Person, person_id)
        if not p:
            raise HTTPException(404, "person not found")
        count = (s.query(FaceEmbedding)
                  .filter(FaceEmbedding.person_id == person_id).count())
        return p.to_dict(face_count=count)


@router.put("/persons/{person_id}")
def update_person(person_id: int, body: PersonUpdate):
    with SessionLocal() as s:
        p = s.get(Person, person_id)
        if not p:
            raise HTTPException(404, "person not found")
        data = body.model_dump(exclude_unset=True)
        if "relation_type" in data and data["relation_type"] not in RELATION_TYPES:
            raise HTTPException(422, f"relation_type must be one of {RELATION_TYPES}")
        for k, v in data.items():
            if k in ("is_admin", "greeting_enabled"):
                v = 1 if v else 0
            setattr(p, k, v)
        p.updated_at = now_local()
        s.commit()
        audit("web", "person.update", f"id={person_id} fields={list(data)}")
        return p.to_dict()


@router.delete("/persons/{person_id}")
def delete_person(person_id: int):
    """Full DPDP purge: embeddings, photos, snapshots of their visits, visits."""
    with SessionLocal() as s:
        p = s.get(Person, person_id)
        if not p:
            raise HTTPException(404, "person not found")
        name = p.full_name
        visits = s.query(Visit).filter(Visit.person_id == person_id).all()
        for v in visits:
            if v.snapshot_path:
                snap = settings.data_dir / v.snapshot_path
                snap.unlink(missing_ok=True)
            s.delete(v)
        s.query(FaceEmbedding).filter(FaceEmbedding.person_id == person_id).delete()
        # detach every remaining FK reference (no cascade in the schema) —
        # otherwise the delete fails with a FOREIGN KEY error
        from core.db import (Conversation, MeetingParticipant, Task,
                             TranscriptSegment)
        s.query(UnknownFace) \
            .filter(UnknownFace.resolved_person_id == person_id) \
            .update({"resolved_person_id": None})
        s.query(Task).filter(Task.assignee_id == person_id) \
            .update({"assignee_id": None})
        s.query(Conversation).filter(Conversation.person_id == person_id) \
            .update({"person_id": None})
        s.query(TranscriptSegment) \
            .filter(TranscriptSegment.person_id == person_id) \
            .update({"person_id": None})
        s.query(MeetingParticipant) \
            .filter(MeetingParticipant.person_id == person_id).delete()
        s.delete(p)
        s.commit()

    person_dir = settings.faces_dir / str(person_id)
    if person_dir.exists():
        shutil.rmtree(person_dir, ignore_errors=True)

    from core.runtime import matcher
    matcher.reload()
    audit("web", "person.delete", f"id={person_id} name={name} (full purge)")
    return {"deleted": person_id}


@router.post("/persons/{person_id}/faces")
def add_faces(person_id: int, photos: List[UploadFile] = File(...)):
    with SessionLocal() as s:
        p = s.get(Person, person_id)
        if not p:
            raise HTTPException(404, "person not found")
    added, warnings = _enroll_photos(p, photos)
    audit("web", "person.add_faces", f"id={person_id} added={added}")
    return {"faces_added": added, "warnings": warnings}


# ── pending unknown faces ─────────────────────────────────────────────────

@router.get("/unknown")
def list_unknown():
    with SessionLocal() as s:
        rows = (s.query(UnknownFace).filter(UnknownFace.status == "pending")
                 .order_by(UnknownFace.last_seen.desc()).all())
        return [r.to_dict() for r in rows]


@router.post("/unknown/{temp_uid}/register")
def register_unknown(temp_uid: str, body: UnknownRegister):
    if body.relation_type not in RELATION_TYPES:
        raise HTTPException(422, f"relation_type must be one of {RELATION_TYPES}")
    with SessionLocal() as s:
        u = s.query(UnknownFace).filter(UnknownFace.temp_uid == temp_uid).first()
        if not u:
            raise HTTPException(404, "unknown face not found")
        if u.status != "pending":
            raise HTTPException(409, f"already {u.status}")

        person = Person(full_name=body.full_name.strip(), name_gu=body.name_gu,
                        call_name=body.call_name, phone=body.phone,
                        company=body.company, relation_type=body.relation_type,
                        preferred_drink=body.preferred_drink)
        s.add(person)
        s.flush()

        # move the sighting embedding into the person's enrollment set
        photo_rel = None
        if u.snapshot_path:
            src = settings.data_dir / u.snapshot_path
            if src.exists():
                person_dir = settings.faces_dir / str(person.id)
                person_dir.mkdir(parents=True, exist_ok=True)
                dst = person_dir / f"from_unknown_{Path(u.snapshot_path).name}"
                shutil.copy2(src, dst)
                photo_rel = f"faces/{person.id}/{dst.name}"
        person.photo_path = photo_rel
        s.add(FaceEmbedding(person_id=person.id, embedding=u.embedding,
                            quality=None, source_path=photo_rel))

        u.status = "registered"
        u.resolved_person_id = person.id
        # attach their past visits to the new person record
        for v in s.query(Visit).filter(Visit.unknown_id == u.id).all():
            v.person_id = person.id
            v.status = "known"
        s.commit()
        person_id = person.id
        person_dict = person.to_dict(face_count=1)

    from core.runtime import matcher, unknowns
    matcher.reload()
    unknowns.remove(temp_uid)
    audit("web", "unknown.register", f"{temp_uid} -> person {person_id}")
    return {"person": person_dict}


@router.post("/unknown/{temp_uid}/ignore")
def ignore_unknown(temp_uid: str):
    with SessionLocal() as s:
        u = s.query(UnknownFace).filter(UnknownFace.temp_uid == temp_uid).first()
        if not u:
            raise HTTPException(404, "unknown face not found")
        u.status = "ignored"
        s.commit()
    from core.runtime import unknowns
    unknowns.remove(temp_uid)
    audit("web", "unknown.ignore", temp_uid)
    return {"ignored": temp_uid}
