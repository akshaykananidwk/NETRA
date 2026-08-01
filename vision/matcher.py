"""Cosine-similarity face matching over an in-memory embedding matrix.

One `matrix @ query` per lookup — never a Python loop over persons.
Also hosts the UnknownRegistry that de-duplicates unseen faces so the
same stranger standing in front of the camera creates one record, not fifty.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

from config import settings

log = logging.getLogger("krishna.matcher")


@dataclass
class MatchResult:
    status: str                 # known|uncertain|unknown
    person_id: Optional[int]
    name: Optional[str]
    similarity: float


class FaceMatcher:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._matrix = np.zeros((0, 512), dtype=np.float32)
        self._person_ids = np.zeros((0,), dtype=np.int64)
        self._names: Dict[int, str] = {}

    def set_data(self, embeddings: List[np.ndarray], person_ids: List[int],
                 names: Dict[int, str]) -> None:
        with self._lock:
            if embeddings:
                m = np.vstack([e.astype(np.float32) for e in embeddings])
                norms = np.linalg.norm(m, axis=1, keepdims=True)
                norms[norms == 0] = 1.0
                self._matrix = m / norms
                self._person_ids = np.asarray(person_ids, dtype=np.int64)
            else:
                self._matrix = np.zeros((0, 512), dtype=np.float32)
                self._person_ids = np.zeros((0,), dtype=np.int64)
            self._names = dict(names)

    def reload(self) -> int:
        """Rebuild the matrix from the DB. Returns embedding count."""
        from core.db import FaceEmbedding, Person, SessionLocal
        from vision.embedder import from_blob
        embs: List[np.ndarray] = []
        pids: List[int] = []
        names: Dict[int, str] = {}
        with SessionLocal() as s:
            rows = (s.query(FaceEmbedding.person_id, FaceEmbedding.embedding, Person.full_name)
                     .join(Person, Person.id == FaceEmbedding.person_id).all())
            for pid, blob, name in rows:
                try:
                    embs.append(from_blob(blob))
                except ValueError:
                    log.warning("skipping corrupt embedding for person %s", pid)
                    continue
                pids.append(pid)
                names[pid] = name
        self.set_data(embs, pids, names)
        log.info("matcher loaded %d embeddings for %d persons", len(pids), len(names))
        return len(pids)

    def match(self, embedding: np.ndarray) -> MatchResult:
        with self._lock:
            if self._matrix.shape[0] == 0:
                return MatchResult("unknown", None, None, 0.0)
            sims = self._matrix @ embedding.astype(np.float32)
            idx = int(np.argmax(sims))
            sim = float(sims[idx])
            pid = int(self._person_ids[idx])
            name = self._names.get(pid)
        if sim >= settings.face_match_threshold:
            return MatchResult("known", pid, name, sim)
        if sim >= settings.face_uncertain_threshold:
            return MatchResult("uncertain", pid, name, sim)
        return MatchResult("unknown", None, None, sim)


class UnknownRegistry:
    """In-memory index of pending unknown faces (mirrors unknown_faces table)."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._matrix = np.zeros((0, 512), dtype=np.float32)
        self._uids: List[str] = []
        self._seq_by_day: Dict[str, int] = {}

    def load(self) -> None:
        from core.db import SessionLocal, UnknownFace
        from vision.embedder import from_blob
        with self._lock:
            embs, uids = [], []
            with SessionLocal() as s:
                for row in s.query(UnknownFace).filter(UnknownFace.status == "pending").all():
                    if not row.temp_uid:
                        continue
                    try:
                        embs.append(from_blob(row.embedding))
                    except ValueError:
                        continue
                    uids.append(row.temp_uid)
                    # keep the per-day sequence counter ahead of existing ids
                    try:
                        _, day, seq = row.temp_uid.split("-")
                        self._seq_by_day[day] = max(self._seq_by_day.get(day, 0), int(seq))
                    except ValueError:
                        pass
            self._matrix = (np.vstack(embs) if embs
                            else np.zeros((0, 512), dtype=np.float32))
            self._uids = uids
        log.info("unknown registry loaded %d pending faces", len(self._uids))

    def observe(self, embedding: np.ndarray, day: str) -> Tuple[str, bool]:
        """Match against pending unknowns; returns (temp_uid, is_new)."""
        emb = embedding.astype(np.float32)
        with self._lock:
            if self._matrix.shape[0] > 0:
                sims = self._matrix @ emb
                idx = int(np.argmax(sims))
                if float(sims[idx]) >= settings.unknown_dedupe_threshold:
                    return self._uids[idx], False
            day_key = day.replace("-", "")
            seq = self._seq_by_day.get(day_key, 0) + 1
            self._seq_by_day[day_key] = seq
            uid = f"UNK-{day_key}-{seq:04d}"
            self._matrix = np.vstack([self._matrix, emb[None, :]]) \
                if self._matrix.shape[0] else emb[None, :].copy()
            self._uids.append(uid)
            return uid, True

    def remove(self, temp_uid: str) -> None:
        with self._lock:
            if temp_uid not in self._uids:
                return
            i = self._uids.index(temp_uid)
            self._uids.pop(i)
            self._matrix = np.delete(self._matrix, i, axis=0)
