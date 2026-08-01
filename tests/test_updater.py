"""GitHub updater — protected paths, apply, backup/rollback, migrations, check."""
import json
import zipfile
from pathlib import Path

import pytest

import core.db as db
from core.updater import UpdateError, UpdateManager


@pytest.fixture()
def app(tmp_path):
    """A fake app root + initialised DB."""
    root = tmp_path / "app"
    (root / "core").mkdir(parents=True)
    (root / "web").mkdir()
    (root / "data").mkdir()
    (root / "main.py").write_text("print('v1')\n")
    (root / "web" / "index.html").write_text("<h1>v1</h1>")
    (root / ".env").write_text("SECRET=keep-me")
    (root / "start.bat").write_text("@echo v1\n")
    # updater re-applies the real idempotent schema
    real_schema = Path(__file__).resolve().parent.parent / "core" / "schema.sql"
    (root / "core" / "schema.sql").write_text(
        real_schema.read_text(encoding="utf-8"), encoding="utf-8")
    db.init_db(tmp_path / "upd.db")
    return root


@pytest.fixture()
def staged(app, tmp_path):
    """An extracted 'downloaded tree' with changes."""
    tree = tmp_path / "tree" / "owner-repo-abc123"
    (tree / "core").mkdir(parents=True)
    (tree / "web").mkdir()
    (tree / "data").mkdir()
    (tree / "main.py").write_text("print('v2')\n")
    (tree / "web" / "index.html").write_text("<h1>v2</h1>")
    (tree / "web" / "new_page.html").write_text("<h1>new</h1>")
    (tree / ".env").write_text("SECRET=evil-overwrite")
    (tree / "data" / "injected.db").write_text("nope")
    (tree / "start.bat").write_text("@echo v2\n")
    return tree


def test_protected_paths():
    m = UpdateManager()
    assert m.is_protected(".env")
    assert m.is_protected("data/krishna.db")
    assert m.is_protected("data/faces/1/a.jpg")
    assert m.is_protected("models/buffalo_l/det.onnx")
    assert m.is_protected("logs/krishna.log")
    assert m.is_protected(".git/HEAD")
    assert not m.is_protected("main.py")
    assert not m.is_protected("web/index.html")
    assert not m.is_protected(".env.example")
    assert not m.is_protected("core/db.py")


def test_apply_updates_code_but_never_protected(app, staged):
    m = UpdateManager(app_root=app)
    files = m._incoming_files(staged)
    stats = m._apply(staged, files)

    assert (app / "main.py").read_text() == "print('v2')\n"
    assert (app / "web" / "index.html").read_text() == "<h1>v2</h1>"
    assert (app / "web" / "new_page.html").exists()          # new file added
    assert (app / ".env").read_text() == "SECRET=keep-me"    # untouched
    assert not (app / "data" / "injected.db").exists()       # untouched
    assert stats["protected_skipped"] >= 2


def test_live_batch_files_are_deferred_not_overwritten(app, staged):
    m = UpdateManager(app_root=app)
    m._apply(staged, m._incoming_files(staged))
    assert (app / "start.bat").read_text() == "@echo v1\n"   # still running copy
    assert (app / "start.bat.new").read_text() == "@echo v2\n"


def test_backup_and_rollback_restores_originals(app, staged):
    m = UpdateManager(app_root=app)
    files = m._incoming_files(staged)
    backup = m._backup(files, app / "data" / "backups" / "t1")
    m._apply(staged, files)
    assert (app / "main.py").read_text() == "print('v2')\n"

    restored = m._rollback_code(Path(backup["code_zip"]))
    assert restored > 0
    assert (app / "main.py").read_text() == "print('v1')\n"
    assert (app / "web" / "index.html").read_text() == "<h1>v1</h1>"
    assert Path(backup["db_backup"]).exists()                # DB snapshot taken


def test_migrations_run_once_and_are_recorded(app):
    m = UpdateManager(app_root=app)
    mig = app / "migrations"
    mig.mkdir()
    (mig / "001_add_note.sql").write_text(
        "ALTER TABLE persons ADD COLUMN test_note TEXT;")

    result = m._migrate()
    assert result["migrations"] == ["001_add_note.sql"]
    result2 = m._migrate()                     # second run: already applied
    assert result2["migrations"] == []

    with db.SessionLocal() as s:               # column really exists
        s.execute(db.text("SELECT test_note FROM persons LIMIT 1"))


def test_check_reports_new_commit(app, monkeypatch):
    m = UpdateManager(app_root=app)
    m.save_config("owner/repo", "main", "tok123")
    m._set_setting("app_commit", "old-sha")

    class FakeResp:
        def __init__(self, data, status=200):
            self._data = data
            self.status_code = status
            self.ok = status == 200
        def json(self):
            return self._data
        def raise_for_status(self):
            pass

    def fake_get(url, headers=None, timeout=None):
        assert headers["Authorization"] == "Bearer tok123"
        if "/branches/" in url:
            return FakeResp({"commit": {
                "sha": "new-sha-123456789",
                "commit": {"message": "Fix greeting bug\n\ndetails",
                           "author": {"name": "AK", "date": "2026-08-01T10:00:00Z"}}}})
        if "/compare/" in url:
            return FakeResp({"ahead_by": 3, "commits": [
                {"sha": "aaaaaaaa1", "commit": {"message": "c1",
                 "author": {"name": "AK", "date": "2026-08-01T09:00:00Z"}}}]})
        raise AssertionError(url)

    monkeypatch.setattr("core.updater.requests.get", fake_get)
    info = m.check()
    assert info["update_available"] is True
    assert info["latest"]["message"] == "Fix greeting bug"
    assert info["commits_behind"] == 3
    assert len(info["commits"]) == 1

    # same sha → no update
    m._set_setting("app_commit", "new-sha-123456789")
    info = m.check()
    assert info["update_available"] is False


def test_check_without_config_raises(app):
    m = UpdateManager(app_root=app)
    with pytest.raises(UpdateError):
        m.check()


def test_save_config_validates_and_strips_url(app):
    m = UpdateManager(app_root=app)
    m.save_config("https://github.com/owner/repo/", "dev", "t")
    cfg = m.get_config()
    assert cfg["repo"] == "owner/repo"
    assert cfg["branch"] == "dev"
    assert cfg["token_set"] is True
    assert cfg["token"].endswith("t") and "•" in cfg["token"]   # masked
    with pytest.raises(UpdateError):
        m.save_config("not-a-repo", "main")


def test_extract_rejects_zip_slip(app, tmp_path):
    m = UpdateManager(app_root=app)
    evil = tmp_path / "evil.zip"
    with zipfile.ZipFile(evil, "w") as z:
        z.writestr("owner-repo-x/ok.txt", "fine")
        z.writestr("../../escape.txt", "evil")
    with pytest.raises(UpdateError):
        m._extract(evil, tmp_path / "st")
