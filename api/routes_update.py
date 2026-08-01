"""GitHub one-click update endpoints."""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core.updater import UpdateError

log = logging.getLogger("krishna.api.update")
router = APIRouter(prefix="/api/update")


class ConfigIn(BaseModel):
    repo: str
    branch: str = "main"
    token: Optional[str] = None      # blank/None keeps the stored token


class RunIn(BaseModel):
    force: bool = False


@router.get("/config")
def get_config():
    from core.runtime import updater
    return updater.get_config()


@router.post("/config")
def save_config(body: ConfigIn):
    from core.runtime import updater
    try:
        updater.save_config(body.repo, body.branch, body.token)
    except UpdateError as e:
        raise HTTPException(422, str(e))
    return updater.get_config()


@router.get("/check")
async def check():
    from core.runtime import updater
    try:
        return await asyncio.to_thread(updater.check)
    except UpdateError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.exception("update check failed")
        raise HTTPException(502, f"GitHub સાથે જોડાઈ શકાયું નહીં: {e}")


@router.post("/run")
async def run_update(body: RunIn):
    from core.runtime import updater
    if updater.running:
        raise HTTPException(409, "Update પહેલેથી ચાલુ છે")
    result = await asyncio.to_thread(updater.run, body.force)
    return result


@router.get("/status")
def status():
    from core.runtime import updater
    return {"running": updater.running, "log": updater.log}
