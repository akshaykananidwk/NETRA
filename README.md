# KRISHNA NETRA (કૃષ્ણ નેત્ર)

Local-first agentic AI office manager for AK Computer, Dwarka.
Watches the office through cameras, recognises people by face, logs every
visit, and (in later phases) greets people in Gujarati, listens to meetings
and executes voice commands. **All recognition and storage runs on the local
machine** — no cloud dependency.

> **Current status: Phase 1 — "Eyes"** ✅
> Camera capture · face detection/recognition (InsightFace buffalo_l) ·
> SQLite (WAL) · person registration from web UI · MJPEG live view ·
> visitor log · camera + mic status badges · Gujarati web UI.

## Quick start (laptop webcam)

```bash
# Python 3.11
python -m venv .venv
.venv\Scripts\activate            # Windows   (Linux: source .venv/bin/activate)
pip install -r requirements.txt

copy .env.example .env            # Linux: cp .env.example .env
python main.py
```

Open **http://localhost:8000** (or `http://<LAN-IP>:8000` from a phone on the
same Wi-Fi).

First run downloads the InsightFace `buffalo_l` model (~300 MB) into
`models/` — the UI's AI badge turns green when it's ready.

### Enrol the owner

1. Open **લોકો (People)** → **+ નવી વ્યક્તિ**.
2. Name: `AK Bhai`, સંબંધ: `એડમિન`, and upload 3–5 clear face photos
   (front, left, right, with/without glasses).
3. Stand in front of the webcam — the dashboard shows the recognition and the
   visit appears in **મુલાકાત (Visits)**.

Unknown faces appear on the dashboard as a "નવો ચહેરો — નામ ઉમેરો" card:
type the name, press સેવ, and the sighting embedding becomes their first
enrollment — next time they're recognised by name.

## Architecture (Phase 1 slice)

```
Browser ── REST + WebSocket + MJPEG ── FastAPI (main.py)
                                          │
                                    Orchestrator (asyncio)   ← event bus
                                          │
        Vision workers (1 thread/camera)  │   Mic health monitor (thread)
        capture → detect@5fps → IOU-track │
        → recognise once per track ───────┘
                                          │
                                   SQLite WAL + snapshots
```

Key rules implemented:

- Detection runs at `DETECTION_FPS` (default 5) on a 640-px downscaled frame;
  full-res frames are only used for snapshots.
- **Recognition runs once per IOU track**, never per frame.
- All embeddings live in one NumPy matrix — matching is a single
  `matrix @ query`.
- Vision threads never touch the DB/network — everything slow goes through
  the event bus to the Orchestrator.
- Every component degrades gracefully: missing camera, mic, or model marks
  the health badge red, nothing crashes.

## Layout

```
main.py               FastAPI entry + lifespan wiring
orchestrator.py       event consumer, visit logging, activity feed
config.py             .env → typed settings (+ runtime overrides from DB)
core/                 schema.sql, SQLAlchemy models, event bus, state machine
vision/               capture, detector, embedder, matcher, IOU tracker, worker
audio/mic.py          mic health monitor (ok / muted / down) + level meter
api/                  REST routes, MJPEG stream, WebSocket hub
web/                  Gujarati UI — dashboard, live, people, visits, settings
tests/                matcher, tracker, DB, orchestrator integration
data/                 krishna.db, faces/, snapshots/   (created at runtime)
```

## Configuration

Everything is in `.env` (see `.env.example` for the full documented list).
Recognition thresholds, cooldowns and FPS are also editable live from the
**સેટિંગ્સ** page (persisted in the `settings` table).

| Setting | Default | Meaning |
|---|---|---|
| `FACE_MATCH_THRESHOLD` | 0.55 | cosine ≥ this → known |
| `FACE_UNCERTAIN_THRESHOLD` | 0.40 | between → logged for admin review |
| `DETECTION_FPS` | 5 | detector runs per second per camera |
| `MIN_FACE_SIZE` | 80 | reject smaller faces (px, full-res) |
| `VISIT_MERGE_MINUTES` | 10 | re-sightings within this = same visit |
| `USE_GPU` | false | switch to onnxruntime-gpu + CUDA provider |

## Tests

```bash
pytest tests/ -q
```

Vision hardware and models are not required — matcher/tracker/orchestrator
tests run on synthetic embeddings.

## Privacy (India DPDP Act 2023)

- Put up a visible notice: **"આ સ્થળે CCTV અને AI રેકોર્ડિંગ ચાલુ છે"**.
- All biometric data stays on this machine.
- Deleting a person from the People page purges their embeddings, photos,
  visit rows and visit snapshots.

## Roadmap

| Phase | Scope |
|---|---|
| **1 — Eyes** ✅ | this release |
| 2 — Voice | Gujarati TTS greetings, VAD + Whisper STT, unknown-name flow, mic ducking |
| 3 — Brain | wake word, admin voice print, LLM agent + tools, WhatsApp tea ordering, tasks/reminders |
| 4 — Meetings | recorder, rolling transcript, speaker labels, summary + task extraction |
| 5 — Office scale | multi-RTSP, anti-spoof, attendance, Cloudflare Tunnel, Windows service |
