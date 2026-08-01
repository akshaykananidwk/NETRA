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
