"""Brain fallback chain + commander wake-word flow (no network, no LLM)."""
import asyncio

import pytest

import core.db as db
from agent.brain import AgentBrain, FALLBACK_REPLY
from agent.commander import Commander, strip_wake_word
from agent.memory import ConversationMemory
from agent.tools import ToolExecutor
from core.state import SystemState

ADMIN = {"person_id": 1, "name": "AK Bhai", "role": "admin"}


class FakeWhatsApp:
    configured = True

    def __init__(self):
        self.sent = []

    def send_text(self, to, message, purpose="x"):
        self.sent.append((to, message))
        return True


@pytest.fixture()
def brain(tmp_path, monkeypatch):
    db.init_db(tmp_path / "brain.db")
    from config import settings
    monkeypatch.setattr(settings, "anthropic_api_key", "")   # no cloud
    monkeypatch.setattr(settings, "wa_canteen_number", "9199999")
    wa = FakeWhatsApp()
    ex = ToolExecutor(whatsapp=wa, reminders=None, state=SystemState())
    b = AgentBrain(ex, ConversationMemory())
    b._wa = wa

    # ollama unreachable
    def fail(*a, **k):
        raise ConnectionError("no ollama")
    monkeypatch.setattr("agent.brain._requests.post", fail)
    return b


def test_rules_fallback_actually_sends_tea_whatsapp(brain):
    """The 'done when' test — no LLM at all, tea order still works."""
    result = asyncio.run(brain.process("બે ચા મંગાવ", ADMIN))
    assert result["provider"] in ("rules", "ollama")
    assert "ચા" in result["reply"]
    assert brain._wa.sent[0][0] == "9199999"
    assert "2 ચા" in brain._wa.sent[0][1]


def test_unmatched_text_gets_apology_not_crash(brain):
    result = asyncio.run(brain.process("કંઈક અજુગતું અગડમ બગડમ", ADMIN))
    assert result["reply"] == FALLBACK_REPLY
    assert result["provider"] == "rules"


def test_conversation_is_logged(brain):
    asyncio.run(brain.process("બે ચા મંગાવ", ADMIN))
    with db.SessionLocal() as s:
        row = s.query(db.Conversation).first()
        assert row is not None
        assert "ચા" in row.user_text


def test_memory_keeps_turns(brain):
    asyncio.run(brain.process("બે ચા મંગાવ", ADMIN))
    key = brain.memory.key_for(ADMIN, "voice")
    msgs = brain.memory.messages(key)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"


# ── wake word ─────────────────────────────────────────────────────────────

def test_strip_wake_word_variants():
    assert strip_wake_word("કૃષ્ણ, બે ચા મંગાવ") == "બે ચા મંગાવ"
    assert strip_wake_word("Krishna list my tasks") == "list my tasks"
    assert strip_wake_word("એ કૃષ્ણ મીટિંગ ચાલુ કર") == "મીટિંગ ચાલુ કર"
    assert strip_wake_word("આજે વરસાદ છે") is None
    assert strip_wake_word("કૃષ્ણ") == ""


class FakeGreeter:
    def __init__(self):
        self.spoken = []

    async def say(self, text):
        self.spoken.append(text)
        return True


class FakeOrch:
    def __init__(self):
        self.last_admin_seen = 0.0
        self.last_admin_person_id = None
        self.last_admin_name = None
        self.activity = []
        self.hub = self

    def admin_recently_seen(self):
        import time
        return (time.time() - self.last_admin_seen) < 30

    def _log_activity(self, icon, text):
        self.activity.append(text)

    async def broadcast(self, type, data):
        pass


def test_commander_requires_wake_word(brain):
    g = FakeGreeter()
    c = Commander(brain, g, SystemState())
    c.attach(FakeOrch())
    asyncio.run(c.on_transcript({"text": "આજે ગરમી બહુ છે"}))
    assert g.spoken == []                       # ordinary chatter ignored


def test_commander_executes_wake_command_with_admin_face(brain):
    import time
    g = FakeGreeter()
    c = Commander(brain, g, SystemState())
    orch = FakeOrch()
    orch.last_admin_seen = time.time()          # admin on camera just now
    orch.last_admin_person_id = 1
    orch.last_admin_name = "AK Bhai"
    c.attach(orch)
    asyncio.run(c.on_transcript({"text": "કૃષ્ણ, બે ચા મંગાવ"}))
    assert any("ચા" in t for t in g.spoken)
    assert brain._wa.sent                        # WhatsApp really fired


def test_commander_follow_up_window(brain):
    import time
    g = FakeGreeter()
    c = Commander(brain, g, SystemState())
    orch = FakeOrch()
    orch.last_admin_seen = time.time()
    c.attach(orch)
    asyncio.run(c.on_transcript({"text": "કૃષ્ણ"}))       # just the wake word
    assert g.spoken and "બોલો" in g.spoken[0]
    # follow-up without wake word within the window
    asyncio.run(c.on_transcript({"text": "બે ચા મંગાવ"}))
    assert any("ચા" in t for t in g.spoken[1:])


def test_commander_paused_only_resumes(brain):
    g = FakeGreeter()
    state = SystemState()
    state.set("paused")
    c = Commander(brain, g, state)
    c.attach(FakeOrch())
    asyncio.run(c.on_transcript({"text": "કૃષ્ણ, બે ચા મંગાવ"}))
    assert g.spoken == []                        # commands ignored while paused
    asyncio.run(c.on_transcript({"text": "કૃષ્ણ ચાલુ થા"}))
    assert state.state == "active"


def test_web_command_is_admin(brain):
    g = FakeGreeter()
    c = Commander(brain, g, SystemState())
    c.attach(FakeOrch())
    result = asyncio.run(c.on_web_command("આજનું કલેક્શન કેટલું છે?"))
    # admin-only tool answered (not the permission-denied message)
    from agent.tools import DENIED_MSG
    assert result["reply"] != DENIED_MSG


def test_ollama_tool_calling_loop(brain, monkeypatch):
    """Local model with function calling executes tools end-to-end."""
    from config import settings
    calls = {"n": 0}

    class FakeResp:
        def __init__(self, data):
            self._data = data
            self.status_code = 200
        def raise_for_status(self):
            pass
        def json(self):
            return self._data

    def fake_post(url, json=None, timeout=None):
        calls["n"] += 1
        assert "tools" in json          # tool definitions passed to ollama
        if calls["n"] == 1:
            return FakeResp({"message": {
                "role": "assistant", "content": "",
                "tool_calls": [{"function": {
                    "name": "order_refreshment",
                    "arguments": {"items": [{"item": "chai", "qty": 2}]}}}]}})
        return FakeResp({"message": {"role": "assistant",
                                     "content": "ચા મંગાવી દીધી છે."}})

    monkeypatch.setattr("agent.brain._requests.post", fake_post)
    # bypass the rules shortcut so the LLM path is exercised
    monkeypatch.setattr("agent.brain.match_intent", lambda t: None)
    result = asyncio.run(brain.process("મહેમાન માટે કંઈક ગરમ મંગાવો", ADMIN))
    assert result["provider"] == "ollama"
    assert result["tool_calls"][0]["name"] == "order_refreshment"
    assert brain._wa.sent                # WhatsApp really fired
    assert "ચા" in result["reply"]
