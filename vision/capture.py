"""Camera capture with auto-reconnect (webcam index or RTSP URL)."""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import numpy as np

log = logging.getLogger("krishna.capture")


class CameraCapture:
    """Continuously reads frames on its own thread, keeps only the latest.

    Reconnects on failure with exponential backoff 1s → 30s.
    """

    def __init__(self, source_type: str, source_url: str, name: str = "camera") -> None:
        self.source_type = source_type
        self.source_url = source_url
        self.name = name
        self.status = "unknown"          # online|offline|error
        self.detail = ""
        self.last_frame_at: float = 0.0
        self._frame: Optional[np.ndarray] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _source(self):
        if self.source_type == "webcam":
            try:
                return int(self.source_url)
            except ValueError:
                return 0
        return self.source_url  # rtsp/http URL

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name=f"capture-{self.name}")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def latest(self) -> Optional[np.ndarray]:
        with self._lock:
            return self._frame

    @property
    def online(self) -> bool:
        return self.status == "online" and (time.time() - self.last_frame_at) < 5

    def _run(self) -> None:
        import cv2  # heavy import kept inside the worker thread
        backoff = 1.0
        cap = None
        while not self._stop.is_set():
            if cap is None:
                try:
                    cap = cv2.VideoCapture(self._source())
                    if self.source_type == "rtsp":
                        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                    if not cap.isOpened():
                        raise RuntimeError("could not open source")
                    backoff = 1.0
                    self.status = "online"
                    self.detail = ""
                    log.info("[%s] connected to %s", self.name, self.source_url)
                except Exception as e:
                    if cap is not None:
                        cap.release()
                    cap = None
                    self.status = "offline"
                    self.detail = str(e)
                    log.warning("[%s] connect failed (%s), retry in %.0fs",
                                self.name, e, backoff)
                    self._stop.wait(backoff)
                    backoff = min(backoff * 2, 30.0)
                    continue

            ok, frame = cap.read()
            if not ok or frame is None:
                self.status = "offline"
                self.detail = "frame read failed"
                cap.release()
                cap = None
                continue

            self.last_frame_at = time.time()
            self.status = "online"
            with self._lock:
                self._frame = frame

            if self.source_type == "webcam":
                # webcams block in read(); tiny sleep keeps CPU sane
                time.sleep(0.005)

        if cap is not None:
            cap.release()
        self.status = "offline"
        self.detail = "stopped"
