"""Schema + ORM round-trip on a temp database."""
import numpy as np
import pytest

import core.db as db
from vision.embedder import from_blob, to_blob


@pytest.fixture()
def session(tmp_path):
    db.init_db(tmp_path / "test.db")
    with db.SessionLocal() as s:
        yield s


def test_schema_applies_and_person_roundtrip(session):
    p = db.Person(full_name="AK Bhai", call_name="સાહેબ", relation_type="admin",
                  is_admin=1, preferred_drink="chai")
    session.add(p)
    session.commit()
    got = session.query(db.Person).one()
    assert got.full_name == "AK Bhai"
    assert got.call_name == "સાહેબ"
    assert bool(got.is_admin)


def test_embedding_blob_roundtrip(session):
    p = db.Person(full_name="X")
    session.add(p)
    session.flush()
    emb = np.random.default_rng(1).normal(size=512).astype(np.float32)
    emb /= np.linalg.norm(emb)
    session.add(db.FaceEmbedding(person_id=p.id, embedding=to_blob(emb), quality=0.9))
    session.commit()
    blob = session.query(db.FaceEmbedding).one().embedding
    back = from_blob(blob)
    assert np.allclose(back, emb, atol=1e-6)


def test_visit_and_unknown_face(session):
    u = db.UnknownFace(temp_uid="UNK-20260801-0001", embedding=b"\x00" * 2048,
                       first_seen=db.now_local(), last_seen=db.now_local())
    session.add(u)
    session.flush()
    v = db.Visit(unknown_id=u.id, camera_id=None, first_seen=db.now_local(),
                 last_seen=db.now_local(), status="unknown")
    session.add(v)
    session.commit()
    assert session.query(db.Visit).one().status == "unknown"


def test_schema_is_idempotent(tmp_path):
    db.init_db(tmp_path / "twice.db")
    db.init_db(tmp_path / "twice.db")   # re-running must not fail


def test_camera_delete_with_visits_detaches_them(session):
    cam = db.Camera(name="ટેસ્ટ", source_type="webcam", source_url="0")
    session.add(cam)
    session.flush()
    v = db.Visit(camera_id=cam.id, first_seen=db.now_local(),
                 last_seen=db.now_local(), status="known")
    session.add(v)
    session.commit()
    # same sequence the DELETE /api/cameras/{id} route runs
    session.query(db.Visit).filter(db.Visit.camera_id == cam.id) \
        .update({"camera_id": None})
    session.delete(cam)
    session.commit()
    assert session.query(db.Camera).count() == 0
    kept = session.query(db.Visit).one()
    assert kept.camera_id is None


def test_person_delete_with_task_and_unknown_refs(session):
    p = db.Person(full_name="જવાનાર")
    session.add(p)
    session.flush()
    u = db.UnknownFace(temp_uid="UNK-X", embedding=b"\x00" * 2048,
                       first_seen=db.now_local(), last_seen=db.now_local(),
                       resolved_person_id=p.id)
    session.add(u)
    session.add(db.Task(title="કામ", assignee_id=p.id))
    session.add(db.Conversation(person_id=p.id, channel="voice",
                                user_text="x", agent_text="y"))
    session.commit()
    # same detach sequence DELETE /api/persons/{id} runs
    session.query(db.UnknownFace) \
        .filter(db.UnknownFace.resolved_person_id == p.id) \
        .update({"resolved_person_id": None})
    session.query(db.Task).filter(db.Task.assignee_id == p.id) \
        .update({"assignee_id": None})
    session.query(db.Conversation).filter(db.Conversation.person_id == p.id) \
        .update({"person_id": None})
    session.delete(p)
    session.commit()
    assert session.query(db.Person).count() == 0
    assert session.query(db.Task).one().assignee_id is None


def test_mic_stays_active_while_paused():
    from core.state import SystemState
    st = SystemState()
    st.set("paused")
    assert st.mic_active          # voice resume must be possible
    st.set("mute_mic")
    assert not st.mic_active
