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
              settings.models_dir, settings.logs_dir, settings.data_dir / "backups"):
        p.mkdir(parents=True, exist_ok=True)
