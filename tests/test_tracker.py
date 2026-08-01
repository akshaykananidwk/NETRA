"""IOU tracker — one recognition per track, aging, greedy matching."""
from vision.tracker import IOUTracker, iou


def test_iou_math():
    assert iou((0, 0, 10, 10), (0, 0, 10, 10)) == 1.0
    assert iou((0, 0, 10, 10), (20, 20, 30, 30)) == 0.0
    assert 0.0 < iou((0, 0, 10, 10), (5, 0, 15, 10)) < 1.0


def test_new_detection_creates_track():
    t = IOUTracker()
    assignments, lost = t.update([(10, 10, 100, 100)], now=100.0)
    assert len(assignments) == 1
    track, det_idx, is_new = assignments[0]
    assert is_new is True and det_idx == 0
    assert lost == []


def test_same_face_keeps_track_id():
    t = IOUTracker()
    (tr1, _, _), = t.update([(10, 10, 100, 100)], now=100.0)[0]
    # slight movement — high IOU
    (tr2, _, is_new), = t.update([(14, 12, 104, 102)], now=100.2)[0]
    assert is_new is False
    assert tr2.track_id == tr1.track_id
    assert tr2.hits == 2


def test_track_ages_out_and_is_reported_lost():
    t = IOUTracker(max_age_sec=2.0)
    t.update([(10, 10, 100, 100)], now=100.0)
    assignments, lost = t.update([], now=103.0)
    assert assignments == []
    assert len(lost) == 1
    # a new appearance afterwards is a NEW track (recognition runs again)
    (tr, _, is_new), = t.update([(10, 10, 100, 100)], now=103.5)[0]
    assert is_new is True


def test_two_faces_two_tracks():
    t = IOUTracker()
    assignments, _ = t.update([(0, 0, 50, 50), (200, 200, 260, 260)], now=1.0)
    assert len(assignments) == 2
    ids = {tr.track_id for tr, _, _ in assignments}
    assert len(ids) == 2


def test_identity_persists_on_track():
    t = IOUTracker()
    (tr, _, _), = t.update([(10, 10, 100, 100)], now=1.0)[0]
    tr.identity = {"kind": "known", "person_id": 7}
    tr.recognized = True
    (tr2, _, _), = t.update([(12, 10, 102, 100)], now=1.2)[0]
    assert tr2.recognized is True
    assert tr2.identity["person_id"] == 7
