"""Agent tools — permission gate, handlers, Gujarati confirmations."""
from datetime import timedelta

import pytest

import core.db as db
from agent.tools import DENIED_MSG, TOOLS, ToolExecutor
from core.db import now_local
from core.state import SystemState


class FakeWhatsApp:
    def __init__(self, ok=True):
        self.ok = ok
        self.sent = []

    @property
    def configured(self):
        return True

    def send_text(self, to, message, purpose="x"):
        self.sent.append({"to": to, "message": message, "purpose": purpose})
        return self.ok


class FakeReminders:
    def __init__(self):
        self.created = []

    def create(self, **kw):
        self.created.append(kw)
        return {"id": 1, **{k: str(v) for k, v in kw.items()}}


ADMIN = {"person_id": 1, "name": "AK Bhai", "role": "admin"}
STAFF = {"person_id": 2, "name": "Ramesh", "role": "staff"}
VISITOR = {"person_id": None, "name": None, "role": "unknown"}


@pytest.fixture()
def executor(tmp_path, monkeypatch):
    db.init_db(tmp_path / "tools.db")
    from config import settings
    monkeypatch.setattr(settings, "wa_canteen_number", "9199999")
    wa = FakeWhatsApp()
    ex = ToolExecutor(whatsapp=wa, reminders=FakeReminders(),
                      state=SystemState())
    ex._wa = wa
    return ex


def test_tool_definitions_are_well_formed():
    names = set()
    for t in TOOLS:
        assert t["name"] not in names
        names.add(t["name"])
        assert t["input_schema"]["type"] == "object"
    # every defined tool has a role assignment and a handler
    from agent.tools import TOOL_ROLES
    for t in TOOLS:
        assert t["name"] in TOOL_ROLES
        assert hasattr(ToolExecutor, f"_t_{t['name']}"), t["name"]


def test_permission_gate_blocks_non_admin(executor):
    out = executor.execute("get_collection_report", {}, VISITOR)
    assert out == DENIED_MSG
    out = executor.execute("get_visitor_log", {}, STAFF)
    assert out == DENIED_MSG
    out = executor.execute("create_task", {"title": "x"}, VISITOR)
    assert out == DENIED_MSG


def test_anyone_can_order_tea_and_whatsapp_goes_to_canteen(executor):
    out = executor.execute("order_refreshment",
                           {"items": [{"item": "chai", "qty": 2}]}, VISITOR)
    assert "ચા" in out and "મંગાવી" in out
    assert executor._wa.sent[0]["to"] == "9199999"
    assert "2 ચા" in executor._wa.sent[0]["message"]


def test_order_queued_message_when_whatsapp_down(executor):
    executor._wa.ok = False
    executor.whatsapp.ok = False
    out = executor.execute("order_refreshment",
                           {"items": [{"item": "coffee", "qty": 1}]}, ADMIN)
    assert "ઓફલાઇન" in out


def test_create_task_and_list(executor):
    out = executor.execute("create_task",
                           {"title": "રમેશને ફોન", "priority": "high"}, STAFF)
    assert "નોંધી" in out
    listing = executor.execute("list_tasks", {"date_range": "all"}, STAFF)
    assert "રમેશને ફોન" in listing


def test_create_reminder_validates_time(executor):
    when = (now_local() + timedelta(hours=2)).isoformat()
    out = executor.execute("create_reminder",
                           {"message": "ફોન કરવો", "remind_at": when}, ADMIN)
    assert "રિમાઇન્ડર સેટ" in out
    assert executor.reminders.created[0]["message"] == "ફોન કરવો"
    bad = executor.execute("create_reminder",
                           {"message": "x", "remind_at": "કાલે"}, ADMIN)
    assert "સમજાયો નહીં" in bad or "સમય" in bad


def test_set_listening_state_pauses(executor):
    out = executor.execute("set_listening_state", {"state": "paused"}, ADMIN)
    assert executor.state.state == "paused"
    assert "થોભી" in out
    executor.execute("set_listening_state", {"state": "active"}, ADMIN)
    assert executor.state.state == "active"


def test_collection_report_reads_setting(executor):
    from core.db import Setting, SessionLocal, today_str
    out = executor.execute("get_collection_report", {}, ADMIN)
    assert "નોંધાયું નથી" in out
    with SessionLocal() as s:
        s.add(Setting(key=f"collection:{today_str()}", value="45000"))
        s.commit()
    out = executor.execute("get_collection_report", {}, ADMIN)
    assert "45000" in out


def test_visitor_log_counts(executor):
    with db.SessionLocal() as s:
        p = db.Person(full_name="Suresh", call_name="સુરેશભાઈ")
        s.add(p)
        s.flush()
        s.add(db.Visit(person_id=p.id, camera_id=None,
                       first_seen=now_local(), last_seen=now_local()))
        s.commit()
    out = executor.execute("get_visitor_log", {}, ADMIN)
    assert "1 મુલાકાત" in out and "સુરેશભાઈ" in out


def test_meetings_start_end(executor):
    out = executor.execute("start_meeting", {}, ADMIN)
    assert "ચાલુ" in out
    again = executor.execute("start_meeting", {}, ADMIN)
    assert "પહેલેથી" in again
    out = executor.execute("end_meeting", {}, ADMIN)
    assert "પૂરી" in out


def test_unknown_tool_and_crash_are_graceful(executor):
    assert "નથી આવડતું" in executor.execute("no_such_tool", {}, ADMIN)
    # invalid input shape → validation error → graceful Gujarati
    out = executor.execute("order_refreshment", {"items": "ચા"}, ADMIN)
    assert "માફ કરશો" in out
