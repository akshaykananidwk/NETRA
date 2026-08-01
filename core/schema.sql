-- ══════════════════════════════════════════════════════════════
-- KRISHNA NETRA — SQLite schema (idempotent, safe to re-run)
-- Applied by core/db.py:init_db() and usable standalone:
--   sqlite3 data/krishna.db < core/schema.sql
-- ══════════════════════════════════════════════════════════════

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS persons (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  full_name         TEXT NOT NULL,
  name_gu           TEXT,                -- Gujarati name for TTS
  call_name         TEXT,                -- "રમેશભાઈ" — how agent addresses them
  phone             TEXT,
  company           TEXT,
  designation       TEXT,
  relation_type     TEXT NOT NULL DEFAULT 'visitor',  -- admin|staff|visitor|vip|vendor|blacklist
  is_admin          INTEGER DEFAULT 0,
  preferred_drink   TEXT,                -- chai|coffee|water|none
  notes             TEXT,
  photo_path        TEXT,
  greeting_enabled  INTEGER DEFAULT 1,
  created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at        DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS face_embeddings (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id   INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
  embedding   BLOB NOT NULL,             -- float32[512]
  quality     REAL,                      -- det_score
  source_path TEXT,
  created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_fe_person ON face_embeddings(person_id);

CREATE TABLE IF NOT EXISTS voice_prints (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id   INTEGER NOT NULL REFERENCES persons(id) ON DELETE CASCADE,
  embedding   BLOB NOT NULL,             -- float32[192] ECAPA
  sample_path TEXT,
  created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cameras (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT NOT NULL,            -- "Reception", "Cabin"
  source_type   TEXT NOT NULL,            -- webcam|rtsp
  source_url    TEXT NOT NULL,            -- "0" or rtsp://...
  zone          TEXT,
  greet_here    INTEGER DEFAULT 1,        -- speak greetings from this camera?
  enabled       INTEGER DEFAULT 1,
  last_frame_at DATETIME,
  status        TEXT DEFAULT 'unknown'    -- online|offline|error
);

CREATE TABLE IF NOT EXISTS unknown_faces (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  temp_uid       TEXT UNIQUE,             -- UNK-20260802-0007
  embedding      BLOB NOT NULL,
  snapshot_path  TEXT,
  spoken_name    TEXT,                    -- what STT heard when asked
  ask_count      INTEGER DEFAULT 0,
  first_seen     DATETIME,
  last_seen      DATETIME,
  resolved_person_id INTEGER REFERENCES persons(id),
  status         TEXT DEFAULT 'pending'   -- pending|registered|ignored
);

CREATE TABLE IF NOT EXISTS visits (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id     INTEGER REFERENCES persons(id),
  unknown_id    INTEGER REFERENCES unknown_faces(id),
  camera_id     INTEGER REFERENCES cameras(id),
  first_seen    DATETIME NOT NULL,
  last_seen     DATETIME NOT NULL,
  confidence    REAL,
  snapshot_path TEXT,
  greeted       INTEGER DEFAULT 0,
  drink_served  TEXT,
  status        TEXT DEFAULT 'known'      -- known|unknown|pending
);
CREATE INDEX IF NOT EXISTS idx_visits_time ON visits(first_seen);

CREATE TABLE IF NOT EXISTS meetings (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  title         TEXT,
  started_at    DATETIME NOT NULL,
  ended_at      DATETIME,
  camera_id     INTEGER,
  audio_path    TEXT,
  language      TEXT DEFAULT 'gu',
  summary       TEXT,
  key_points    TEXT,                     -- JSON array
  decisions     TEXT,                     -- JSON array
  status        TEXT DEFAULT 'recording'  -- recording|processing|done|failed
);

CREATE TABLE IF NOT EXISTS meeting_participants (
  meeting_id INTEGER REFERENCES meetings(id) ON DELETE CASCADE,
  person_id  INTEGER REFERENCES persons(id),
  detected_by TEXT                        -- face|voice|manual
);

CREATE TABLE IF NOT EXISTS transcript_segments (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  meeting_id    INTEGER REFERENCES meetings(id) ON DELETE CASCADE,
  speaker_label TEXT,                     -- SPK_1 / person name
  person_id     INTEGER REFERENCES persons(id),
  start_ms      INTEGER,
  end_ms        INTEGER,
  text          TEXT NOT NULL,
  confidence    REAL
);

CREATE TABLE IF NOT EXISTS tasks (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  title           TEXT NOT NULL,
  description     TEXT,
  assignee_id     INTEGER REFERENCES persons(id),
  assignee_name   TEXT,                   -- if not a registered person
  due_at          DATETIME,
  priority        TEXT DEFAULT 'medium',  -- low|medium|high|urgent
  status          TEXT DEFAULT 'open',    -- open|in_progress|done|cancelled
  source          TEXT DEFAULT 'voice',   -- voice|meeting|manual
  meeting_id      INTEGER REFERENCES meetings(id),
  created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
  completed_at    DATETIME
);

CREATE TABLE IF NOT EXISTS reminders (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id      INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
  remind_at    DATETIME NOT NULL,
  channel      TEXT DEFAULT 'voice',      -- voice|whatsapp|web|all
  target_phone TEXT,
  message      TEXT,
  status       TEXT DEFAULT 'scheduled',  -- scheduled|sent|failed|cancelled
  sent_at      DATETIME
);

CREATE TABLE IF NOT EXISTS conversations (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  person_id    INTEGER REFERENCES persons(id),
  channel      TEXT DEFAULT 'voice',      -- voice|web
  user_text    TEXT,
  agent_text   TEXT,
  tool_calls   TEXT,                      -- JSON
  latency_ms   INTEGER,
  created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS whatsapp_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  to_number   TEXT NOT NULL,
  message     TEXT,
  purpose     TEXT,                       -- tea_order|reminder|summary|alert
  status      TEXT,                       -- sent|failed
  response    TEXT,
  created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS system_health (
  component   TEXT PRIMARY KEY,           -- camera_1|mic|stt|tts|llm|whatsapp|db
  status      TEXT,                       -- ok|degraded|down
  detail      TEXT,
  updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  actor      TEXT,
  action     TEXT,
  detail     TEXT,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
