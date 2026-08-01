"""WhatsApp client — queueing, retries, flush, rate limiting (all mocked)."""
import pytest

import core.db as db
from actions.whatsapp import RETRIES, WhatsAppClient


class FakeResponse:
    def __init__(self, status=200, text="OK"):
        self.status_code = status
        self.text = text
        self.ok = status < 400


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db.init_db(tmp_path / "wa.db")
    from config import settings
    monkeypatch.setattr(settings, "wa_api_key", "test-key")
    monkeypatch.setattr(settings, "wa_api_base", "https://wa.example/api")
    c = WhatsAppClient(queue_path=tmp_path / "queue.json")
    monkeypatch.setattr("actions.whatsapp.RATE_LIMIT_SEC", 0)
    import actions.whatsapp as wa_mod
    monkeypatch.setattr(wa_mod.time, "sleep", lambda s: None)
    return c


def test_send_success_logs_to_db(client, monkeypatch):
    calls = []
    monkeypatch.setattr("actions.whatsapp.requests.post",
                        lambda *a, **k: calls.append(k) or FakeResponse())
    assert client.send_text("9198765", "નમસ્તે", purpose="test") is True
    assert calls[0]["json"]["number"] == "9198765"
    with db.SessionLocal() as s:
        row = s.query(db.WhatsAppLog).one()
        assert row.status == "sent"
        assert row.purpose == "test"


def test_network_failure_queues_and_flush_delivers(client, monkeypatch):
    import requests as req

    def fail(*a, **k):
        raise req.ConnectionError("offline")
    monkeypatch.setattr("actions.whatsapp.requests.post", fail)
    assert client.send_text("9198765", "queued msg") is False
    assert client.queue_size() == 1

    # network returns
    monkeypatch.setattr("actions.whatsapp.requests.post",
                        lambda *a, **k: FakeResponse())
    assert client.flush_queue() == 1
    assert client.queue_size() == 0


def test_client_error_is_not_retried(client, monkeypatch):
    attempts = []
    monkeypatch.setattr(
        "actions.whatsapp.requests.post",
        lambda *a, **k: attempts.append(1) or FakeResponse(status=401,
                                                           text="bad key"))
    client.send_text("9198765", "x")
    assert len(attempts) == 1          # 4xx → no pointless retries


def test_server_error_retried_n_times(client, monkeypatch):
    attempts = []
    monkeypatch.setattr(
        "actions.whatsapp.requests.post",
        lambda *a, **k: attempts.append(1) or FakeResponse(status=500))
    client.send_text("9198765", "x")
    assert len(attempts) == RETRIES


def test_unconfigured_client_fails_gracefully(tmp_path, monkeypatch):
    db.init_db(tmp_path / "wa2.db")
    from config import settings
    monkeypatch.setattr(settings, "wa_api_key", "")
    c = WhatsAppClient(queue_path=tmp_path / "q.json")
    assert c.configured is False
    assert c.send_text("91", "x") is False


def test_queue_survives_restart(client, monkeypatch, tmp_path):
    import requests as req
    monkeypatch.setattr("actions.whatsapp.requests.post",
                        lambda *a, **k: (_ for _ in ()).throw(
                            req.ConnectionError("down")))
    client.send_text("9111", "one")
    client.send_text("9222", "two")
    # a new client instance (≈ app restart) sees the same queue file
    c2 = WhatsAppClient(queue_path=client.queue_path)
    assert c2.queue_size() == 2
