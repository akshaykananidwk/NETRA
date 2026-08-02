"""Meeting mode — recording lifecycle, speaker labels, summary → tasks."""
import asyncio
import time
from datetime import timedelta

import numpy as np
import pytest

import core.db as db
from agent.meeting import MeetingManager, SpeakerLabeler
from core.db import now_local


class FakeAudio:
    def __init__(self):
        self.recording = False

    def start_meeting_recording(self, path):
        self.recording = True
        return True

    def stop_meeting_recording(self):
        self.recording = False


class FakeGreeter:
    def __init__(self):
        self.spoken = []

    async def say(self, text):
        self.spoken.append(text)
        return True


class FakeWhatsApp:
    def __init__(self):
        self.sent = []

    def send_text(self, to, message, purpose="x"):
        self.sent.append({"to": to, "message": message, "purpose": purpose})
        return True


class FakeReminders:
    def __init__(self):
        self.created = []

    def create(self, **kw):
        self.created.append(kw)
        return {"id": len(self.created)}


class FakeOrch:
    def __init__(self):
        self.activity = []
        self.hub = self
        self.state = None

    def present_names(self):
        return []

    def _log_activity(self, icon, text):
        self.activity.append(text)

    async def broadcast(self, type, data):
        pass


@pytest.fixture()
def manager(tmp_path, monkeypatch):
    db.init_db(tmp_path / "meet.db")
    from config import settings
    monkeypatch.setattr(settings, "meetings_dir", tmp_path / "meetings")
    monkeypatch.setattr(settings, "wa_admin_number", "9198888")
    m = MeetingManager(audio_worker=FakeAudio(), brain=None,
                       whatsapp=FakeWhatsApp(), reminders=FakeReminders(),
                       greeter=FakeGreeter(), bus=None)
    m.orch = FakeOrch()
    return m


def unit(seed):
    rng = np.random.default_rng(seed)
    v = rng.normal(size=192).astype(np.float32)
    return v / np.linalg.norm(v)


# ── speaker labeler ───────────────────────────────────────────────────────

def test_labeler_prefers_known_name():
    lab = SpeakerLabeler()
    assert lab.label(unit(1), "AK Bhai") == "AK Bhai"


def test_labeler_clusters_same_voice():
    lab = SpeakerLabeler()
    v = unit(1)
    assert lab.label(v, None) == "SPK_1"
    assert lab.label(v, None) == "SPK_1"          # same voice → same label
    assert lab.label(unit(2), None) == "SPK_2"    # different voice → new


def test_labeler_no_embedding():
    assert SpeakerLabeler().label(None, None) == "SPK_?"


# ── lifecycle ─────────────────────────────────────────────────────────────

def test_start_records_and_announces(manager):
    async def go():
        reply = await manager.start("સેલ્સ મીટિંગ")
        assert manager.active
        assert "ચાલુ" in reply
        # DPDP announcement spoken
        assert any("રેકોર્ડિંગ ચાલુ" in t for t in manager.greeter.spoken)
        again = await manager.start()
        assert "પહેલેથી" in again
        if manager._watchdog:
            manager._watchdog.cancel()
    asyncio.run(go())
    with db.SessionLocal() as s:
        m = s.query(db.Meeting).one()
        assert m.status == "recording"
        assert m.audio_path == f"meetings/{m.id}/audio.wav"
    assert manager.audio.recording


def test_transcript_segments_stored_with_labels(manager):
    async def go():
        await manager.start()
        v = unit(7)
        await manager.on_transcript({"text": "ભાવ વધારવો પડશે",
                                     "speaker_emb": v.tobytes(),
                                     "duration_sec": 2.0})
        await manager.on_transcript({"text": "હા બરાબર છે",
                                     "speaker_emb": v.tobytes(),
                                     "duration_sec": 1.0})
        await manager.on_transcript({"text": "હું નોંધી લઉં",
                                     "speaker_name": "AK Bhai",
                                     "speaker_person_id": 99,
                                     "duration_sec": 1.0})
        if manager._watchdog:
            manager._watchdog.cancel()
    asyncio.run(go())
    with db.SessionLocal() as s:
        rows = (s.query(db.TranscriptSegment)
                 .order_by(db.TranscriptSegment.id).all())
        assert [r.speaker_label for r in rows] == ["SPK_1", "SPK_1", "AK Bhai"]
        assert rows[0].text == "ભાવ વધારવો પડશે"


def test_participants_from_faces(manager, tmp_path):
    with db.SessionLocal() as s:
        s.add(db.Person(id=5, full_name="Ramesh"))
        s.commit()

    async def go():
        await manager.start()
        await manager.on_face(5)
        await manager.on_face(5)          # dedup
        if manager._watchdog:
            manager._watchdog.cancel()
    asyncio.run(go())
    with db.SessionLocal() as s:
        rows = s.query(db.MeetingParticipant).all()
        assert len(rows) == 1
        assert rows[0].detected_by == "face"


def test_end_summarizes_creates_tasks_reminders_whatsapp(manager, monkeypatch):
    due = (now_local() + timedelta(days=3)).replace(microsecond=0)
    fake_summary = {
        "summary": "ભાવ અંગે ચર્ચા થઈ.",
        "key_points": ["ભાવ વધારો"],
        "decisions": ["સોમવારથી નવો ભાવ"],
        "tasks": [{"title": "રમેશને નવો ભાવ મોકલવો",
                   "assignee": None,
                   "due_at": due.isoformat(), "priority": "high"}],
        "followups": [], "sentiment": "positive",
    }

    async def fake_summarize(mid):
        return fake_summary
    monkeypatch.setattr(manager, "_summarize", fake_summarize)

    async def go():
        await manager.start()
        await manager.on_transcript({"text": "ભાવ વધારવો પડશે",
                                     "duration_sec": 1.0})
        reply = await manager.end()
        assert "પૂરી" in reply
        await asyncio.sleep(0.3)          # background summariser task
    asyncio.run(go())

    with db.SessionLocal() as s:
        m = s.query(db.Meeting).one()
        assert m.status == "done"
        assert m.summary == "ભાવ અંગે ચર્ચા થઈ."
        t = s.query(db.Task).one()
        assert t.source == "meeting"
        assert t.meeting_id == m.id
    # auto reminder at due −1 day, 10:00
    r = manager.reminders.created[0]
    assert r["remind_at"].hour == 10
    assert r["remind_at"].date() == (due - timedelta(days=1)).date()
    # WhatsApp summary to admin
    wa = manager.whatsapp.sent[0]
    assert wa["to"] == "9198888"
    assert "ભાવ અંગે ચર્ચા થઈ" in wa["message"]
    # spoken confirmation with task count
    assert any("1 ટાસ્ક" in t for t in manager.greeter.spoken)
    assert not manager.audio.recording


def test_failed_summary_keeps_transcript(manager, monkeypatch):
    async def fake_summarize(mid):
        return None
    monkeypatch.setattr(manager, "_summarize", fake_summarize)

    async def go():
        await manager.start()
        await manager.on_transcript({"text": "કંઈક વાત", "duration_sec": 1.0})
        await manager.end()
        await asyncio.sleep(0.3)
    asyncio.run(go())
    with db.SessionLocal() as s:
        m = s.query(db.Meeting).one()
        assert m.status == "done"          # transcript is still usable
        assert m.summary is None
        assert s.query(db.TranscriptSegment).count() == 1


def test_end_without_meeting(manager):
    async def go():
        return await manager.end()
    assert "કોઈ મીટિંગ ચાલુ નથી" in asyncio.run(go())
