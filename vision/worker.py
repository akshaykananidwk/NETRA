"""Vision worker — one thread per camera.

Loop: read latest frame → (at DETECTION_FPS) detect on a downscaled copy →
IOU-track → recognise once per new track → publish events → render the
annotated JPEG used by the MJPEG stream.

This thread NEVER touches the DB, TTS or LLM — everything slow goes over
the event bus to the Orchestrator.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Dict, List, Optional

import numpy as np

from config import settings
from core import events as ev
from core.events import EventBus
from vision.capture import CameraCapture
from vision.detector import FaceDetector
from vision.embedder import to_blob
from vision.matcher import FaceMatcher, UnknownRegistry
from vision.tracker import IOUTracker, Track

log = logging.getLogger("krishna.vision")

DETECT_WIDTH = 640          # downscaled width used for detection
SEEN_INTERVAL_SEC = 5.0     # per-track "still here" heartbeat
HEALTH_INTERVAL_SEC = 2.0

# BGR colors for the overlay
_COLORS = {"known": (80, 200, 120), "uncertain": (0, 200, 255),
           "unknown": (60, 60, 230), None: (160, 160, 160)}


class VisionWorker(threading.Thread):
    def __init__(self, camera: dict, bus: EventBus, detector: FaceDetector,
                 matcher: FaceMatcher, unknowns: UnknownRegistry,
                 state_provider=None) -> None:
        super().__init__(daemon=True, name=f"vision-{camera['id']}")
        self.camera_id = camera["id"]
        self.camera_name = camera["name"]
        self.bus = bus
        self.detector = detector
        self.matcher = matcher
        self.unknowns = unknowns
        self.state_provider = state_provider  # callable -> bool (vision active?)
        self.capture = CameraCapture(camera["source_type"], camera["source_url"],
                                     name=camera["name"])
        self.tracker = IOUTracker()
        self._stop = threading.Event()
        self._jpeg: Optional[bytes] = None
        self._jpeg_lock = threading.Lock()
        self._last_status_sent = ""

    # ── public ────────────────────────────────────────────────────────────
    def stop(self) -> None:
        self._stop.set()
        self.capture.stop()

    def latest_jpeg(self) -> Optional[bytes]:
        with self._jpeg_lock:
            return self._jpeg

    # ── loop ──────────────────────────────────────────────────────────────
    def run(self) -> None:
        import cv2
        self.capture.start()
        last_detect = 0.0
        last_health = 0.0
        boxes_to_draw: List[dict] = []

        while not self._stop.is_set():
            try:
                now = time.time()
                if now - last_health >= HEALTH_INTERVAL_SEC:
                    last_health = now
                    self._emit_health()

                frame = self.capture.latest()
                if frame is None or not self.capture.online:
                    time.sleep(0.2)
                    continue

                interval = 1.0 / max(settings.detection_fps, 0.5)
                if now - last_detect < interval:
                    time.sleep(0.01)
                    continue
                last_detect = now

                paused = self.state_provider is not None and not self.state_provider()
                if paused or not self.detector.ready:
                    boxes_to_draw = []
                    self._render(cv2, frame, boxes_to_draw)
                    continue

                h, w = frame.shape[:2]
                scale = w / DETECT_WIDTH if w > DETECT_WIDTH else 1.0
                small = (cv2.resize(frame, (DETECT_WIDTH, int(h / scale)))
                         if scale > 1.0 else frame)
                dets = self.detector.detect(small, scale=scale)

                full_boxes = [tuple(int(v * scale) for v in d.bbox) for d in dets]
                assignments, lost = self.tracker.update(full_boxes, now=now)

                boxes_to_draw = []
                for track, det_idx, is_new in assignments:
                    det = dets[det_idx]
                    if is_new and not track.recognized:
                        self._identify(cv2, track, det, frame)
                    self._heartbeat_track(track, now)
                    boxes_to_draw.append(self._draw_info(track))

                for track in lost:
                    if track.identity:
                        self.bus.publish(ev.TRACK_LOST, camera_id=self.camera_id,
                                         identity=track.identity,
                                         last_seen=track.last_seen)

                self._render(cv2, frame, boxes_to_draw)
            except Exception:
                log.exception("[%s] vision loop error", self.camera_name)
                time.sleep(1.0)

        log.info("[%s] vision worker stopped", self.camera_name)

    # ── helpers ───────────────────────────────────────────────────────────
    def _identify(self, cv2, track: Track, det, frame: np.ndarray) -> None:
        result = self.matcher.match(det.embedding)
        snapshot = self._save_snapshot(cv2, frame, result.status)
        track.recognized = True
        if result.status == "known":
            track.identity = {"kind": "known", "person_id": result.person_id,
                              "name": result.name, "sim": result.similarity}
            self.bus.publish(ev.FACE_RECOGNIZED, camera_id=self.camera_id,
                             camera_name=self.camera_name,
                             person_id=result.person_id, name=result.name,
                             confidence=round(result.similarity, 3),
                             snapshot_path=snapshot, track_id=track.track_id)
        elif result.status == "uncertain":
            track.identity = {"kind": "uncertain", "person_id": result.person_id,
                              "name": result.name, "sim": result.similarity}
            self.bus.publish(ev.FACE_UNCERTAIN, camera_id=self.camera_id,
                             camera_name=self.camera_name,
                             person_id=result.person_id, name=result.name,
                             confidence=round(result.similarity, 3),
                             snapshot_path=snapshot, track_id=track.track_id)
        else:
            from core.db import today_str
            uid, is_new = self.unknowns.observe(det.embedding, today_str())
            track.identity = {"kind": "unknown", "temp_uid": uid}
            self.bus.publish(ev.FACE_UNKNOWN, camera_id=self.camera_id,
                             camera_name=self.camera_name, temp_uid=uid,
                             is_new=is_new, snapshot_path=snapshot,
                             embedding=to_blob(det.embedding) if is_new else None,
                             track_id=track.track_id)

    def _heartbeat_track(self, track: Track, now: float) -> None:
        if track.identity is None:
            return
        last = track.identity.get("_last_seen_emit", 0.0)
        if now - last >= SEEN_INTERVAL_SEC:
            track.identity["_last_seen_emit"] = now
            self.bus.publish(ev.FACE_SEEN, camera_id=self.camera_id,
                             identity=track.identity)

    def _draw_info(self, track: Track) -> dict:
        ident = track.identity or {}
        kind = ident.get("kind")
        if kind == "known":
            label = ident.get("name") or "?"
        elif kind == "uncertain":
            label = f"{ident.get('name') or '?'}?"
        elif kind == "unknown":
            label = ident.get("temp_uid", "UNKNOWN")
        else:
            label = "..."
        return {"bbox": track.bbox, "kind": kind, "label": label}

    def _render(self, cv2, frame: np.ndarray, boxes: List[dict]) -> None:
        h, w = frame.shape[:2]
        scale = w / DETECT_WIDTH if w > DETECT_WIDTH else 1.0
        view = cv2.resize(frame, (DETECT_WIDTH, int(h / scale))) if scale > 1.0 else frame.copy()
        for b in boxes:
            x1, y1, x2, y2 = [int(v / scale) for v in b["bbox"]]
            color = _COLORS.get(b["kind"], _COLORS[None])
            cv2.rectangle(view, (x1, y1), (x2, y2), color, 2)
            cv2.putText(view, b["label"], (x1, max(y1 - 8, 14)),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2, cv2.LINE_AA)
        ok, buf = cv2.imencode(".jpg", view, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
        if ok:
            with self._jpeg_lock:
                self._jpeg = buf.tobytes()

    def _save_snapshot(self, cv2, frame: np.ndarray, tag: str) -> str:
        from core.db import today_str
        day = today_str()
        folder = settings.snapshots_dir / day
        folder.mkdir(parents=True, exist_ok=True)
        fname = f"cam{self.camera_id}_{tag}_{int(time.time() * 1000)}.jpg"
        path = folder / fname
        try:
            cv2.imwrite(str(path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
        except Exception:
            log.exception("snapshot write failed")
            return ""
        return f"snapshots/{day}/{fname}"       # path relative to data dir

    def _emit_health(self) -> None:
        status = "ok" if self.capture.online else "down"
        key = f"{status}:{self.capture.detail}"
        # always publish (drives last_frame_at), flag change for logging
        if key != self._last_status_sent:
            self._last_status_sent = key
            log.info("[%s] camera status -> %s %s", self.camera_name, status,
                     self.capture.detail)
        self.bus.publish(ev.CAMERA_STATUS, camera_id=self.camera_id,
                         camera_name=self.camera_name, status=status,
                         detail=self.capture.detail,
                         detector_ready=self.detector.ready,
                         detector_error=self.detector.error)


class VisionManager:
    """Starts/stops one VisionWorker per enabled camera."""

    def __init__(self, bus: EventBus, detector: FaceDetector,
                 matcher: FaceMatcher, unknowns: UnknownRegistry,
                 state_provider=None) -> None:
        self.bus = bus
        self.detector = detector
        self.matcher = matcher
        self.unknowns = unknowns
        self.state_provider = state_provider
        self.workers: Dict[int, VisionWorker] = {}
        self._lock = threading.Lock()

    def start_all(self) -> None:
        from core.db import Camera, SessionLocal
        with SessionLocal() as s:
            cameras = [c.to_dict() for c in
                       s.query(Camera).filter(Camera.enabled == 1).all()]
        with self._lock:
            for cam in cameras:
                if cam["id"] not in self.workers:
                    w = VisionWorker(cam, self.bus, self.detector,
                                     self.matcher, self.unknowns,
                                     self.state_provider)
                    self.workers[cam["id"]] = w
                    w.start()
        log.info("vision manager: %d worker(s) running", len(self.workers))

    def stop_all(self) -> None:
        with self._lock:
            for w in self.workers.values():
                w.stop()
            self.workers.clear()

    def restart(self) -> None:
        """Apply camera table changes: stop everything, start enabled cameras."""
        self.stop_all()
        self.start_all()

    def get(self, camera_id: int) -> Optional[VisionWorker]:
        with self._lock:
            return self.workers.get(camera_id)
