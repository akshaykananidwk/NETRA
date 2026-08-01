"""Greeting flows — cooldown, ask-limit, and name capture (no audio hardware)."""
import asyncio
from datetime import timedelta

import pytest

import core.db as db
from agent.greeter import Greeter
from config import settings
from core.state import SystemState


class FakeAudio:
    def __init__(self):
        self.window_open = False

    def open_reply_window(self, seconds, context=None):
        self.window_open = True

    def close_reply_window(self):
        self.window_open = False


class FakeOrch:
    def __init__(self):
        self.activity = []
        self.broadcasts = []
        self.hub = self

    def _log_activity(self, icon, text):
        self.activity.append((icon, text))

    async def broadcast(self, type, data):
        self.broadcasts.append((type, data))


def make_greeter(say_results=None):
    g = Greeter(tts=None, speaker=None, state=SystemState(),
                audio_worker=FakeAudio())
    orch = FakeOrch()
    g.orch = orch
    g.spoken = []
    results = say_results or {}

    async def fake_say(text):
        g.spoken.append(text)
        return results.get("ok", True)
    g.say = fake_say
    return g, orch


@pytest.fixture()
def database(tmp_path):
    db.init_db(tmp_path / "greet.db")
    with db.SessionLocal() as s:
        s.add(db.Person(id=1, full_name="Ramesh", call_name="રમેશભાઈ",
                        relation_type="visitor", greeting_enabled=1))
        s.add(db.Person(id=2, full_name="Silent", relation_type="visitor",
                        greeting_enabled=0))
        s.add(db.Camera(id=1, name="Reception", source_type="webcam",
                        source_url="0", greet_here=1))
        s.add(db.Camera(id=2, name="Store", source_type="webcam",
                        source_url="1", greet_here=0))
        s.commit()


def add_visit(person_id=1, camera_id=1, greeted=0, hours_ago=0.0):
    with db.SessionLocal() as s:
        v = db.Visit(person_id=person_id, camera_id=camera_id,
                     first_seen=db.now_local() - timedelta(hours=hours_ago),
                     last_seen=db.now_local() - timedelta(hours=hours_ago),
                     greeted=greeted, status="known")
        s.add(v)
        s.commit()
        return v.id


def add_unknown(uid="UNK-20260801-0001", ask_count=0):
    with db.SessionLocal() as s:
        s.add(db.UnknownFace(temp_uid=uid, embedding=b"\x00" * 2048,
                             first_seen=db.now_local(), last_seen=db.now_local(),
                             ask_count=ask_count, status="pending"))
        s.commit()


# ── Flow B: known ─────────────────────────────────────────────────────────

def test_known_person_is_greeted_and_visit_marked(database):
    g, orch = make_greeter()
    vid = add_visit()
    asyncio.run(g.on_known({"person_id": 1, "camera_id": 1, "visit_id": vid}))
    assert any("રમેશભાઈ" in t for t in g.spoken)
    with db.SessionLocal() as s:
        assert s.get(db.Visit, vid).greeted == 1


def test_cooldown_blocks_second_greeting(database):
    g, _ = make_greeter()
    add_visit(greeted=1, hours_ago=1.0)          # greeted an hour ago
    vid = add_visit()
    asyncio.run(g.on_known({"person_id": 1, "camera_id": 1, "visit_id": vid}))
    assert g.spoken == []


def test_greeting_again_after_cooldown_expires(database):
    g, _ = make_greeter()
    add_visit(greeted=1, hours_ago=settings.greet_cooldown_hours + 1)
    vid = add_visit()
    asyncio.run(g.on_known({"person_id": 1, "camera_id": 1, "visit_id": vid}))
    assert len(g.spoken) == 1


def test_greeting_disabled_person_is_silent(database):
    g, _ = make_greeter()
    vid = add_visit(person_id=2)
    asyncio.run(g.on_known({"person_id": 2, "camera_id": 1, "visit_id": vid}))
    assert g.spoken == []


def test_no_greeting_on_non_greet_camera(database):
    g, _ = make_greeter()
    vid = add_visit(camera_id=2)
    asyncio.run(g.on_known({"person_id": 1, "camera_id": 2, "visit_id": vid}))
    assert g.spoken == []


def test_paused_state_is_silent(database):
    g, _ = make_greeter()
    g.state.set("paused")
    vid = add_visit()
    asyncio.run(g.on_known({"person_id": 1, "camera_id": 1, "visit_id": vid}))
    assert g.spoken == []


# ── Flow A: unknown ───────────────────────────────────────────────────────

def test_unknown_is_asked_and_ask_count_increments(database):
    g, orch = make_greeter()
    add_unknown()

    async def scenario():
        await g.on_unknown({"temp_uid": "UNK-20260801-0001", "camera_id": 1})
    asyncio.run(scenario())
    assert any("આપનું નામ" in t for t in g.spoken)
    assert g.awaiting["temp_uid"] == "UNK-20260801-0001"
    assert g.audio.window_open
    with db.SessionLocal() as s:
        assert (s.query(db.UnknownFace).one()).ask_count == 1


def test_ask_limit_reached_stays_silent(database):
    g, _ = make_greeter()
    add_unknown(ask_count=settings.unknown_ask_max_per_day)
    # marker must say "today" so counts are NOT reset by the daily check
    with db.SessionLocal() as s:
        s.add(db.Setting(key="unknown_ask_reset_date", value=db.today_str()))
        s.commit()
    asyncio.run(g.on_unknown({"temp_uid": "UNK-20260801-0001", "camera_id": 1}))
    assert g.spoken == []


def test_stale_ask_counts_reset_on_new_day(database):
    g, _ = make_greeter()
    add_unknown(ask_count=99)
    with db.SessionLocal() as s:                 # marker from yesterday
        s.add(db.Setting(key="unknown_ask_reset_date", value="2020-01-01"))
        s.commit()
    asyncio.run(g.on_unknown({"temp_uid": "UNK-20260801-0001", "camera_id": 1}))
    assert len(g.spoken) == 1                    # reset → asked again


def test_reply_saves_spoken_name_and_thanks(database):
    g, orch = make_greeter()
    add_unknown()

    async def scenario():
        await g.on_unknown({"temp_uid": "UNK-20260801-0001", "camera_id": 1})
        await g.on_transcript({"text": "મારું નામ રમેશ પટેલ છે"})
    asyncio.run(scenario())
    with db.SessionLocal() as s:
        assert s.query(db.UnknownFace).one().spoken_name == "રમેશ પટેલ"
    assert any("આભાર રમેશભાઈ" in t for t in g.spoken)
    assert g.awaiting is None
    assert not g.audio.window_open
    assert any(t == "unknown.updated" for t, _ in orch.broadcasts)


def test_transcript_without_awaiting_is_ignored(database):
    g, _ = make_greeter()
    asyncio.run(g.on_transcript({"text": "કંઈક બોલ્યા"}))
    assert g.spoken == []


def test_say_failure_means_no_ask_count_bump(database):
    g, _ = make_greeter(say_results={"ok": False})
    add_unknown()
    asyncio.run(g.on_unknown({"temp_uid": "UNK-20260801-0001", "camera_id": 1}))
    assert g.awaiting is None
    with db.SessionLocal() as s:
        assert s.query(db.UnknownFace).one().ask_count == 0
