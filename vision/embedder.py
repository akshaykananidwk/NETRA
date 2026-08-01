"""Embedding helpers — enrollment photos → embeddings, blob <-> numpy."""
from __future__ import annotations

import logging
from typing import List, Optional, Tuple

import numpy as np

from vision.detector import DetectedFace, FaceDetector

log = logging.getLogger("krishna.embedder")

EMB_DIM = 512


def to_blob(embedding: np.ndarray) -> bytes:
    return np.asarray(embedding, dtype=np.float32).tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    emb = np.frombuffer(blob, dtype=np.float32)
    if emb.shape[0] != EMB_DIM:
        raise ValueError(f"bad embedding blob length {emb.shape[0]}")
    n = np.linalg.norm(emb)
    return emb / n if n > 0 else emb


def best_face_from_image(detector: FaceDetector,
                         image_bytes: bytes) -> Tuple[Optional[DetectedFace], Optional[np.ndarray]]:
    """Decode an uploaded photo and return the highest-quality face + the
    decoded BGR image. Returns (None, image) when no acceptable face found."""
    import cv2
    arr = np.frombuffer(image_bytes, dtype=np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is None:
        return None, None
    # enrollment photos are usually close-ups — no downscaling needed
    faces: List[DetectedFace] = detector.detect(img, scale=1.0)
    if not faces:
        return None, img
    faces.sort(key=lambda f: f.score, reverse=True)
    return faces[0], img
