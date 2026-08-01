"""Task CRUD used by the agent tools and the REST API."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import List, Optional

from core.db import Person, SessionLocal, Task, now_local, today_str

log = logging.getLogger("krishna.tasks")

PRIORITIES = ("low", "medium", "high", "urgent")
STATUSES = ("open", "in_progress", "done", "cancelled")


def _resolve_assignee(s, assignee: Optional[str]):
    """Match a spoken name against the person directory (id, display-name)."""
    if not assignee:
        return None, None
    name = assignee.strip()
    person = (s.query(Person)
               .filter((Person.full_name.ilike(f"%{name}%"))
                       | (Person.call_name.ilike(f"%{name}%"))
                       | (Person.name_gu.ilike(f"%{name}%"))).first())
    if person:
        return person.id, person.full_name
    return None, name


def create_task(title: str, description: Optional[str] = None,
                assignee: Optional[str] = None,
                due_at: Optional[datetime] = None,
                priority: str = "medium", source: str = "voice",
                meeting_id: Optional[int] = None) -> dict:
    if priority not in PRIORITIES:
        priority = "medium"
    with SessionLocal() as s:
        assignee_id, assignee_name = _resolve_assignee(s, assignee)
        t = Task(title=title.strip(), description=description,
                 assignee_id=assignee_id, assignee_name=assignee_name,
                 due_at=due_at, priority=priority, source=source,
                 meeting_id=meeting_id)
        s.add(t)
        s.commit()
        s.refresh(t)
        return t.to_dict()


def list_tasks(assignee: Optional[str] = None, status: Optional[str] = None,
               date_range: str = "all", limit: int = 50) -> List[dict]:
    with SessionLocal() as s:
        q = s.query(Task)
        if status and status != "all":
            q = q.filter(Task.status == status)
        elif date_range != "all":
            q = q.filter(Task.status.in_(("open", "in_progress")))
        if assignee:
            aid, aname = _resolve_assignee(s, assignee)
            if aid:
                q = q.filter(Task.assignee_id == aid)
            else:
                q = q.filter(Task.assignee_name.ilike(f"%{aname}%"))
        now = now_local()
        if date_range == "today":
            day = datetime.strptime(today_str(), "%Y-%m-%d")
            q = q.filter(Task.due_at.isnot(None), Task.due_at >= day,
                         Task.due_at < day + timedelta(days=1))
        elif date_range == "week":
            q = q.filter(Task.due_at.isnot(None),
                         Task.due_at < now + timedelta(days=7))
        elif date_range == "overdue":
            q = q.filter(Task.due_at.isnot(None), Task.due_at < now,
                         Task.status.in_(("open", "in_progress")))
        rows = q.order_by(Task.due_at.isnot(None).desc(), Task.due_at,
                          Task.created_at.desc()).limit(limit).all()
        return [t.to_dict() for t in rows]


def update_task(task_id: int, **fields) -> Optional[dict]:
    with SessionLocal() as s:
        t = s.get(Task, task_id)
        if not t:
            return None
        for k, v in fields.items():
            if k == "status" and v not in STATUSES:
                continue
            if k == "priority" and v not in PRIORITIES:
                continue
            setattr(t, k, v)
        if fields.get("status") == "done" and t.completed_at is None:
            t.completed_at = now_local()
        s.commit()
        return t.to_dict()


def delete_task(task_id: int) -> bool:
    with SessionLocal() as s:
        t = s.get(Task, task_id)
        if not t:
            return False
        s.delete(t)
        s.commit()
        return True


def open_task_count() -> int:
    with SessionLocal() as s:
        return s.query(Task).filter(Task.status.in_(("open", "in_progress"))).count()
