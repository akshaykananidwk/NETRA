"""Typed application settings loaded from .env (with sane defaults).

Values that appear in the Settings UI (thresholds, cooldowns) may be
overridden at runtime from the `settings` DB table — see core.db.apply_db_settings.
"""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _s(key: str, default: str = "") -> str:
    return os.getenv(key, default).strip()


def _i(key: str, default: int) -> int:
    try:
        return int(_s(key, str(default)))
    except ValueError:
        return default


def _f(key: str, default: float) -> float:
    try:
        return float(_s(key, str(default)))
    except ValueError:
        return default


def _b(key: str, default: bool) -> bool:
    return _s(key, str(default)).lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # server
    host: str
    port: int
    ui_lang: str
    timezone: str
    work_hours_start: str
    work_hours_end: str
    # vision
    face_model: str
    detection_fps: float
    face_match_threshold: float
    face_uncertain_threshold: float
    min_face_size: int
    det_score_min: float
    max_yaw_deg: float
    greet_cooldown_hours: float
    unknown_ask_max_per_day: int
    unknown_dedupe_threshold: float
    visit_merge_minutes: int
    stream_fps: int
    use_gpu: bool
    # audio
    mic_device_index: int
    sample_rate: int
    vad_aggressiveness: int
    reply_timeout_sec: float
    # stt
    whisper_model: str
    whisper_language: str
    stt_always_on: bool
    # tts
    tts_engine: str
    edge_voice: str
    tts_rate: str
    piper_model_path: Path
    tts_cache_dir: Path
    # wake word + speaker id
    wake_words: tuple
    speaker_match_threshold: float
    admin_face_window_sec: float
    # llm
    llm_provider: str
    anthropic_api_key: str
    anthropic_model: str
    ollama_host: str
    ollama_model: str
    llm_timeout_sec: float
    # whatsapp
    wa_api_base: str
    wa_api_key: str
    wa_device_id: str
    wa_send_path: str
    wa_canteen_number: str
    wa_admin_number: str
    wa_timeout_sec: float
    # security
    admin_password: str
    # meetings
    meeting_auto_end_min: float
    meetings_dir: Path
    # data
    snapshot_retention_days: int
    db_path: Path
    data_dir: Path
    faces_dir: Path
    snapshots_dir: Path
    models_dir: Path
    logs_dir: Path

    # keys editable from the Settings page (name -> python type)
    RUNTIME_EDITABLE = {
        "detection_fps": float,
        "face_match_threshold": float,
        "face_uncertain_threshold": float,
        "min_face_size": int,
        "det_score_min": float,
        "max_yaw_deg": float,
        "greet_cooldown_hours": float,
        "unknown_ask_max_per_day": int,
        "unknown_dedupe_threshold": float,
        "visit_merge_minutes": int,
        "stream_fps": int,
        "snapshot_retention_days": int,
    }


def _load() -> Settings:
    db_path = BASE_DIR / _s("DB_PATH", "data/krishna.db")
    data_dir = db_path.parent
    return Settings(
        host=_s("HOST", "0.0.0.0"),
        port=_i("PORT", 8000),
        ui_lang=_s("UI_LANG", "gu"),
        timezone=_s("TIMEZONE", "Asia/Kolkata"),
        work_hours_start=_s("WORK_HOURS_START", "09:00"),
        work_hours_end=_s("WORK_HOURS_END", "21:00"),
        face_model=_s("FACE_MODEL", "buffalo_l"),
        detection_fps=_f("DETECTION_FPS", 5),
        face_match_threshold=_f("FACE_MATCH_THRESHOLD", 0.55),
        face_uncertain_threshold=_f("FACE_UNCERTAIN_THRESHOLD", 0.40),
        min_face_size=_i("MIN_FACE_SIZE", 80),
        det_score_min=_f("DET_SCORE_MIN", 0.65),
        max_yaw_deg=_f("MAX_YAW_DEG", 45),
        greet_cooldown_hours=_f("GREET_COOLDOWN_HOURS", 4),
        unknown_ask_max_per_day=_i("UNKNOWN_ASK_MAX_PER_DAY", 2),
        unknown_dedupe_threshold=_f("UNKNOWN_DEDUPE_THRESHOLD", 0.50),
        visit_merge_minutes=_i("VISIT_MERGE_MINUTES", 10),
        stream_fps=_i("STREAM_FPS", 10),
        use_gpu=_b("USE_GPU", False),
        mic_device_index=_i("MIC_DEVICE_INDEX", -1),
        sample_rate=_i("SAMPLE_RATE", 16000),
        vad_aggressiveness=_i("VAD_AGGRESSIVENESS", 2),
        reply_timeout_sec=_f("REPLY_TIMEOUT_SEC", 12),
        whisper_model=_s("WHISPER_MODEL", "medium"),
        whisper_language=_s("WHISPER_LANGUAGE", "gu"),
        stt_always_on=_b("STT_ALWAYS_ON", True),
        tts_engine=_s("TTS_ENGINE", "edge"),
        edge_voice=_s("EDGE_VOICE", "gu-IN-DhwaniNeural"),
        tts_rate=_s("TTS_RATE", "+5%"),
        piper_model_path=BASE_DIR / _s("PIPER_MODEL_PATH", "models/piper/gu_IN.onnx"),
        tts_cache_dir=db_path.parent / "tts_cache",
        # built-in variants always included — Whisper spells "કૃષ્ણ" many ways
        wake_words=tuple(dict.fromkeys(
            [w.strip().lower() for w in _s("WAKE_WORD", "").split(",") if w.strip()]
            + ["krishna", "krishn", "કૃષ્ણ", "ક્રિષ્ના", "ક્રિશ્ના", "કૃષ્ના"])),
        speaker_match_threshold=_f("SPEAKER_MATCH_THRESHOLD", 0.72),
        admin_face_window_sec=_f("ADMIN_FACE_WINDOW_SEC", 30),
        llm_provider=_s("LLM_PROVIDER", "anthropic"),
        anthropic_api_key=_s("ANTHROPIC_API_KEY", ""),
        anthropic_model=_s("ANTHROPIC_MODEL", "claude-sonnet-4-6"),
        ollama_host=_s("OLLAMA_HOST", "http://localhost:11434"),
        ollama_model=_s("OLLAMA_MODEL", "qwen2.5:7b-instruct"),
        llm_timeout_sec=_f("LLM_TIMEOUT_SEC", 8),
        wa_api_base=_s("WA_API_BASE", "https://bulk.akdwk.in/api"),
        wa_api_key=_s("WA_API_KEY", ""),
        wa_device_id=_s("WA_DEVICE_ID", ""),
        wa_send_path=_s("WA_SEND_PATH", "/send-message"),
        wa_canteen_number=_s("WA_CANTEEN_NUMBER", ""),
        wa_admin_number=_s("WA_ADMIN_NUMBER", ""),
        wa_timeout_sec=_f("WA_TIMEOUT_SEC", 10),
        admin_password=_s("ADMIN_PASSWORD", ""),
        meeting_auto_end_min=_f("MEETING_AUTO_END_MIN", 5),
        meetings_dir=data_dir / "meetings",
        snapshot_retention_days=_i("SNAPSHOT_RETENTION_DAYS", 90),
        db_path=db_path,
        data_dir=data_dir,
        faces_dir=data_dir / "faces",
        snapshots_dir=data_dir / "snapshots",
        models_dir=BASE_DIR / "models",
        logs_dir=BASE_DIR / "logs",
    )


settings = _load()


def ensure_dirs() -> None:
    for p in (settings.data_dir, settings.faces_dir, settings.snapshots_dir,
              settings.models_dir, settings.logs_dir, settings.tts_cache_dir,
              settings.meetings_dir, settings.data_dir / "backups"):
        p.mkdir(parents=True, exist_ok=True)
