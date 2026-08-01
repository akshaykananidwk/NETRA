"""Microphone health monitor (Phase 1).

Full listening pipeline (VAD / wake word / STT) arrives in Phase 2 — Phase 1
only proves the mic works and drives the dashboard badge + level meter:

  ok    — device open, audio flowing
  muted — device open but RMS ≈ 0 for > 10 s
  down  — device error / unplugged
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import numpy as np

from config import settings
from core import events as ev
from core.events import EventBus

log = logging.getLogger("krishna.mic")

SILENCE_RMS = 1e-4          # normalised float32 RMS below this counts as silence
SILENCE_SECS = 10.0
REPORT_INTERVAL = 2.0


class MicMonitor(threading.Thread):
    def __init__(self, bus: EventBus) -> None:
        super().__init__(daemon=True, name="mic-monitor")
        self.bus = bus
        self._stop = threading.Event()
        self.status = "down"
        self.detail = ""
        self._rms = 0.0
        self._last_loud = time.time()

    def stop(self) -> None:
        self._stop.set()

    def _callback(self, indata, frames, t, cb_status) -> None:
        rms = float(np.sqrt(np.mean(np.square(indata.astype(np.float32)))))
        self._rms = rms
        if rms > SILENCE_RMS:
            self._last_loud = time.time()

    def run(self) -> None:
        stream = None
        while not self._stop.is_set():
            if stream is None:
                try:
                    import sounddevice as sd
                    device = (None if settings.mic_device_index < 0
                              else settings.mic_device_index)
                    stream = sd.InputStream(samplerate=settings.sample_rate,
                                            channels=1, dtype="float32",
                                            device=device,
                                            callback=self._callback)
                    stream.start()
                    self._last_loud = time.time()
                    self.status = "ok"
                    self.detail = ""
                    log.info("microphone stream open")
                except Exception as e:
                    stream = None
                    self.status = "down"
                    self.detail = str(e)
                    self._publish()
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

            self._publish()
            self._stop.wait(REPORT_INTERVAL)

        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def _publish(self) -> None:
        self.bus.publish(ev.MIC_STATUS, status=self.status, detail=self.detail)
        self.bus.publish(ev.MIC_LEVEL, rms=round(self._rms, 5),
                         level=min(int(self._rms * 2500), 100))
