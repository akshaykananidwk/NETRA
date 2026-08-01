"""Face matcher — cosine search, thresholds, unknown dedupe."""
import numpy as np
import pytest

from config import settings
from vision.matcher import FaceMatcher, UnknownRegistry


def unit(vec):
    v = np.asarray(vec, dtype=np.float32)
    return v / np.linalg.norm(v)


def rand_emb(seed):
    rng = np.random.default_rng(seed)
    return unit(rng.normal(size=512))


@pytest.fixture(autouse=True)
def thresholds():
    old = (settings.face_match_threshold, settings.face_uncertain_threshold,
           settings.unknown_dedupe_threshold)
    settings.face_match_threshold = 0.55
    settings.face_uncertain_threshold = 0.40
    settings.unknown_dedupe_threshold = 0.50
    yield
    (settings.face_match_threshold, settings.face_uncertain_threshold,
     settings.unknown_dedupe_threshold) = old


def make_matcher():
    m = FaceMatcher()
    a, b = rand_emb(1), rand_emb(2)
    m.set_data([a, b], [10, 20], {10: "AK Bhai", 20: "Ramesh"})
    return m, a, b


def test_exact_match_is_known():
    m, a, _ = make_matcher()
    r = m.match(a)
    assert r.status == "known"
    assert r.person_id == 10
    assert r.name == "AK Bhai"
    assert r.similarity > 0.99


def test_random_face_is_unknown():
    m, _, _ = make_matcher()
    r = m.match(rand_emb(99))          # ~orthogonal to enrolled faces
    assert r.status == "unknown"
    assert r.person_id is None


def test_uncertain_band():
    m, a, _ = make_matcher()
    # blend enrolled face with noise until similarity lands in [0.40, 0.55)
    noisy = unit(0.47 * a + np.sqrt(1 - 0.47 ** 2) * rand_emb(7))
    r = m.match(noisy)
    assert 0.40 <= r.similarity < 0.55
    assert r.status == "uncertain"
    assert r.person_id == 10           # best guess still reported


def test_empty_matcher_returns_unknown():
    m = FaceMatcher()
    r = m.match(rand_emb(1))
    assert r.status == "unknown"


def test_unknown_registry_dedupes_same_face():
    reg = UnknownRegistry()
    e = rand_emb(5)
    uid1, new1 = reg.observe(e, "2026-08-01")
    uid2, new2 = reg.observe(e, "2026-08-01")
    assert new1 is True and new2 is False
    assert uid1 == uid2
    assert uid1.startswith("UNK-20260801-")


def test_unknown_registry_new_faces_get_new_uids():
    reg = UnknownRegistry()
    uid1, _ = reg.observe(rand_emb(5), "2026-08-01")
    uid2, _ = reg.observe(rand_emb(6), "2026-08-01")
    assert uid1 != uid2
    assert uid1.endswith("0001") and uid2.endswith("0002")


def test_unknown_registry_remove():
    reg = UnknownRegistry()
    e = rand_emb(5)
    uid, _ = reg.observe(e, "2026-08-01")
    reg.remove(uid)
    uid2, is_new = reg.observe(e, "2026-08-01")
    assert is_new is True
    assert uid2 != uid
