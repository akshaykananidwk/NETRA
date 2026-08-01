"""Reminder engine — persistence, firing, channels."""
import time
from datetime import timedelta

import pytest

import core.db as db
from actions.reminders import ReminderEngine, todays_reminders
from core.db import now_local


class FakeWhatsApp:
    def __init__(self):
        self.sent = []

    def send_text(self, to, message, purpose="x"):
        self.sent.append({"to": to, "message": message})
        return True


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    db.init_db(tmp_path / "rem.db")
    from config import settings
    monkeypatch.setattr(settings, "wa_admin_number", "9198888")
    spoken = []
    e = ReminderEngine(whatsapp=FakeWhatsApp(), bus=None,
                       say_callback=lambda t: spoken.append(t))
    e._spoken = spoken
    yield e
    e.stop()


def test_create_persists_row(engine):
    out = engine.create("ફોન કરવો", now_local() + timedelta(hours=1))
    assert out["status"] == "scheduled"
    assert todays_reminders()[0]["message"] == "ફોન કરવો"


def test_fire_voice_reminder(engine):
    out = engine.create("ચા પીવાની", now_local() + timedelta(hours=1),
                        channel="voice")
    engine.fire(out["id"])
    assert engine._spoken and "ચા પીવાની" in engine._spoken[0]
    with db.SessionLocal() as s:
        assert s.get(db.Reminder, out["id"]).status == "sent"


def test_fire_whatsapp_reminder_to_admin(engine):
    out = engine.create("બિલ ભરવાનું", now_local() + timedelta(hours=1),
                        channel="whatsapp")
    engine.fire(out["id"])
    assert engine.whatsapp.sent[0]["to"] == "9198888"
    assert "બિલ ભરવાનું" in engine.whatsapp.sent[0]["message"]


def test_fire_all_channels(engine):
    out = engine.create("મીટિંગ", now_local() + timedelta(minutes=5),
                        channel="all")
    engine.fire(out["id"])
    assert engine._spoken and engine.whatsapp.sent


def test_cancelled_reminder_never_fires(engine):
    out = engine.create("રદ થયેલું", now_local() + timedelta(hours=1))
    assert engine.cancel(out["id"]) is True
    engine.fire(out["id"])
    assert engine._spoken == []
    with db.SessionLocal() as s:
        assert s.get(db.Reminder, out["id"]).status == "cancelled"


def test_scheduler_rearms_and_fires_due_reminder(engine):
    # a reminder already past-due in the DB (as after a restart)
    out = engine.create("મોડું થયેલું", now_local() - timedelta(minutes=5))
    engine.start()                     # re-arm: past-due fires immediately
    deadline = time.time() + 5
    while time.time() < deadline and not engine._spoken:
        time.sleep(0.1)
    assert engine._spoken and "મોડું થયેલું" in engine._spoken[0]
