"""MJPEG live stream — annotated frames from the vision workers."""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from config import settings

router = APIRouter()

_BOUNDARY = "krishnaframe"
_PLACEHOLDER = None  # lazily-built "camera offline" JPEG


def _placeholder_jpeg() -> bytes:
    global _PLACEHOLDER
    if _PLACEHOLDER is None:
        try:
            import cv2
            import numpy as np
            img = np.zeros((360, 640, 3), dtype=np.uint8)
            img[:] = (32, 18, 11)  # dark navy (BGR)
            cv2.putText(img, "CAMERA OFFLINE", (170, 190),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.9, (168, 136, 30), 2, cv2.LINE_AA)
            ok, buf = cv2.imencode(".jpg", img)
            _PLACEHOLDER = buf.tobytes() if ok else b""
        except Exception:
            _PLACEHOLDER = b""
    return _PLACEHOLDER


@router.get("/stream/{camera_id}")
async def mjpeg_stream(camera_id: int):
    from core.runtime import vision_manager
    worker = vision_manager.get(camera_id)
    if worker is None:
        raise HTTPException(404, "camera not running")

    async def generate():
        interval = 1.0 / max(settings.stream_fps, 1)
        while True:
            frame = worker.latest_jpeg() or _placeholder_jpeg()
            if frame:
                yield (f"--{_BOUNDARY}\r\n"
                       "Content-Type: image/jpeg\r\n"
                       f"Content-Length: {len(frame)}\r\n\r\n").encode() + frame + b"\r\n"
            await asyncio.sleep(interval)

    return StreamingResponse(
        generate(),
        media_type=f"multipart/x-mixed-replace; boundary={_BOUNDARY}",
        headers={"Cache-Control": "no-store"})
