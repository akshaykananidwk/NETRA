"""KRISHNA NETRA (કૃષ્ણ નેત્ર) — FastAPI entry point.

Run:  python main.py        (or: uvicorn main:app --host 0.0.0.0 --port 8000)
"""
from __future__ import annotations

import os

# quiet ffmpeg's per-frame h264 warnings from flaky RTSP cameras
# (must be set before OpenCV loads)
os.environ.setdefault("OPENCV_FFMPEG_LOGLEVEL", "-8")

import asyncio
import logging
import threading
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles

from config import ensure_dirs, settings

BASE_DIR = Path(__file__).resolve().parent


class _SafeConsoleHandler(logging.StreamHandler):
    """Console handler that can never print '--- Logging error ---'.

    Some Windows consoles reject Gujarati/emoji even after reconfigure();
    fall back to replacement characters — the UTF-8 log file keeps the
    full text either way."""

    def emit(self, record):
        try:
            msg = self.format(record) + self.terminator
            try:
                self.stream.write(msg)
            except UnicodeEncodeError:
                enc = getattr(self.stream, "encoding", None) or "ascii"
                self.stream.write(msg.encode(enc, "replace").decode(enc))
            self.flush()
        except Exception:
            pass


def _setup_logging() -> None:
    ensure_dirs()
    # Windows console defaults to cp1252 — Gujarati/emoji log lines would
    # raise "--- Logging error ---" without this
    import sys
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    logging.raiseExceptions = False  # a log line must never crash-print
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    if getattr(root, "_krishna_configured", False):
        return  # uvicorn imports main twice (__main__ + main)
    root._krishna_configured = True
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(settings.logs_dir / "krishna.log",
                             maxBytes=10 * 1024 * 1024, backupCount=5,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    sh = _SafeConsoleHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)

    # Windows proactor logs a harmless ConnectionResetError every time a
    # browser drops a socket mid-close — pure noise, hide it
    class _DropConnReset(logging.Filter):
        def filter(self, record):
            return "_call_connection_lost" not in record.getMessage()
    logging.getLogger("asyncio").addFilter(_DropConnReset())


_setup_logging()
log = logging.getLogger("krishna.main")


def _self_heal_packages() -> None:
    """A half-installed huggingface_hub crashes with a circular import and
    silently kills face recognition AND Whisper. Detect and reinstall it
    before any worker imports it — no manual repair.bat needed."""
    import importlib
    import subprocess
    import sys
    try:
        import huggingface_hub.utils  # noqa: F401
        return
    except Exception as e:
        log.warning("huggingface_hub તૂટેલું છે — આપોઆપ repair ચાલુ… (%s)",
                    str(e)[:120])
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "--force-reinstall",
             "--no-cache-dir", "huggingface_hub", "hf_xet"],
            check=False, timeout=600)
    except Exception:
        log.exception("auto-repair run failed")
    # drop the half-imported modules so the fresh install is picked up
    for name in [m for m in list(sys.modules)
                 if m == "huggingface_hub"
                 or m.startswith("huggingface_hub.")]:
        sys.modules.pop(name, None)
    importlib.invalidate_caches()
    try:
        import huggingface_hub.utils  # noqa: F401
        log.info("huggingface_hub repair સફળ ✓ (કેમેરા-ઓળખ + Whisper હવે "
                 "લોડ થશે)")
    except Exception:
        log.error("auto-repair પછી પણ ભૂલ છે — server બંધ કરી repair.bat "
                  "ચલાવો")


def _llm_health_loop() -> None:
    """Probe the configured brain and show a clear Gujarati status/fix."""
    import time as _time

    import requests as _rq

    from core.db import set_health

    while True:
        try:
            local_first = (settings.llm_provider == "ollama"
                           or not settings.anthropic_api_key)
            if local_first:
                try:
                    r = _rq.get(settings.ollama_host.rstrip("/") + "/api/tags",
                                timeout=5)
                    r.raise_for_status()
                    models = [m.get("name", "")
                              for m in r.json().get("models", [])]
                    base = settings.ollama_model.split(":")[0]
                    if any(base in m for m in models):
                        # empty-prompt generate = load model into RAM and
                        # keep it there — first user command answers fast
                        # instead of timing out on a minutes-long cold load
                        try:
                            _rq.post(settings.ollama_host.rstrip("/")
                                     + "/api/generate",
                                     json={"model": settings.ollama_model,
                                           "keep_alive": "60m"},
                                     timeout=settings.ollama_timeout_sec)
                        except Exception:
                            pass
                        set_health("llm", "ok",
                                   f"ollama સ્થાનિક: {settings.ollama_model} તૈયાર ✓")
                    else:
                        set_health("llm", "degraded",
                                   "Ollama ચાલુ છે પણ model નથી — cmd માં "
                                   f"ચલાવો: ollama pull {settings.ollama_model}")
                except Exception:
                    if settings.anthropic_api_key:
                        set_health("llm", "ok", "anthropic (cloud)")
                    else:
                        set_health("llm", "down",
                                   "LLM નથી: ollama.com પરથી Ollama install "
                                   f"કરી 'ollama pull {settings.ollama_model}' "
                                   "ચલાવો — સામાન્ય commands તો પણ ચાલશે")
            else:
                set_health("llm", "ok", "anthropic (cloud) તૈયાર")
        except Exception:
            log.exception("llm health check failed")
        _time.sleep(120)


def _tts_self_test() -> None:
    """Synth a short phrase at startup so the Settings page shows the real
    voice status right away — not 'no speech yet' until the first greeting.
    The phrase lands in the disk cache, so later runs pass even offline."""
    import asyncio as _aio

    from core.db import set_health
    from core.runtime import tts
    try:
        path = _aio.run(tts.synth("કૃષ્ણ નેત્ર તૈયાર છે"))
        if path:
            set_health("tts", "ok", f"{tts.last_engine} ✓")
        else:
            set_health("tts", "degraded", tts.last_error or "synth failed")
    except Exception as e:
        set_health("tts", "degraded", str(e)[:200])


@asynccontextmanager
async def lifespan(app: FastAPI):
    from core.db import apply_db_settings, init_db, seed_defaults, set_health
    from core.runtime import (audio_worker, bus, detector, matcher,
                              orchestrator, speaker, stt_worker, unknowns,
                              vision_manager)

    init_db()
    seed_defaults()
    apply_db_settings()
    matcher.reload()
    unknowns.load()

    bus.attach_loop(asyncio.get_running_loop())
    orch_task = asyncio.create_task(orchestrator.run())

    # model download/load can take minutes on first run — never block startup
    threading.Thread(target=detector.load, daemon=True,
                     name="detector-load").start()

    vision_manager.start_all()
    speaker.start()          # TTS playback queue
    stt_worker.start()       # loads whisper in its own thread, keeps it warm
    audio_worker.start()     # mic → health → VAD → STT

    from core.runtime import reminders, speaker_id, whatsapp
    # one crashed component must not kill the system (reliability rule)
    for name, fn in (("reminders", reminders.start),
                     ("whatsapp-flusher", whatsapp.start_flusher)):
        try:
            fn()
        except Exception:
            log.exception("%s failed to start — continuing without it", name)
    threading.Thread(target=speaker_id.load, daemon=True,
                     name="speaker-id-load").start()
    set_health("db", "ok", "")
    set_health("tts", "degraded", "ટેસ્ટ ચાલુ છે…")
    threading.Thread(target=_tts_self_test, daemon=True,
                     name="tts-self-test").start()
    threading.Thread(target=_llm_health_loop, daemon=True,
                     name="llm-health").start()
    if not whatsapp.configured:
        set_health("whatsapp", "down", "WA_API_KEY સેટ નથી (.env)")
    from config import get_lan_ip
    log.info("KRISHNA NETRA up — http://%s:%s", settings.host, settings.port)
    log.info("📱 ફોન માટે (same Wi-Fi): http://%s:%s", get_lan_ip(),
             settings.port)

    yield

    log.info("shutting down…")
    vision_manager.stop_all()
    audio_worker.stop()
    stt_worker.stop()
    speaker.stop()
    reminders.stop()
    whatsapp.stop()
    orch_task.cancel()
    try:
        await orch_task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Krishna Netra", version="1.0.0-phase3", lifespan=lifespan)


# ── simple admin login (enabled when ADMIN_PASSWORD is set in .env) ───────
def _auth_token() -> str:
    import hashlib
    import hmac as hmac_mod
    return hmac_mod.new(settings.admin_password.encode("utf-8"),
                        b"krishna-netra-login",
                        hashlib.sha256).hexdigest()


_PUBLIC_PATHS = ("/login.html", "/api/login", "/assets/")


@app.middleware("http")
async def auth_middleware(request, call_next):
    if not settings.admin_password:
        return await call_next(request)
    path = request.url.path
    if any(path == p or path.startswith(p) for p in _PUBLIC_PATHS):
        return await call_next(request)
    if request.cookies.get("kn_auth") == _auth_token():
        return await call_next(request)
    from fastapi.responses import JSONResponse, RedirectResponse
    if path.startswith(("/api/", "/stream", "/media")):
        return JSONResponse({"detail": "login required"}, status_code=401)
    return RedirectResponse("/login.html", status_code=302)


@app.post("/api/login")
async def login(request: Request):
    from fastapi.responses import JSONResponse
    body = await request.json()
    if not settings.admin_password:
        return JSONResponse({"ok": True})
    if str(body.get("password", "")) != settings.admin_password:
        return JSONResponse({"detail": "ખોટો પાસવર્ડ"}, status_code=401)
    resp = JSONResponse({"ok": True})
    resp.set_cookie("kn_auth", _auth_token(), max_age=30 * 86400,
                    httponly=True, samesite="lax")
    return resp


from api import routes_agent, routes_cameras, routes_meetings, \
    routes_persons, routes_stream, routes_system, routes_tasks, \
    routes_update, routes_visits, ws  # noqa: E402

app.include_router(routes_system.router)
app.include_router(routes_update.router)
app.include_router(routes_tasks.router)
app.include_router(routes_agent.router)
app.include_router(routes_meetings.router)
app.include_router(routes_persons.router)
app.include_router(routes_visits.router)
app.include_router(routes_cameras.router)
app.include_router(routes_stream.router)
app.include_router(ws.router)

# media (snapshots + enrollment photos) — DB file is NOT exposed
ensure_dirs()
app.mount("/media/snapshots", StaticFiles(directory=settings.snapshots_dir),
          name="snapshots")
app.mount("/media/faces", StaticFiles(directory=settings.faces_dir), name="faces")
app.mount("/media/meetings", StaticFiles(directory=settings.meetings_dir),
          name="meetings")
app.mount("/media/tts", StaticFiles(directory=settings.tts_cache_dir),
          name="tts")
# web UI last — catches everything else, serves index.html at /
app.mount("/", StaticFiles(directory=BASE_DIR / "web", html=True), name="web")


if __name__ == "__main__":
    _self_heal_packages()
    import uvicorn
    uvicorn.run("main:app", host=settings.host, port=settings.port,
                log_config=None)
