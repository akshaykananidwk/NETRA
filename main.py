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

from fastapi import FastAPI
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
    set_health("db", "ok", "")
    set_health("tts", "degraded", "no speech yet")
    log.info("KRISHNA NETRA up — http://%s:%s", settings.host, settings.port)

    yield

    log.info("shutting down…")
    vision_manager.stop_all()
    audio_worker.stop()
    stt_worker.stop()
    speaker.stop()
    orch_task.cancel()
    try:
        await orch_task
    except asyncio.CancelledError:
        pass


app = FastAPI(title="Krishna Netra", version="1.0.0-phase1", lifespan=lifespan)

from api import routes_cameras, routes_persons, routes_stream, routes_system, \
    routes_update, routes_visits, ws  # noqa: E402

app.include_router(routes_system.router)
app.include_router(routes_update.router)
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
