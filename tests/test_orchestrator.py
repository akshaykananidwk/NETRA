"""Orchestrator integration — bus events land in the visits table."""
import asyncio

import pytest

import core.db as db
from core import events as ev
from core.events import EventBus
from orchestrator import Orchestrator


class StubHub:
    def __init__(self):
        self.sent = []

    async def broadcast(self, type, data):
        self.sent.append((type, data))


@pytest.fixture()
def database(tmp_path):
    db.init_db(tmp_path / "orch.db")
    with db.SessionLocal() as s:
        s.add(db.Person(full_name="AK Bhai", relation_type="admin", is_admin=1))
        s.add(db.Camera(name="Reception", source_type="webcam", source_url="0"))
        s.commit()


def _run_with_events(events, settle=0.4):
    hub = StubHub()

    async def scenario():
        bus = EventBus()
        orch = Orchestrator(bus, hub)
        bus.attach_loop(asyncio.get_running_loop())
        task = asyncio.create_task(orch.run())
        await asyncio.sleep(0.05)
        for etype, data in events:
            bus.publish(etype, **data)
            await asyncio.sleep(0.05)
        await asyncio.sleep(settle)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return orch

    return asyncio.run(scenario()), hub


def test_recognized_creates_one_merged_visit(database):
    sighting = {"camera_id": 1, "camera_name": "Reception", "person_id": 1,
                "name": "AK Bhai", "confidence": 0.91, "snapshot_path": None,
                "track_id": 1}
    orch, hub = _run_with_events([
        (ev.FACE_RECOGNIZED, sighting),
        (ev.FACE_RECOGNIZED, {**sighting, "track_id": 2}),  # walked out + back in
    ])
    with db.SessionLocal() as s:
        visits = s.query(db.Visit).all()
        assert len(visits) == 1              # merged within the window
        assert visits[0].person_id == 1
        assert visits[0].status == "known"
    assert any(t == ev.FACE_RECOGNIZED for t, _ in hub.sent)
    assert any("AK Bhai" in a["text"] for a in orch.activity)


def test_unknown_event_persists_face_and_visit(database):
    import numpy as np
    emb = np.random.default_rng(3).normal(size=512).astype(np.float32).tobytes()
    orch, hub = _run_with_events([
        (ev.FACE_UNKNOWN, {"camera_id": 1, "camera_name": "Reception",
                           "temp_uid": "UNK-20260801-0001", "is_new": True,
                           "snapshot_path": None, "embedding": emb,
                           "track_id": 5}),
    ])
    with db.SessionLocal() as s:
        u = s.query(db.UnknownFace).one()
        assert u.temp_uid == "UNK-20260801-0001"
        assert u.status == "pending"
        v = s.query(db.Visit).one()
        assert v.unknown_id == u.id
        assert v.status == "unknown"
    # broadcast must not leak the raw embedding to browsers
    unknown_msgs = [d for t, d in hub.sent if t == ev.FACE_UNKNOWN]
    assert unknown_msgs and unknown_msgs[0]["embedding"] is None


def test_paused_state_suppresses_visit_logging(database):
    async def scenario():
        bus = EventBus()
        orch = Orchestrator(bus, StubHub())
        orch.state.set("paused")
        bus.attach_loop(asyncio.get_running_loop())
        task = asyncio.create_task(orch.run())
        await asyncio.sleep(0.05)
        bus.publish(ev.FACE_RECOGNIZED, camera_id=1, camera_name="Reception",
                    person_id=1, name="AK Bhai", confidence=0.9,
                    snapshot_path=None, track_id=1)
        await asyncio.sleep(0.3)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())
    with db.SessionLocal() as s:
        assert s.query(db.Visit).count() == 0
