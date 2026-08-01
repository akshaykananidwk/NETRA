"""Gujarati TTS — edge-tts (cloud) primary, Piper (offline) fallback.

Synthesised audio is cached on disk keyed by (engine, voice, rate, text), so
repeated greetings are instant and yesterday's phrases keep working offline.
synth() returns the path of a playable audio file, or None when every engine
failed (callers must treat that as "could not speak" and skip the flow).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import threading
import wave
from pathlib import Path
from typing import Optional

from config import settings

log = logging.getLogger("krishna.tts")

EDGE_TIMEOUT_SEC = 8

_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "️‍❤]", flags=re.UNICODE)


def clean_for_tts(text: str) -> str:
    text = _EMOJI_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


class TTSEngine:
    def __init__(self) -> None:
        self._piper_voice = None
        self._piper_lock = threading.Lock()
        self.last_error: Optional[str] = None
        self.last_engine: Optional[str] = None

    def _cache_path(self, engine: str, text: str, ext: str) -> Path:
        key = hashlib.sha1(
            f"{engine}|{settings.edge_voice}|{settings.tts_rate}|{text}"
            .encode("utf-8")).hexdigest()
        return settings.tts_cache_dir / f"{key}.{ext}"

    async def synth(self, text: str) -> Optional[str]:
        """Text → audio file path (mp3/wav). None if all engines failed."""
        text = clean_for_tts(text)
        if not text:
            return None
        settings.tts_cache_dir.mkdir(parents=True, exist_ok=True)

        engines = (["edge", "piper"] if settings.tts_engine == "edge"
                   else ["piper", "edge"])
        for engine in engines:
            try:
                if engine == "edge":
                    path = await self._edge(text)
                else:
                    path = await asyncio.to_thread(self._piper, text)
                if path:
                    self.last_engine = engine
                    self.last_error = None
                    return str(path)
            except Exception as e:
                self.last_error = f"{engine}: {e}"
                log.warning("tts %s failed: %s", engine, e)
        return None

    async def _edge(self, text: str) -> Optional[Path]:
        path = self._cache_path("edge", text, "mp3")
        if path.exists() and path.stat().st_size > 0:
            return path
        import edge_tts
        tmp = path.with_suffix(".part")
        communicate = edge_tts.Communicate(text, settings.edge_voice,
                                           rate=settings.tts_rate)
        await asyncio.wait_for(communicate.save(str(tmp)), timeout=EDGE_TIMEOUT_SEC)
        if not tmp.exists() or tmp.stat().st_size == 0:
            raise RuntimeError("empty edge-tts output")
        tmp.replace(path)
        return path

    def _piper(self, text: str) -> Optional[Path]:
        path = self._cache_path("piper", text, "wav")
        if path.exists() and path.stat().st_size > 0:
            return path
        if not settings.piper_model_path.exists():
            raise FileNotFoundError(
                f"piper model missing: {settings.piper_model_path}")
        with self._piper_lock:
            if self._piper_voice is None:
                from piper import PiperVoice
                self._piper_voice = PiperVoice.load(str(settings.piper_model_path))
            tmp = path.with_suffix(".part")
            with wave.open(str(tmp), "wb") as wav_file:
                self._piper_voice.synthesize(text, wav_file)
        tmp.replace(path)
        return path
