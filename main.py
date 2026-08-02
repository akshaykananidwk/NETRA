"""KRISHNA NETRA (કૃષ્ણ નેત્ર) — FastAPI entry point.

Run:  python main.py        (or: uvicorn main:app --host 0.0.0.0 --port 8000)
"""
from __future__ import annotations

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


def _setup_logging() -> None:
    ensure_dirs()
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
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


_setup_logging()
log = logging.getLogger("krishna.main")


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
    set_health("tts", "degraded", "no speech yet")
    set_health("llm", "degraded",
               "no command yet" if settings.anthropic_api_key
               else "ANTHROPIC_API_KEY સેટ નથી — ollama/rules fallback")
    if not whatsapp.configured:
        set_health("whatsapp", "down", "WA_API_KEY સેટ નથી (.env)")
    log.info("KRISHNA NETRA up — http://%s:%s", settings.host, settings.port)

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


from api import routes_agent, routes_cameras, routes_persons, routes_stream, \
    routes_system, routes_tasks, routes_update, routes_visits, ws  # noqa: E402

app.include_router(routes_system.router)
app.include_router(routes_update.router)
app.include_router(routes_tasks.router)
app.include_router(routes_agent.router)
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
# web UI last — catches everything else, serves index.html at /
app.mount("/", StaticFiles(directory=BASE_DIR / "web", html=True), name="web")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.host, port=settings.port,
                log_config=None)
