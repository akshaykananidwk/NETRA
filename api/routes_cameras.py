"""Camera management. Changes restart the vision workers."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from core.db import Camera, SessionLocal, audit

router = APIRouter(prefix="/api")


class CameraIn(BaseModel):
    name: str = Field(min_length=1)
    source_type: str = "webcam"          # webcam|rtsp
    source_url: str = "0"
    zone: Optional[str] = None
    greet_here: bool = True
    enabled: bool = True


class CameraUpdate(BaseModel):
    name: Optional[str] = None
    source_type: Optional[str] = None
    source_url: Optional[str] = None
    zone: Optional[str] = None
    greet_here: Optional[bool] = None
    enabled: Optional[bool] = None


def _validate_source(source_type: str) -> None:
    if source_type not in ("webcam", "rtsp"):
        raise HTTPException(422, "source_type must be webcam or rtsp")


@router.get("/cameras")
def list_cameras():
    with SessionLocal() as s:
        return [c.to_dict() for c in s.query(Camera).order_by(Camera.id).all()]


@router.post("/cameras")
def create_camera(body: CameraIn):
    _validate_source(body.source_type)
    with SessionLocal() as s:
        cam = Camera(name=body.name, source_type=body.source_type,
                     source_url=body.source_url, zone=body.zone,
                     greet_here=1 if body.greet_here else 0,
                     enabled=1 if body.enabled else 0)
        s.add(cam)
        s.commit()
        s.refresh(cam)
        out = cam.to_dict()
    from core.runtime import vision_manager
    vision_manager.restart()
    audit("web", "camera.create", f"id={out['id']} {body.name}")
    return out


@router.put("/cameras/{camera_id}")
def update_camera(camera_id: int, body: CameraUpdate):
    with SessionLocal() as s:
        cam = s.get(Camera, camera_id)
        if not cam:
            raise HTTPException(404, "camera not found")
        data = body.model_dump(exclude_unset=True)
        if "source_type" in data:
            _validate_source(data["source_type"])
        for k, v in data.items():
            if k in ("greet_here", "enabled"):
                v = 1 if v else 0
            setattr(cam, k, v)
        s.commit()
        out = cam.to_dict()
    from core.runtime import vision_manager
    vision_manager.restart()
    audit("web", "camera.update", f"id={camera_id} fields={list(data)}")
    return out


@router.delete("/cameras/{camera_id}")
def delete_camera(camera_id: int):
    with SessionLocal() as s:
        cam = s.get(Camera, camera_id)
        if not cam:
            raise HTTPException(404, "camera not found")
        s.delete(cam)
        s.commit()
    from core.runtime import vision_manager
    vision_manager.restart()
    audit("web", "camera.delete", f"id={camera_id}")
    return {"deleted": camera_id}
