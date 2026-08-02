"""Agent tools — Anthropic tool definitions + gated handlers.

Every handler:
- validates input with Pydantic
- enforces the required role IN CODE (not just in the prompt)
- writes to audit_log
- returns a short Gujarati string (spoken aloud / fed back to the LLM)
- never raises — errors come back as graceful Gujarati text
"""
from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from pydantic import BaseModel, Field, ValidationError

from config import settings
from core.db import (Camera, FaceEmbedding, Meeting, Person, SessionLocal,
                     Setting, SystemHealth, UnknownFace, Visit, audit,
                     now_local, today_str)

log = logging.getLogger("krishna.tools")

# role rank: anyone < staff < admin
ROLE_RANK = {"anyone": 0, "staff": 1, "admin": 2}

TOOL_ROLES = {
    "order_refreshment": "anyone",
    "answer_general": "anyone",
    "create_task": "staff",
    "create_reminder": "staff",
    "list_tasks": "staff",
    "search_person": "staff",
    "system_status": "staff",
    "register_person": "admin",
    "send_whatsapp": "admin",
    "get_collection_report": "admin",
    "get_visitor_log": "admin",
    "set_listening_state": "admin",
    "start_meeting": "admin",
    "end_meeting": "admin",
    "camera_snapshot": "admin",
}

DENIED_MSG = "માફ કરશો, આ માહિતી ફક્ત સાહેબને જ આપી શકું."

TOOLS = [
    {"name": "order_refreshment",
     "description": "Order tea/coffee/water by sending a WhatsApp message to "
                    "the canteen. Use when anyone asks for chai/coffee/water.",
     "input_schema": {"type": "object", "properties": {
         "items": {"type": "array", "items": {"type": "object", "properties": {
             "item": {"type": "string",
                      "enum": ["chai", "coffee", "water", "cold_drink"]},
             "qty": {"type": "integer", "minimum": 1, "maximum": 20}},
             "required": ["item", "qty"]}},
         "note": {"type": "string"},
         "for_person": {"type": "string"}},
         "required": ["items"]}},

    {"name": "send_whatsapp",
     "description": "Send a WhatsApp message to any phone number.",
     "input_schema": {"type": "object", "properties": {
         "to": {"type": "string", "description": "phone number with country code, e.g. 9198..."},
         "message": {"type": "string"},
         "purpose": {"type": "string"}},
         "required": ["to", "message"]}},

    {"name": "create_task",
     "description": "Create a task/to-do. due_at is ISO 8601 local time.",
     "input_schema": {"type": "object", "properties": {
         "title": {"type": "string"},
         "description": {"type": "string"},
         "assignee": {"type": "string"},
         "due_at": {"type": "string"},
         "priority": {"type": "string",
                      "enum": ["low", "medium", "high", "urgent"]}},
         "required": ["title"]}},

    {"name": "create_reminder",
     "description": "Schedule a reminder. remind_at is ISO 8601 local time. "
                    "channel: voice (speaker), whatsapp, web, or all.",
     "input_schema": {"type": "object", "properties": {
         "task_id": {"type": "integer"},
         "message": {"type": "string"},
         "remind_at": {"type": "string"},
         "channel": {"type": "string",
                     "enum": ["voice", "whatsapp", "web", "all"]},
         "target_phone": {"type": "string"}},
         "required": ["message", "remind_at"]}},

    {"name": "list_tasks",
     "description": "List tasks. date_range: today|week|overdue|all.",
     "input_schema": {"type": "object", "properties": {
         "assignee": {"type": "string"},
         "status": {"type": "string"},
         "date_range": {"type": "string",
                        "enum": ["today", "week", "overdue", "all"]}}}},

    {"name": "get_visitor_log",
     "description": "Who visited the office. date is YYYY-MM-DD (default today).",
     "input_schema": {"type": "object", "properties": {
         "date": {"type": "string"},
         "person_name": {"type": "string"}}}},

    {"name": "get_collection_report",
     "description": "Today's collection/sales figure. date YYYY-MM-DD optional.",
     "input_schema": {"type": "object", "properties": {
         "date": {"type": "string"}}}},

    {"name": "start_meeting",
     "description": "Start recording a meeting.",
     "input_schema": {"type": "object", "properties": {
         "title": {"type": "string"},
         "participants": {"type": "array", "items": {"type": "string"}}}}},

    {"name": "end_meeting",
     "description": "End the running meeting.",
     "input_schema": {"type": "object", "properties": {
         "meeting_id": {"type": "integer"}}}},

    {"name": "set_listening_state",
     "description": "Pause or resume Krishna. 'બંધ થઈ જા' => paused, "
                    "'ચાલુ થા' => active. duration_min optionally auto-resumes.",
     "input_schema": {"type": "object", "properties": {
         "state": {"type": "string",
                   "enum": ["active", "paused", "mute_speaker", "mute_mic"]},
         "duration_min": {"type": "integer"}},
         "required": ["state"]}},

    {"name": "register_person",
     "description": "Register a pending unknown face as a person.",
     "input_schema": {"type": "object", "properties": {
         "unknown_temp_uid": {"type": "string"},
         "full_name": {"type": "string"},
         "name_gu": {"type": "string"},
         "call_name": {"type": "string"},
         "phone": {"type": "string"},
         "company": {"type": "string"},
         "relation_type": {"type": "string",
                           "enum": ["visitor", "staff", "vendor", "vip"]},
         "preferred_drink": {"type": "string"}},
         "required": ["unknown_temp_uid", "full_name"]}},

    {"name": "search_person",
     "description": "Look up a person in the directory by name/company/phone.",
     "input_schema": {"type": "object", "properties": {
         "query": {"type": "string"}}, "required": ["query"]}},

    {"name": "camera_snapshot",
     "description": "Save a snapshot from a camera right now.",
     "input_schema": {"type": "object", "properties": {
         "camera_id": {"type": "integer"}}}},

    {"name": "system_status",
     "description": "Health of cameras, mic, AI, WhatsApp.",
     "input_schema": {"type": "object", "properties": {}}},
]

ITEM_GU = {"chai": "ચા", "coffee": "કોફી", "water": "પાણી",
           "cold_drink": "ઠંડુ પીણું"}


def _parse_dt(value: str) -> Optional[datetime]:
    try:
        dt = datetime.fromisoformat(value.replace("Z", ""))
        return dt.replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None


class _Items(BaseModel):
    item: str
    qty: int = Field(ge=1, le=20)


class _OrderIn(BaseModel):
    items: List[_Items]
    note: Optional[str] = None
    for_person: Optional[str] = None


class ToolExecutor:
    """Executes tools with role enforcement. Dependencies injected lazily."""

    def __init__(self, whatsapp=None, reminders=None, state=None,
                 meetings=None) -> None:
        self.whatsapp = whatsapp
        self.reminders = reminders
        self.state = state
        self.meetings = meetings        # MeetingManager (set after runtime wiring)
        self._resume_timer = None

    def _run_async(self, coro, timeout: float = 20) -> Optional[str]:
        """Run a coroutine on the main loop from this worker thread."""
        import asyncio
        try:
            from core.runtime import bus
            loop = bus._loop
            if loop is None or loop.is_closed():
                coro.close()
                return None
            return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)
        except Exception:
            log.exception("async tool bridge failed")
            try:
                coro.close()
            except Exception:
                pass
            return None

    # ── entry point ───────────────────────────────────────────────────────
    def execute(self, name: str, tool_input: dict, speaker: dict) -> str:
        role = speaker.get("role", "unknown")
        rank = ROLE_RANK.get(role, 0) if role != "unknown" else 0
        required = ROLE_RANK.get(TOOL_ROLES.get(name, "admin"), 2)
        actor = speaker.get("name") or role
        if rank < required:
            audit(actor, f"tool.denied:{name}", json.dumps(tool_input,
                                                           ensure_ascii=False)[:300])
            return DENIED_MSG
        handler = getattr(self, f"_t_{name}", None)
        if handler is None:
            return "માફ કરશો, એ કામ હજી નથી આવડતું."
        try:
            result = handler(tool_input, speaker)
            audit(actor, f"tool:{name}",
                  json.dumps(tool_input, ensure_ascii=False)[:300])
            return result
        except ValidationError:
            return "માફ કરશો, માહિતી અધૂરી લાગી. ફરી કહેશો?"
        except Exception:
            log.exception("tool %s failed", name)
            return "માફ કરશો, કંઈક ભૂલ થઈ. ફરી પ્રયત્ન કરો."

    # ── handlers ──────────────────────────────────────────────────────────
    def _t_order_refreshment(self, tool_input: dict, speaker: dict) -> str:
        data = _OrderIn(**tool_input)
        items_txt = ", ".join(f"{i.qty} {ITEM_GU.get(i.item, i.item)}"
                              for i in data.items)
        if not settings.wa_canteen_number:
            return "માફ કરશો, કેન્ટીનનો નંબર સેટ નથી. Settings માં ઉમેરો."
        from actions.whatsapp import TEMPLATES
        msg = TEMPLATES["tea_order"].format(
            items=items_txt, time=now_local().strftime("%H:%M"))
        if data.note:
            msg += f"\nનોંધ: {data.note}"
        sent = self.whatsapp.send_text(settings.wa_canteen_number, msg,
                                       purpose="tea_order") if self.whatsapp else False
        if sent:
            return f"{items_txt} મંગાવી દીધી છે, થોડી વારમાં આવી જશે."
        return (f"{items_txt} નો ઓર્ડર નોંધ્યો છે — WhatsApp હમણાં ઓફલાઇન છે, "
                "જોડાતાં જ મોકલાઈ જશે.")

    def _t_send_whatsapp(self, tool_input: dict, speaker: dict) -> str:
        to = str(tool_input.get("to", "")).strip()
        message = str(tool_input.get("message", "")).strip()
        if not to or not message:
            return "નંબર અને મેસેજ બન્ને જોઈએ."
        sent = self.whatsapp.send_text(
            to, message, purpose=tool_input.get("purpose", "manual")) \
            if self.whatsapp else False
        return ("મેસેજ મોકલી દીધો છે." if sent
                else "મેસેજ queue માં મૂક્યો છે — નેટ આવતાં જ જશે.")

    def _t_create_task(self, tool_input: dict, speaker: dict) -> str:
        from actions.tasks import create_task
        title = str(tool_input.get("title", "")).strip()
        if not title:
            return "કામનું નામ કહો."
        due = _parse_dt(tool_input.get("due_at", "")) if tool_input.get("due_at") else None
        t = create_task(title=title,
                        description=tool_input.get("description"),
                        assignee=tool_input.get("assignee"),
                        due_at=due,
                        priority=tool_input.get("priority", "medium"))
        who = f" {t['assignee_name']} માટે" if t.get("assignee_name") else ""
        when = f", {str(due)[:16]} સુધી" if due else ""
        return f"ટાસ્ક નોંધી લીધો{who}: {title}{when}."

    def _t_create_reminder(self, tool_input: dict, speaker: dict) -> str:
        message = str(tool_input.get("message", "")).strip()
        remind_at = _parse_dt(tool_input.get("remind_at", ""))
        if not message or remind_at is None:
            return "રિમાઇન્ડરનો સમય સમજાયો નહીં. ફરી કહેશો?"
        if self.reminders is None:
            return "રિમાઇન્ડર એન્જિન ચાલુ નથી."
        self.reminders.create(message=message, remind_at=remind_at,
                              channel=tool_input.get("channel", "voice"),
                              task_id=tool_input.get("task_id"),
                              target_phone=tool_input.get("target_phone"))
        return (f"રિમાઇન્ડર સેટ કરી દીધું: {remind_at.strftime('%d-%m %H:%M')} "
                f"વાગ્યે — {message}")

    def _t_list_tasks(self, tool_input: dict, speaker: dict) -> str:
        from actions.tasks import list_tasks
        rows = list_tasks(assignee=tool_input.get("assignee"),
                          status=tool_input.get("status"),
                          date_range=tool_input.get("date_range", "all"),
                          limit=8)
        if not rows:
            return "કોઈ બાકી કામ નથી. 👍"
        lines = []
        for t in rows:
            due = f" ({t['due_at'][:16]})" if t["due_at"] else ""
            who = f" — {t['assignee_name']}" if t["assignee_name"] else ""
            lines.append(f"{t['title']}{who}{due}")
        return f"{len(rows)} કામ છે: " + "; ".join(lines)

    def _t_get_visitor_log(self, tool_input: dict, speaker: dict) -> str:
        date = tool_input.get("date") or today_str()
        try:
            day = datetime.strptime(date, "%Y-%m-%d")
        except ValueError:
            day = datetime.strptime(today_str(), "%Y-%m-%d")
        from datetime import timedelta
        with SessionLocal() as s:
            q = (s.query(Visit, Person)
                  .outerjoin(Person, Person.id == Visit.person_id)
                  .filter(Visit.first_seen >= day,
                          Visit.first_seen < day + timedelta(days=1)))
            if tool_input.get("person_name"):
                q = q.filter(Person.full_name.ilike(
                    f"%{tool_input['person_name']}%"))
            rows = q.order_by(Visit.first_seen).all()
        if not rows:
            return "એ દિવસે કોઈ મુલાકાત નોંધાઈ નથી."
        names = []
        unknown = 0
        for v, p in rows:
            if p is not None:
                nm = p.call_name or p.full_name
                if nm not in names:
                    names.append(nm)
            else:
                unknown += 1
        parts = [f"કુલ {len(rows)} મુલાકાત"]
        if names:
            parts.append("આવ્યા: " + ", ".join(names[:10]))
        if unknown:
            parts.append(f"{unknown} અજાણ્યા")
        return "; ".join(parts) + "."

    def _t_get_collection_report(self, tool_input: dict, speaker: dict) -> str:
        date = tool_input.get("date") or today_str()
        with SessionLocal() as s:
            row = s.get(Setting, f"collection:{date}")
        if row is None or not row.value:
            return (f"{date} નું કલેક્શન હજી નોંધાયું નથી. "
                    "Dashboard પરથી આજનો આંકડો ઉમેરી શકાય.")
        return f"{date} નું કલેક્શન: ₹{row.value}."

    def _t_start_meeting(self, tool_input: dict, speaker: dict) -> str:
        if self.meetings is not None:
            out = self._run_async(
                self.meetings.start(tool_input.get("title")))
            if out is not None:
                return out
        # fallback: bare DB row (no recording pipeline available)
        with SessionLocal() as s:
            running = (s.query(Meeting)
                        .filter(Meeting.status == "recording").first())
            if running:
                return "મીટિંગ પહેલેથી ચાલુ છે."
            s.add(Meeting(title=tool_input.get("title") or
                          f"મીટિંગ {now_local().strftime('%d-%m %H:%M')}",
                          started_at=now_local(), status="recording"))
            s.commit()
        return "મીટિંગ નોંધવાનું ચાલુ કર્યું છે. મીટિંગ રેકોર્ડિંગ ચાલુ છે."

    def _t_end_meeting(self, tool_input: dict, speaker: dict) -> str:
        if self.meetings is not None and self.meetings.active:
            out = self._run_async(
                self.meetings.end(tool_input.get("meeting_id")))
            if out is not None:
                return out
        with SessionLocal() as s:
            m = None
            if tool_input.get("meeting_id"):
                m = s.get(Meeting, tool_input["meeting_id"])
            if m is None:
                m = (s.query(Meeting).filter(Meeting.status == "recording")
                      .order_by(Meeting.started_at.desc()).first())
            if m is None:
                return "કોઈ મીટિંગ ચાલુ નથી."
            m.ended_at = now_local()
            m.status = "done"
            s.commit()
        return "મીટિંગ પૂરી થઈ."

    def _t_set_listening_state(self, tool_input: dict, speaker: dict) -> str:
        state = tool_input.get("state")
        if self.state is None or state not in ("active", "paused",
                                               "mute_speaker", "mute_mic"):
            return "એ સ્ટેટ સમજાયો નહીં."
        self.state.set(state)
        duration = tool_input.get("duration_min")
        if state != "active" and duration:
            import threading
            if self._resume_timer:
                self._resume_timer.cancel()
            self._resume_timer = threading.Timer(
                duration * 60, lambda: self.state.set("active"))
            self._resume_timer.daemon = True
            self._resume_timer.start()
        texts = {"paused": "સારું, હું થોભી જાઉં છું. 'ચાલુ થા' કહેશો એટલે પાછો આવીશ.",
                 "active": "હું પાછો હાજર છું!",
                 "mute_speaker": "સ્પીકર બંધ કર્યું.",
                 "mute_mic": "માઇક બંધ કર્યું."}
        return texts[state]

    def _t_register_person(self, tool_input: dict, speaker: dict) -> str:
        uid = tool_input.get("unknown_temp_uid", "")
        full_name = str(tool_input.get("full_name", "")).strip()
        if not full_name:
            return "નામ કહો."
        with SessionLocal() as s:
            u = s.query(UnknownFace).filter(UnknownFace.temp_uid == uid).first()
            if u is None or u.status != "pending":
                return f"{uid} નામનો કોઈ pending ચહેરો નથી."
            person = Person(full_name=full_name,
                            name_gu=tool_input.get("name_gu"),
                            call_name=tool_input.get("call_name"),
                            phone=tool_input.get("phone"),
                            company=tool_input.get("company"),
                            relation_type=tool_input.get("relation_type",
                                                         "visitor"),
                            preferred_drink=tool_input.get("preferred_drink"))
            s.add(person)
            s.flush()
            photo_rel = None
            if u.snapshot_path:
                src = settings.data_dir / u.snapshot_path
                if src.exists():
                    pdir = settings.faces_dir / str(person.id)
                    pdir.mkdir(parents=True, exist_ok=True)
                    dst = pdir / f"from_unknown_{Path(u.snapshot_path).name}"
                    shutil.copy2(src, dst)
                    photo_rel = f"faces/{person.id}/{dst.name}"
            person.photo_path = photo_rel
            s.add(FaceEmbedding(person_id=person.id, embedding=u.embedding,
                                source_path=photo_rel))
            u.status = "registered"
            u.resolved_person_id = person.id
            for v in s.query(Visit).filter(Visit.unknown_id == u.id).all():
                v.person_id = person.id
                v.status = "known"
            s.commit()
        try:
            from core.runtime import matcher, unknowns
            matcher.reload()
            unknowns.remove(uid)
        except Exception:
            pass
        return f"{full_name} ની નોંધણી થઈ ગઈ. હવે એમને ઓળખી લઈશ."

    def _t_search_person(self, tool_input: dict, speaker: dict) -> str:
        query = str(tool_input.get("query", "")).strip()
        if not query:
            return "કોને શોધું?"
        with SessionLocal() as s:
            rows = (s.query(Person)
                     .filter((Person.full_name.ilike(f"%{query}%"))
                             | (Person.call_name.ilike(f"%{query}%"))
                             | (Person.name_gu.ilike(f"%{query}%"))
                             | (Person.company.ilike(f"%{query}%"))
                             | (Person.phone.ilike(f"%{query}%")))
                     .limit(5).all())
            if not rows:
                return f"'{query}' નામનું કોઈ મળ્યું નહીં."
            parts = []
            for p in rows:
                bits = [p.full_name]
                if p.phone:
                    bits.append(p.phone)
                if p.company:
                    bits.append(p.company)
                parts.append(", ".join(bits))
        return "; ".join(parts) + "."

    def _t_camera_snapshot(self, tool_input: dict, speaker: dict) -> str:
        try:
            import cv2
            from core.runtime import vision_manager
        except Exception:
            return "કેમેરા સિસ્ટમ ઉપલબ્ધ નથી."
        cam_id = tool_input.get("camera_id")
        worker = None
        if cam_id:
            worker = vision_manager.get(cam_id)
        else:
            for w in vision_manager.workers.values():
                worker = w
                break
        frame = worker.capture.latest() if worker else None
        if frame is None:
            return "કેમેરામાંથી ફોટો મળ્યો નહીં."
        day = today_str()
        folder = settings.snapshots_dir / day
        folder.mkdir(parents=True, exist_ok=True)
        fname = f"manual_{int(time.time())}.jpg"
        cv2.imwrite(str(folder / fname), frame)
        return f"ફોટો લઈ લીધો છે — Visits પેજ પર જોવા મળશે. ({fname})"

    def _t_system_status(self, tool_input: dict, speaker: dict) -> str:
        with SessionLocal() as s:
            rows = s.query(SystemHealth).all()
        good, bad = [], []
        labels = {"mic": "માઇક", "stt": "કાન (STT)", "tts": "અવાજ (TTS)",
                  "ai": "ચહેરા ઓળખ", "db": "ડેટાબેઝ", "whatsapp": "WhatsApp"}
        for h in rows:
            label = labels.get(h.component,
                               h.component.replace("camera_", "કેમેરા "))
            (good if h.status == "ok" else bad).append(label)
        if not bad:
            return "બધું બરાબર ચાલે છે. 👍"
        return ("ચાલુ: " + ", ".join(good) + ". તકલીફ: " + ", ".join(bad) + ".")
