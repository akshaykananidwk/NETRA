"""Rule-based intent matcher — the offline fallback for common commands."""
from agent.rules import match_intent


def test_tea_order_with_gujarati_number():
    name, inp = match_intent("બે ચા મંગાવ")
    assert name == "order_refreshment"
    assert inp["items"][0] == {"item": "chai", "qty": 2}


def test_coffee_with_digit():
    name, inp = match_intent("3 કોફી મોકલજો")
    assert name == "order_refreshment"
    assert inp["items"][0]["item"] == "coffee"
    assert inp["items"][0]["qty"] == 3


def test_pause_and_resume():
    assert match_intent("બંધ થઈ જા")[0] == "set_listening_state"
    assert match_intent("બંધ થઈ જા")[1]["state"] == "paused"
    assert match_intent("ચાલુ થા")[1]["state"] == "active"


def test_meeting_commands():
    assert match_intent("મીટિંગ ચાલુ કર")[0] == "start_meeting"
    assert match_intent("મીટિંગ પૂરી કરી")[0] == "end_meeting"


def test_visitor_and_collection_queries():
    assert match_intent("આજે કેટલા લોકો આવ્યા?")[0] == "get_visitor_log"
    assert match_intent("આજનું કલેક્શન કેટલું છે?")[0] == "get_collection_report"


def test_task_listing():
    name, inp = match_intent("આજના બાકી કામ કયા છે?")
    assert name == "list_tasks"
    assert inp["date_range"] == "today"


def test_reminder_gets_default_slot():
    name, inp = match_intent("રમેશને ફોન કરવાનું યાદ કરાવજે")
    assert name == "create_reminder"
    assert "રમેશ" in inp["message"]
    assert inp["remind_at"]        # ISO string present


def test_smalltalk_returns_none():
    assert match_intent("કેમ છો ભાઈ") is None
    assert match_intent("hello there") is None
