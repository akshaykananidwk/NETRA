"""Tasks + reminders REST API."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from actions import tasks as task_actions
from actions.reminders import todays_reminders

router = APIRouter(prefix="/api")


def _dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "")).replace(tzinfo=None)
    except ValueError:
        raise HTTPException(422, f"bad datetime: {value}")


class TaskIn(BaseModel):
    title: str = Field(min_length=1)
    description: Optional[str] = None
    assignee: Optional[str] = None
    due_at: Optional[str] = None
    priority: str = "medium"


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    due_at: Optional[str] = None
    priority: Optional[str] = None
    status: Optional[str] = None


class ReminderIn(BaseModel):
    message: str = Field(min_length=1)
    remind_at: str
    channel: str = "voice"
    task_id: Optional[int] = None
    target_phone: Optional[str] = None


@router.get("/tasks")
def get_tasks(status: Optional[str] = None, assignee: Optional[str] = None,
              range: str = "all"):
    return task_actions.list_tasks(assignee=assignee, status=status,
                                   date_range=range, limit=200)


@router.post("/tasks")
def post_task(body: TaskIn):
    return task_actions.create_task(
        title=body.title, description=body.description,
        assignee=body.assignee, due_at=_dt(body.due_at),
        priority=body.priority, source="manual")


@router.put("/tasks/{task_id}")
def put_task(task_id: int, body: TaskUpdate):
    fields = body.model_dump(exclude_unset=True)
    if "due_at" in fields:
        fields["due_at"] = _dt(fields["due_at"])
    out = task_actions.update_task(task_id, **fields)
    if out is None:
        raise HTTPException(404, "task not found")
    return out


@router.delete("/tasks/{task_id}")
def del_task(task_id: int):
    if not task_actions.delete_task(task_id):
        raise HTTPException(404, "task not found")
    return {"deleted": task_id}


@router.get("/reminders/today")
def reminders_today():
    return todays_reminders()


@router.post("/reminders")
def post_reminder(body: ReminderIn):
    from core.runtime import reminders
    when = _dt(body.remind_at)
    if when is None:
        raise HTTPException(422, "remind_at required")
    return reminders.create(message=body.message, remind_at=when,
                            channel=body.channel, task_id=body.task_id,
                            target_phone=body.target_phone)


@router.delete("/reminders/{reminder_id}")
def cancel_reminder(reminder_id: int):
    from core.runtime import reminders
    if not reminders.cancel(reminder_id):
        raise HTTPException(404, "reminder not found / already sent")
    return {"cancelled": reminder_id}
