# KRISHNA NETRA (કૃષ્ણ નેત્ર)

Local-first agentic AI office manager for AK Computer, Dwarka.
Watches the office through cameras, recognises people by face, logs every
visit, and (in later phases) greets people in Gujarati, listens to meetings
and executes voice commands. **All recognition and storage runs on the local
machine** — no cloud dependency.

> **Current status: Phase 2 — "Voice"** ✅
> Everything from Phase 1 (camera capture, face recognition, visit log,
> Gujarati web UI) **plus**: Gujarati TTS greetings (edge-tts + Piper
> fallback, disk-cached), greeting on known face with a persisted 4-hour
> cooldown, the unknown-visitor ask-name flow (max 2 asks/day), mic capture
> → VAD → faster-whisper STT, and mic ducking so the agent never hears
> itself.

## Quick start — Windows (one click)

1. Double-click **`install.bat`** once — it installs Python 3.11 (via winget
   if missing), creates the venv, installs all packages, creates `.env`, and
   opens the firewall port for phone access.
2. Double-click **`start.bat`** — the server starts and **Google Chrome
   opens on http://localhost:8000 automatically**. The window keeps a
   restart loop, so crashes and GitHub updates come back up on their own.
   Close the window to stop.

## Quick start — manual / Linux

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

## One-click GitHub updates

Settings → **🔄 સોફ્ટવેર અપડેટ (GitHub)**. Save the repository
(`owner/repo`), branch, and a GitHub token **once** — after that no file
ever needs to be uploaded manually.

- **🔍 Check for Update** — compares the running version against GitHub and
  shows the new version's commit message, author, date, and how many
  commits behind you are.
- **⬇ Update Now** — one click does all of this, in order:
  1. downloads the branch zip straight from GitHub to the server,
  2. **backs up** the current code **and** the SQLite DB to
     `data/backups/update_<timestamp>/`,
  3. replaces the app files — `.env`, `data/`, `models/`, `logs/` are
     **never** touched,
  4. re-applies the idempotent schema + runs any pending
     `migrations/*.sql` (each exactly once — see `migrations/README.md`),
  5. clears `__pycache__`,
  6. restarts the server (the `start.bat` loop / NSSM / systemd brings it
     back), and the page reloads itself when it's up.
- Any failure after files were applied triggers **automatic rollback** of
  both code and DB from the backup, then a restart — the office never stays
  broken.
- `start.bat` / `install.bat` are running while cmd reads them, so updates
  to them are written as `.bat.new` and adopted on the next start —
  never corrupted mid-run.

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

### Voice (Phase 2)

- A recognised person is greeted aloud — *"નમસ્તે રમેશભાઈ, આપનું સ્વાગત
  છે."* — at most once per `GREET_COOLDOWN_HOURS` per camera. The cooldown
  lives in the `visits` table, so a restart never causes re-greeting spam.
- An unknown visitor is welcomed and asked their name (at most
  `UNKNOWN_ASK_MAX_PER_DAY` times a day, then silently logged). Whatever
  Whisper hears is saved as `spoken_name` and pre-fills the dashboard's
  pending-registration card for the admin to confirm.
- TTS chain: **edge-tts** (`gu-IN-DhwaniNeural`, needs internet) → **Piper**
  (offline, drop a Gujarati model at `PIPER_MODEL_PATH`) → skip-with-log.
  Synthesised phrases are cached in `data/tts_cache/`, so repeated greetings
  are instant and keep working offline.
- While the speaker plays, mic input is dropped (**ducking**) so the agent
  never transcribes its own voice.
- First run downloads the Whisper model (`WHISPER_MODEL`, default `medium`,
  ~1.5 GB) into `models/`; use `small` on a weaker CPU. The STT badge on the
  Settings page shows load progress.

## Architecture (Phase 1 slice)

```
Browser ── REST + WebSocket + MJPEG ── FastAPI (main.py)
                                          │
                                    Orchestrator (asyncio)   ← event bus
                                          │
        Vision workers (1 thread/camera)  │   Audio worker (thread)
        capture → detect@5fps → IOU-track │   mic → VAD → STT queue
        → recognise once per track ───────┤
                                          │   STT worker (whisper, warm)
        Greeter (asyncio)                 │   Speaker thread (TTS queue,
        known → greet · unknown → ask ────┘     mic ducking)
                                          │
                                   SQLite WAL + snapshots + tts_cache
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
audio/                mic stream + health, VAD segmenter, whisper STT, worker
speech/               TTS engine (edge → piper → skip) + playback/ducking
agent/                greeter flows, name extraction (LLM brain in Phase 3)
api/                  REST routes, MJPEG stream, WebSocket hub
web/                  Gujarati UI — dashboard, live, people, visits, settings
core/updater.py       one-click GitHub update (backup → apply → migrate → rollback)
migrations/           numbered .sql files, applied once each during updates
install.bat           one-time Windows setup (Python, venv, packages, firewall)
start.bat             one-click start + auto-restart loop + opens Chrome
tests/                matcher, tracker, DB, orchestrator, greeter, VAD, names, updater
data/                 krishna.db, faces/, snapshots/, tts_cache/, backups/  (runtime)
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
| **1 — Eyes** ✅ | face recognition, visit log, live view, web UI |
| **2 — Voice** ✅ | this release — TTS greetings, VAD + Whisper STT, unknown-name flow, mic ducking |
| 3 — Brain | wake word, admin voice print, LLM agent + tools, WhatsApp tea ordering, tasks/reminders |
| 4 — Meetings | recorder, rolling transcript, speaker labels, summary + task extraction |
| 5 — Office scale | multi-RTSP, anti-spoof, attendance, Cloudflare Tunnel, Windows service |
