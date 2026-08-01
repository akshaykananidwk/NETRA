"""InsightFace (buffalo_l) face detection + quality filtering.

The model bundle includes detection + recognition, so each detected face
already carries its 512-d normalised embedding.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

from config import settings

log = logging.getLogger("krishna.detector")


@dataclass
class DetectedFace:
    bbox: tuple            # (x1, y1, x2, y2) in the frame passed to detect()
    score: float           # det_score
    embedding: np.ndarray  # float32[512], L2-normalised
    yaw: float


def _estimate_yaw(face) -> float:
    """Prefer the model's 3D pose; fall back to a keypoint heuristic."""
    pose = getattr(face, "pose", None)
    if pose is not None:
        try:
            return float(pose[1])
        except (TypeError, IndexError):
            pass
    kps = getattr(face, "kps", None)
    if kps is None or len(kps) < 3:
        return 0.0
    left_eye, right_eye, nose = kps[0], kps[1], kps[2]
    eye_mid_x = (left_eye[0] + right_eye[0]) / 2.0
    eye_width = max(abs(right_eye[0] - left_eye[0]), 1.0)
    return float((nose[0] - eye_mid_x) / eye_width * 90.0)


class FaceDetector:
    """Lazy-loading, thread-safe wrapper around insightface.FaceAnalysis."""

    def __init__(self) -> None:
        self._app = None
        self._load_lock = threading.Lock()
        self._infer_lock = threading.Lock()
        self.error: Optional[str] = "not loaded yet"

    @property
    def ready(self) -> bool:
        return self._app is not None

    def load(self) -> bool:
        with self._load_lock:
            if self._app is not None:
                return True
            try:
                from insightface.app import FaceAnalysis
                providers = (["CUDAExecutionProvider", "CPUExecutionProvider"]
                             if settings.use_gpu else ["CPUExecutionProvider"])
                app = FaceAnalysis(name=settings.face_model,
                                   root=str(settings.models_dir),
                                   providers=providers)
                app.prepare(ctx_id=0 if settings.use_gpu else -1, det_size=(640, 640))
                self._app = app
                self.error = None
                log.info("insightface %s ready (gpu=%s)", settings.face_model, settings.use_gpu)
                return True
            except Exception as e:
                self.error = str(e)
                log.error("insightface load failed: %s", e)
                return False

    def detect(self, frame_bgr: np.ndarray, scale: float = 1.0) -> List[DetectedFace]:
        """Detect faces in frame. `scale` maps bbox/size checks back to the
        full-resolution frame (frame_bgr may be downscaled by 1/scale)."""
        if self._app is None and not self.load():
            return []
        with self._infer_lock:
            faces = self._app.get(frame_bgr)

        out: List[DetectedFace] = []
        for f in faces:
            if float(f.det_score) < settings.det_score_min:
                continue
            x1, y1, x2, y2 = [int(v) for v in f.bbox]
            # size check against full-res pixels
            if (x2 - x1) * scale < settings.min_face_size or \
               (y2 - y1) * scale < settings.min_face_size:
                continue
            yaw = _estimate_yaw(f)
            if abs(yaw) > settings.max_yaw_deg:
                continue
            emb = np.asarray(f.normed_embedding, dtype=np.float32)
            n = np.linalg.norm(emb)
            if n > 0:
                emb = emb / n
            out.append(DetectedFace(bbox=(x1, y1, x2, y2),
                                    score=float(f.det_score),
                                    embedding=emb, yaw=yaw))
        return out
