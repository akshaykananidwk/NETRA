"""Simple IOU tracker.

Recognition runs ONCE per new track — the key performance rule.  A face that
stays in frame keeps its track (and identity) without re-running recognition,
which also prevents re-greeting the same person every frame.
"""
from __future__ import annotations

import itertools
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

Box = Tuple[int, int, int, int]  # x1, y1, x2, y2


def iou(a: Box, b: Box) -> float:
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(a[2], b[2]), min(a[3], b[3])
    iw, ih = max(0, ix2 - ix1), max(0, iy2 - iy1)
    inter = iw * ih
    if inter == 0:
        return 0.0
    area_a = (a[2] - a[0]) * (a[3] - a[1])
    area_b = (b[2] - b[0]) * (b[3] - b[1])
    return inter / float(area_a + area_b - inter)


@dataclass
class Track:
    track_id: int
    bbox: Box
    created_at: float
    last_seen: float
    hits: int = 1
    # identity is set once, after recognition on the first good detection
    identity: Optional[dict] = None    # {kind: person|unknown|uncertain, ...}
    recognized: bool = False


class IOUTracker:
    def __init__(self, iou_threshold: float = 0.3, max_age_sec: float = 2.0) -> None:
        self.iou_threshold = iou_threshold
        self.max_age_sec = max_age_sec
        self._tracks: Dict[int, Track] = {}
        self._ids = itertools.count(1)

    @property
    def active_tracks(self) -> List[Track]:
        return list(self._tracks.values())

    def update(self, boxes: List[Box],
               now: Optional[float] = None) -> Tuple[List[Tuple[Track, int, bool]], List[Track]]:
        """Match detections to tracks greedily by IOU.

        Returns (assignments, lost):
          assignments — [(track, detection_index, is_new_track), ...]
          lost        — tracks removed this update (aged out)
        """
        now = now if now is not None else time.time()
        assignments: List[Tuple[Track, int, bool]] = []
        unmatched = set(range(len(boxes)))

        # greedy: best IOU pair first
        pairs = []
        for tid, tr in self._tracks.items():
            for di in unmatched:
                score = iou(tr.bbox, boxes[di])
                if score >= self.iou_threshold:
                    pairs.append((score, tid, di))
        pairs.sort(reverse=True)
        used_tracks: set = set()
        for score, tid, di in pairs:
            if tid in used_tracks or di not in unmatched:
                continue
            tr = self._tracks[tid]
            tr.bbox = boxes[di]
            tr.last_seen = now
            tr.hits += 1
            used_tracks.add(tid)
            unmatched.discard(di)
            assignments.append((tr, di, False))

        # new tracks for unmatched detections
        for di in sorted(unmatched):
            tr = Track(track_id=next(self._ids), bbox=boxes[di],
                       created_at=now, last_seen=now)
            self._tracks[tr.track_id] = tr
            assignments.append((tr, di, True))

        # age out stale tracks
        lost = [tr for tr in self._tracks.values()
                if now - tr.last_seen > self.max_age_sec]
        for tr in lost:
            del self._tracks[tr.track_id]
        return assignments, lost
