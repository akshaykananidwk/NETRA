"""Speaker identification — ECAPA-TDNN voice prints (speechbrain).

OPTIONAL: speechbrain pulls in torch (~2.5 GB), so it is not in
requirements.txt. Without it, `ready` stays False and admin verification
falls back to the admin-face-on-camera heuristic (see agent/commander.py).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import List, Optional, Tuple

import numpy as np

from config import settings

log = logging.getLogger("krishna.speaker")

ECAPA_DIM = 192
CACHE_TTL_SEC = 120


class SpeakerID:
    def __init__(self) -> None:
        self._model = None
        self._lock = threading.Lock()
        self.ready = False
        self.error: Optional[str] = "not loaded"
        self._matrix = np.zeros((0, ECAPA_DIM), dtype=np.float32)
        self._person_ids: List[int] = []
        self._names = {}
        self._admins = set()
        self._cache_at = 0.0

    def load(self) -> bool:
        with self._lock:
            if self._model is not None:
                return True
            try:
                import torch  # noqa: F401
                from speechbrain.inference.speaker import EncoderClassifier
                self._model = EncoderClassifier.from_hparams(
                    source="speechbrain/spkrec-ecapa-voxceleb",
                    savedir=str(settings.models_dir / "ecapa"))
                self.ready = True
                self.error = None
                log.info("ECAPA speaker model ready")
                return True
            except Exception as e:
                self.error = str(e)[:200]
                log.info("speaker-id unavailable (%s) — using face fallback",
                         self.error)
                return False

    # ── embeddings ────────────────────────────────────────────────────────
    def embed(self, pcm: bytes) -> Optional[np.ndarray]:
        if not self.ready and not self.load():
            return None
        import torch
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if len(audio) < settings.sample_rate // 2:      # < 0.5 s — too short
            return None
        with self._lock:
            emb = self._model.encode_batch(
                torch.from_numpy(audio).unsqueeze(0)).squeeze().numpy()
        emb = emb.astype(np.float32)
        n = np.linalg.norm(emb)
        return emb / n if n > 0 else emb

    def enroll(self, person_id: int, pcm: bytes,
               sample_path: Optional[str] = None) -> bool:
        emb = self.embed(pcm)
        if emb is None:
            return False
        from core.db import SessionLocal, VoicePrint
        with SessionLocal() as s:
            s.add(VoicePrint(person_id=person_id, embedding=emb.tobytes(),
                             sample_path=sample_path))
            s.commit()
        self._cache_at = 0.0            # force reload
        return True

    # ── identification ────────────────────────────────────────────────────
    def _refresh_cache(self) -> None:
        if time.time() - self._cache_at < CACHE_TTL_SEC:
            return
        from core.db import Person, SessionLocal, VoicePrint
        embs, pids, names, admins = [], [], {}, set()
        with SessionLocal() as s:
            rows = (s.query(VoicePrint.person_id, VoicePrint.embedding,
                            Person.full_name, Person.is_admin)
                     .join(Person, Person.id == VoicePrint.person_id).all())
            for pid, blob, name, is_admin in rows:
                emb = np.frombuffer(blob, dtype=np.float32)
                if emb.shape[0] != ECAPA_DIM:
                    continue
                embs.append(emb)
                pids.append(pid)
                names[pid] = name
                if is_admin:
                    admins.add(pid)
        self._matrix = (np.vstack(embs) if embs
                        else np.zeros((0, ECAPA_DIM), dtype=np.float32))
        self._person_ids = pids
        self._names = names
        self._admins = admins
        self._cache_at = time.time()

    def identify(self, pcm: bytes) -> Optional[Tuple[int, str, bool, float]]:
        """Returns (person_id, name, is_admin, score) or None."""
        emb = self.embed(pcm)
        if emb is None:
            return None
        self._refresh_cache()
        if self._matrix.shape[0] == 0:
            return None
        sims = self._matrix @ emb
        idx = int(np.argmax(sims))
        score = float(sims[idx])
        if score < settings.speaker_match_threshold:
            return None
        pid = self._person_ids[idx]
        return pid, self._names.get(pid, ""), pid in self._admins, score
