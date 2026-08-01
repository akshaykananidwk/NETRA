"""Reminder engine — APScheduler jobs that fire over voice / WhatsApp / web.

Reminders live in the `reminders` table; at startup every still-scheduled
future reminder is re-armed, so restarts never lose one. Past-due reminders
found at startup fire immediately (better late than lost).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from config import settings
from core.db import Reminder, SessionLocal, Task, now_local

log = logging.getLogger("krishna.reminders")


class ReminderEngine:
    def __init__(self, whatsapp=None, bus=None, say_callback=None) -> None:
        """say_callback: callable(text:str) scheduled on the asyncio loop."""
        self.whatsapp = whatsapp
        self.bus = bus
        self.say_callback = say_callback
        self._scheduler = None

    # ── lifecycle ─────────────────────────────────────────────────────────
    def start(self) -> None:
        from apscheduler.schedulers.background import BackgroundScheduler
        self._scheduler = BackgroundScheduler(
            timezone=settings.timezone,
            job_defaults={"misfire_grace_time": 3600, "coalesce": True})
        self._scheduler.start()
        self._rearm_all()

    def stop(self) -> None:
        if self._scheduler:
            self._scheduler.shutdown(wait=False)

    def _rearm_all(self) -> None:
        with SessionLocal() as s:
            rows = (s.query(Reminder)
                     .filter(Reminder.status == "scheduled").all())
            ids = [r.id for r in rows]
        for rid in ids:
            self._arm(rid)
        if ids:
            log.info("re-armed %d reminder(s)", len(ids))

    # ── create / cancel ───────────────────────────────────────────────────
    def create(self, message: str, remind_at: datetime,
               channel: str = "voice", task_id: Optional[int] = None,
               target_phone: Optional[str] = None) -> dict:
        with SessionLocal() as s:
            r = Reminder(task_id=task_id, remind_at=remind_at,
                         channel=channel, target_phone=target_phone,
                         message=message)
            s.add(r)
            s.commit()
            s.refresh(r)
            out = r.to_dict()
        self._arm(out["id"])
        return out

    def cancel(self, reminder_id: int) -> bool:
        with SessionLocal() as s:
            r = s.get(Reminder, reminder_id)
            if not r or r.status != "scheduled":
                return False
            r.status = "cancelled"
            s.commit()
        if self._scheduler:
            try:
                self._scheduler.remove_job(f"rem-{reminder_id}")
            except Exception:
                pass
        return True

    def _arm(self, reminder_id: int) -> None:
        if self._scheduler is None:
            return
        with SessionLocal() as s:
            r = s.get(Reminder, reminder_id)
            if not r or r.status != "scheduled":
                return
            when = r.remind_at
        run_at = max(when, now_local())
        self._scheduler.add_job(self.fire, "date", run_date=run_at,
                                args=[reminder_id], id=f"rem-{reminder_id}",
                                replace_existing=True)

    # ── firing ────────────────────────────────────────────────────────────
    def fire(self, reminder_id: int) -> None:
        try:
            self._fire(reminder_id)
        except Exception:
            log.exception("reminder %s failed to fire", reminder_id)
            with SessionLocal() as s:
                r = s.get(Reminder, reminder_id)
                if r:
                    r.status = "failed"
                    s.commit()

    def _fire(self, reminder_id: int) -> None:
        with SessionLocal() as s:
            r = s.get(Reminder, reminder_id)
            if not r or r.status != "scheduled":
                return
            task = s.get(Task, r.task_id) if r.task_id else None
            message = r.message or (task.title if task else "રિમાઇન્ડર")
            channel = r.channel or "voice"
            phone = r.target_phone
            due = str(r.remind_at)[:16]

        log.info("reminder %s firing (%s): %s", reminder_id, channel, message)
        spoken_text = f"રિમાઇન્ડર: {message}"

        if channel in ("voice", "all") and self.say_callback:
            try:
                self.say_callback(spoken_text)
            except Exception:
                log.exception("voice reminder failed")

        if channel in ("whatsapp", "all") and self.whatsapp:
            from actions.whatsapp import TEMPLATES
            to = phone or settings.wa_admin_number
            if to:
                self.whatsapp.send_text(
                    to, TEMPLATES["reminder"].format(task_title=message, due=due),
                    purpose="reminder")

        if self.bus is not None:
            self.bus.publish("reminder.fired",
                             reminder_id=reminder_id, message=message,
                             channel=channel)

        with SessionLocal() as s:
            r = s.get(Reminder, reminder_id)
            if r:
                r.status = "sent"
                r.sent_at = now_local()
                s.commit()


def todays_reminders() -> list:
    from datetime import timedelta
    from core.db import today_str
    day = datetime.strptime(today_str(), "%Y-%m-%d")
    with SessionLocal() as s:
        rows = (s.query(Reminder)
                 .filter(Reminder.remind_at >= day,
                         Reminder.remind_at < day + timedelta(days=1))
                 .order_by(Reminder.remind_at).all())
        return [r.to_dict() for r in rows]
