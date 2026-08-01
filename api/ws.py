"""WebSocket hub — pushes live events to every connected browser."""
from __future__ import annotations

import json
import logging
from typing import Set

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

log = logging.getLogger("krishna.ws")


class WSHub:
    def __init__(self) -> None:
        self.clients: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self.clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self.clients.discard(ws)

    async def broadcast(self, type: str, data: dict) -> None:
        if not self.clients:
            return
        msg = json.dumps({"type": type, "data": data}, ensure_ascii=False, default=str)
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


router = APIRouter()


@router.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    from core.runtime import hub, orchestrator
    # http middleware doesn't cover websockets — enforce login here too
    from config import settings
    if settings.admin_password:
        from main import _auth_token
        if ws.cookies.get("kn_auth") != _auth_token():
            await ws.close(code=4401)
            return
    await hub.connect(ws)
    try:
        # initial snapshot so badges render instantly
        await ws.send_text(json.dumps({
            "type": "hello",
            "data": orchestrator.snapshot(),
        }, ensure_ascii=False, default=str))
        while True:
            await ws.receive_text()   # keepalive pings from the client
    except WebSocketDisconnect:
        pass
    except Exception:
        log.debug("ws closed abnormally", exc_info=True)
    finally:
        hub.disconnect(ws)
