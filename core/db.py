"""SQLAlchemy models + session management for SQLite (WAL mode).

The authoritative schema lives in core/schema.sql (idempotent).
init_db() executes it, then the ORM models below map onto those tables.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import (Column, DateTime, Float, ForeignKey, Integer,
                        LargeBinary, Text, create_engine, event, text)
from sqlalchemy.orm import declarative_base, sessionmaker

from config import settings, ensure_dirs

log = logging.getLogger("krishna.db")

Base = declarative_base()
engine = None
SessionLocal = sessionmaker(autocommit=False, autoflush=False, expire_on_commit=False)

try:
    _TZ = ZoneInfo(settings.timezone)
except Exception:
    # Windows Python has no built-in IANA tz database (needs the `tzdata`
    # package). Fall back to the system clock rather than crashing —
    # the office PC's clock is IST anyway.
    _TZ = None
    logging.getLogger("krishna.db").warning(
        "timezone %s unavailable (install `tzdata`) — using system time",
        settings.timezone)


def now_local() -> datetime:
    """Naive local (office) time — all app timestamps use this."""
    if _TZ is None:
        return datetime.now().replace(microsecond=0)
    return datetime.now(_TZ).replace(tzinfo=None, microsecond=0)


def today_str() -> str:
    return now_local().strftime("%Y-%m-%d")


# ── models ────────────────────────────────────────────────────────────────

class Person(Base):
    __tablename__ = "persons"
    id = Column(Integer, primary_key=True)
    full_name = Column(Text, nullable=False)
    name_gu = Column(Text)
    call_name = Column(Text)
    phone = Column(Text)
    company = Column(Text)
    designation = Column(Text)
    relation_type = Column(Text, nullable=False, default="visitor")
    is_admin = Column(Integer, default=0)
    preferred_drink = Column(Text)
    notes = Column(Text)
    photo_path = Column(Text)
    greeting_enabled = Column(Integer, default=1)
    created_at = Column(DateTime, default=now_local)
    updated_at = Column(DateTime, default=now_local, onupdate=now_local)

    def to_dict(self, face_count: int | None = None) -> dict:
        d = {
            "id": self.id, "full_name": self.full_name, "name_gu": self.name_gu,
            "call_name": self.call_name, "phone": self.phone, "company": self.company,
            "designation": self.designation, "relation_type": self.relation_type,
            "is_admin": bool(self.is_admin), "preferred_drink": self.preferred_drink,
            "notes": self.notes, "photo_path": self.photo_path,
            "greeting_enabled": bool(self.greeting_enabled),
            "created_at": str(self.created_at or ""),
        }
        if face_count is not None:
            d["face_count"] = face_count
        return d


class FaceEmbedding(Base):
    __tablename__ = "face_embeddings"
    id = Column(Integer, primary_key=True)
    person_id = Column(Integer, ForeignKey("persons.id", ondelete="CASCADE"), nullable=False)
    embedding = Column(LargeBinary, nullable=False)
    quality = Column(Float)
    source_path = Column(Text)
    created_at = Column(DateTime, default=now_local)


class VoicePrint(Base):
    __tablename__ = "voice_prints"
    id = Column(Integer, primary_key=True)
    person_id = Column(Integer, ForeignKey("persons.id", ondelete="CASCADE"), nullable=False)
    embedding = Column(LargeBinary, nullable=False)
    sample_path = Column(Text)
    created_at = Column(DateTime, default=now_local)


class Camera(Base):
    __tablename__ = "cameras"
    id = Column(Integer, primary_key=True)
    name = Column(Text, nullable=False)
    source_type = Column(Text, nullable=False, default="webcam")
    source_url = Column(Text, nullable=False, default="0")
    zone = Column(Text)
    greet_here = Column(Integer, default=1)
    enabled = Column(Integer, default=1)
    last_frame_at = Column(DateTime)
    status = Column(Text, default="unknown")

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "source_type": self.source_type,
            "source_url": self.source_url, "zone": self.zone,
            "greet_here": bool(self.greet_here), "enabled": bool(self.enabled),
            "last_frame_at": str(self.last_frame_at or ""), "status": self.status,
        }


class UnknownFace(Base):
    __tablename__ = "unknown_faces"
    id = Column(Integer, primary_key=True)
    temp_uid = Column(Text, unique=True)
    embedding = Column(LargeBinary, nullable=False)
    snapshot_path = Column(Text)
    spoken_name = Column(Text)
    ask_count = Column(Integer, default=0)
    first_seen = Column(DateTime)
    last_seen = Column(DateTime)
    resolved_person_id = Column(Integer, ForeignKey("persons.id"))
    status = Column(Text, default="pending")

    def to_dict(self) -> dict:
        return {
            "id": self.id, "temp_uid": self.temp_uid,
            "snapshot_path": self.snapshot_path, "spoken_name": self.spoken_name,
            "ask_count": self.ask_count, "first_seen": str(self.first_seen or ""),
            "last_seen": str(self.last_seen or ""), "status": self.status,
        }


class Visit(Base):
    __tablename__ = "visits"
    id = Column(Integer, primary_key=True)
    person_id = Column(Integer, ForeignKey("persons.id"))
    unknown_id = Column(Integer, ForeignKey("unknown_faces.id"))
    camera_id = Column(Integer, ForeignKey("cameras.id"))
    first_seen = Column(DateTime, nullable=False)
    last_seen = Column(DateTime, nullable=False)
    confidence = Column(Float)
    snapshot_path = Column(Text)
    greeted = Column(Integer, default=0)
    drink_served = Column(Text)
    status = Column(Text, default="known")


class Meeting(Base):
    __tablename__ = "meetings"
    id = Column(Integer, primary_key=True)
    title = Column(Text)
    started_at = Column(DateTime, nullable=False)
    ended_at = Column(DateTime)
    camera_id = Column(Integer)
    audio_path = Column(Text)
    language = Column(Text, default="gu")
    summary = Column(Text)
    key_points = Column(Text)
    decisions = Column(Text)
    status = Column(Text, default="recording")


class MeetingParticipant(Base):
    __tablename__ = "meeting_participants"
    __mapper_args__ = {"primary_key": ["meeting_id", "person_id"]}
    meeting_id = Column(Integer, ForeignKey("meetings.id", ondelete="CASCADE"),
                        primary_key=True)
    person_id = Column(Integer, ForeignKey("persons.id"), primary_key=True)
    detected_by = Column(Text)


class TranscriptSegment(Base):
    __tablename__ = "transcript_segments"
    id = Column(Integer, primary_key=True)
    meeting_id = Column(Integer, ForeignKey("meetings.id", ondelete="CASCADE"))
    speaker_label = Column(Text)
    person_id = Column(Integer, ForeignKey("persons.id"))
    start_ms = Column(Integer)
    end_ms = Column(Integer)
    text = Column(Text, nullable=False)
    confidence = Column(Float)


class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    title = Column(Text, nullable=False)
    description = Column(Text)
    assignee_id = Column(Integer, ForeignKey("persons.id"))
    assignee_name = Column(Text)
    due_at = Column(DateTime)
    priority = Column(Text, default="medium")
    status = Column(Text, default="open")
    source = Column(Text, default="voice")
    meeting_id = Column(Integer, ForeignKey("meetings.id"))
    created_at = Column(DateTime, default=now_local)
    completed_at = Column(DateTime)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "description": self.description,
            "assignee_id": self.assignee_id, "assignee_name": self.assignee_name,
            "due_at": str(self.due_at or ""), "priority": self.priority,
            "status": self.status, "source": self.source,
            "created_at": str(self.created_at or ""),
            "completed_at": str(self.completed_at or ""),
        }


class Reminder(Base):
    __tablename__ = "reminders"
    id = Column(Integer, primary_key=True)
    task_id = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"))
    remind_at = Column(DateTime, nullable=False)
    channel = Column(Text, default="voice")
    target_phone = Column(Text)
    message = Column(Text)
    status = Column(Text, default="scheduled")
    sent_at = Column(DateTime)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "task_id": self.task_id,
            "remind_at": str(self.remind_at or ""), "channel": self.channel,
            "message": self.message, "status": self.status,
            "sent_at": str(self.sent_at or ""),
        }


class Conversation(Base):
    __tablename__ = "conversations"
    id = Column(Integer, primary_key=True)
    person_id = Column(Integer, ForeignKey("persons.id"))
    channel = Column(Text, default="voice")
    user_text = Column(Text)
    agent_text = Column(Text)
    tool_calls = Column(Text)
    latency_ms = Column(Integer)
    created_at = Column(DateTime, default=now_local)


class WhatsAppLog(Base):
    __tablename__ = "whatsapp_log"
    id = Column(Integer, primary_key=True)
    to_number = Column(Text, nullable=False)
    message = Column(Text)
    purpose = Column(Text)
    status = Column(Text)
    response = Column(Text)
    created_at = Column(DateTime, default=now_local)


class SystemHealth(Base):
    __tablename__ = "system_health"
    component = Column(Text, primary_key=True)
    status = Column(Text)
    detail = Column(Text)
    updated_at = Column(DateTime, default=now_local)


class Setting(Base):
    __tablename__ = "settings"
    key = Column(Text, primary_key=True)
    value = Column(Text)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id = Column(Integer, primary_key=True)
    actor = Column(Text)
    action = Column(Text)
    detail = Column(Text)
    created_at = Column(DateTime, default=now_local)


# ── init / helpers ────────────────────────────────────────────────────────

def init_db(db_path: Path | None = None) -> None:
    """Create data dirs, apply schema.sql, bind the ORM session factory."""
    global engine
    ensure_dirs()
    path = Path(db_path or settings.db_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    schema_sql = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    raw = sqlite3.connect(str(path))
    try:
        raw.executescript(schema_sql)
        raw.commit()
    finally:
        raw.close()

    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": 15},
    )

    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.close()

    SessionLocal.configure(bind=engine)
    log.info("database ready at %s", path)


def seed_defaults() -> None:
    """Insert a default webcam camera if no cameras exist yet."""
    with SessionLocal() as s:
        if s.query(Camera).count() == 0:
            s.add(Camera(name="Reception", source_type="webcam", source_url="0",
                         zone="reception", greet_here=1, enabled=1))
            s.commit()
            log.info("seeded default webcam camera")


def apply_db_settings() -> None:
    """Overlay values saved from the Settings page onto config.settings."""
    with SessionLocal() as s:
        for row in s.query(Setting).all():
            key = row.key.lower()
            typ = settings.RUNTIME_EDITABLE.get(key)
            if typ is None:
                continue
            try:
                setattr(settings, key, typ(row.value))
            except (TypeError, ValueError):
                log.warning("bad settings value %s=%r", row.key, row.value)


def set_health(component: str, status: str, detail: str = "") -> None:
    with SessionLocal() as s:
        row = s.get(SystemHealth, component)
        if row is None:
            row = SystemHealth(component=component)
            s.add(row)
        row.status = status
        row.detail = detail
        row.updated_at = now_local()
        s.commit()


def audit(actor: str, action: str, detail: str = "") -> None:
    try:
        with SessionLocal() as s:
            s.add(AuditLog(actor=actor, action=action, detail=detail))
            s.commit()
    except Exception:
        log.exception("audit write failed")
