"""Microphone stream — 16 kHz mono int16 frames with health + auto-reconnect.

States: ok (audio flowing) · muted (open but silent > 10 s) · down (error).
The AudioWorker consumes frames and publishes the status badges.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Optional

import numpy as np

from config import settings

log = logging.getLogger("krishna.mic")

FRAME_MS = 30
SILENCE_RMS = 60          # int16 RMS below this counts as silence
SILENCE_SECS = 10.0


class MicStream(threading.Thread):
    def __init__(self) -> None:
        super().__init__(daemon=True, name="mic-stream")
        self.frame_samples = settings.sample_rate * FRAME_MS // 1000
        self._frames: "queue.Queue[bytes]" = queue.Queue(maxsize=300)
        self._stop = threading.Event()
        self._buf = b""
        self.status = "down"
        self.detail = "starting"
        self.rms = 0.0
        self._last_loud = time.time()

    def read(self, timeout: float = 0.5) -> Optional[bytes]:
        """Next 30 ms int16 frame, or None on timeout."""
        try:
            return self._frames.get(timeout=timeout)
        except queue.Empty:
            return None

    def stop(self) -> None:
        self._stop.set()

    @property
    def level_pct(self) -> int:
        return min(int(self.rms / 6000 * 100), 100)

    def _callback(self, indata, frames, t, cb_status) -> None:
        data = bytes(indata)
        self._buf += data
        n = self.frame_samples * 2
        while len(self._buf) >= n:
            chunk, self._buf = self._buf[:n], self._buf[n:]
            arr = np.frombuffer(chunk, dtype=np.int16)
            self.rms = float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))
            if self.rms > SILENCE_RMS:
                self._last_loud = time.time()
            try:
                self._frames.put_nowait(chunk)
            except queue.Full:
                try:                      # drop oldest, keep newest
                    self._frames.get_nowait()
                    self._frames.put_nowait(chunk)
                except queue.Empty:
                    pass

    def run(self) -> None:
        stream = None
        while not self._stop.is_set():
            if stream is None:
                try:
                    import sounddevice as sd
                    device = (None if settings.mic_device_index < 0
                              else settings.mic_device_index)
                    if not getattr(self, "_devices_logged", False):
                        self._devices_logged = True
                        try:
                            for i, dv in enumerate(sd.query_devices()):
                                if dv.get("max_input_channels", 0) > 0:
                                    log.info("mic વિકલ્પ %d: %s", i,
                                             dv.get("name", "?"))
                        except Exception:
                            pass
                    stream = sd.RawInputStream(
                        samplerate=settings.sample_rate, channels=1,
                        dtype="int16", device=device,
                        blocksize=self.frame_samples,
                        callback=self._callback)
                    stream.start()
                    self._last_loud = time.time()
                    self.status = "ok"
                    self.detail = ""
                    try:
                        used = sd.query_devices(
                            device if device is not None
                            else sd.default.device[0], "input")
                        log.info("microphone stream open — વપરાય છે: %s "
                                 "(બદલવા .env માં MIC_DEVICE_INDEX સેટ કરો)",
                                 used.get("name", "?"))
                    except Exception:
                        log.info("microphone stream open")
                except Exception as e:
                    stream = None
                    self.status = "down"
                    self.detail = str(e)
                    self._stop.wait(5.0)
                    continue
            try:
                if not stream.active:
                    raise RuntimeError("stream inactive")
                if time.time() - self._last_loud > SILENCE_SECS:
                    self.status = "muted"
                    self.detail = "no audio signal"
                else:
                    self.status = "ok"
                    self.detail = ""
            except Exception as e:
                try:
                    stream.close()
                except Exception:
                    pass
                stream = None
                self.status = "down"
                self.detail = str(e)
            self._stop.wait(1.0)

        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
