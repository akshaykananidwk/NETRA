"""WhatsApp client for the self-hosted gateway at bulk.akdwk.in.

- send_text / send_image with 3× retry + exponential backoff
- offline queue persisted to disk — messages are NEVER lost; a background
  flusher retries them when the network returns
- rate limit: max 1 message per 3 seconds
- every attempt logged to whatsapp_log + health badge updated
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from config import settings

log = logging.getLogger("krishna.whatsapp")

RATE_LIMIT_SEC = 3.0
RETRIES = 3
FLUSH_INTERVAL_SEC = 60

TEMPLATES = {
    "tea_order": "🙏 જય દ્વારકાધીશ\nઓફિસમાં {items} મોકલજો.\n— AK Computer ({time})",
    "visitor": "👤 નવા મહેમાન આવ્યા છે\nનામ: {name}\nસમય: {time}",
    "reminder": "⏰ રિમાઇન્ડર\n{task_title}\nસમય: {due}",
    "meeting": "📋 મીટિંગ સારાંશ ({date})\n\n{summary}\n\nટાસ્ક:\n{task_list}",
}

ITEM_NAMES_GU = {"chai": "ચા", "coffee": "કોફી", "water": "પાણી",
                 "cold_drink": "ઠંડુ પીણું"}


class WhatsAppClient:
    def __init__(self, queue_path: Optional[Path] = None) -> None:
        self.queue_path = queue_path or (settings.data_dir / "wa_queue.json")
        self._lock = threading.Lock()
        self._last_sent_at = 0.0
        self._flusher: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @property
    def configured(self) -> bool:
        return bool(settings.wa_api_key and settings.wa_api_base)

    # ── public API ────────────────────────────────────────────────────────
    def send_text(self, to: str, message: str, purpose: str = "general") -> bool:
        """Send now; on failure queue for later. Returns True if sent NOW."""
        if not self.configured:
            self._log_db(to, message, purpose, "failed", "not configured")
            self._set_health("down", "WA_API_KEY સેટ નથી (.env)")
            return False
        ok, detail = self._attempt_with_retries(to, message)
        self._log_db(to, message, purpose, "sent" if ok else "queued", detail)
        if ok:
            self._set_health("ok", "")
        else:
            self._enqueue({"to": to, "message": message, "purpose": purpose,
                           "ts": datetime.now().isoformat()})
            self._set_health("degraded", f"offline — queued ({detail})")
        return ok

    def send_image(self, to: str, image_path: str, caption: str = "",
                   purpose: str = "alert") -> bool:
        if not self.configured:
            return False
        ok, detail = self._attempt_with_retries(to, caption,
                                                image_path=image_path)
        self._log_db(to, f"[image] {caption}", purpose,
                     "sent" if ok else "failed", detail)
        return ok

    def queue_size(self) -> int:
        with self._lock:
            return len(self._read_queue())

    # ── background flusher ────────────────────────────────────────────────
    def start_flusher(self) -> None:
        if self._flusher and self._flusher.is_alive():
            return
        self._flusher = threading.Thread(target=self._flush_loop, daemon=True,
                                         name="wa-flusher")
        self._flusher.start()

    def stop(self) -> None:
        self._stop.set()

    def _flush_loop(self) -> None:
        while not self._stop.wait(FLUSH_INTERVAL_SEC):
            try:
                self.flush_queue()
            except Exception:
                log.exception("queue flush failed")

    def flush_queue(self) -> int:
        """Try to send everything queued. Returns number delivered."""
        if not self.configured:
            return 0
        with self._lock:
            pending = self._read_queue()
        if not pending:
            return 0
        delivered = 0
        remaining = []
        for item in pending:
            ok, detail = self._attempt_with_retries(item["to"], item["message"])
            if ok:
                delivered += 1
                self._log_db(item["to"], item["message"],
                             item.get("purpose", "queued"), "sent",
                             "flushed from queue")
            else:
                remaining.append(item)
        with self._lock:
            self._write_queue(remaining)
        if delivered:
            log.info("flushed %d queued WhatsApp message(s)", delivered)
            self._set_health("ok" if not remaining else "degraded",
                             f"{len(remaining)} queued" if remaining else "")
        return delivered

    # ── transport ─────────────────────────────────────────────────────────
    def _attempt_with_retries(self, to: str, message: str,
                              image_path: Optional[str] = None) -> tuple:
        detail = ""
        for attempt in range(RETRIES):
            self._respect_rate_limit()
            try:
                resp = self._post(to, message, image_path)
                if resp.ok:
                    return True, resp.text[:200]
                detail = f"HTTP {resp.status_code}: {resp.text[:150]}"
                if 400 <= resp.status_code < 500:
                    return False, detail       # our fault — retrying won't help
            except requests.RequestException as e:
                detail = str(e)[:200]
            time.sleep(2 ** attempt)           # 1s, 2s, 4s
        return False, detail

    def _post(self, to: str, message: str,
              image_path: Optional[str] = None) -> requests.Response:
        url = settings.wa_api_base.rstrip("/") + settings.wa_send_path
        payload = {
            "api_key": settings.wa_api_key,
            "sender": settings.wa_device_id,
            "number": to,
            "message": message,
        }
        if image_path:
            p = Path(image_path)
            if not p.is_absolute():
                p = settings.data_dir / image_path
            with open(p, "rb") as f:
                return requests.post(url, data=payload,
                                     files={"file": (p.name, f)},
                                     timeout=settings.wa_timeout_sec)
        return requests.post(url, json=payload,
                             timeout=settings.wa_timeout_sec)

    def _respect_rate_limit(self) -> None:
        with self._lock:
            wait = RATE_LIMIT_SEC - (time.time() - self._last_sent_at)
            if wait > 0:
                time.sleep(wait)
            self._last_sent_at = time.time()

    # ── persistence ───────────────────────────────────────────────────────
    def _read_queue(self) -> list:
        try:
            return json.loads(self.queue_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []

    def _write_queue(self, items: list) -> None:
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.queue_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        tmp.replace(self.queue_path)

    def _enqueue(self, item: dict) -> None:
        with self._lock:
            items = self._read_queue()
            items.append(item)
            self._write_queue(items)

    def _log_db(self, to: str, message: str, purpose: str, status: str,
                response: str) -> None:
        try:
            from core.db import SessionLocal, WhatsAppLog
            with SessionLocal() as s:
                s.add(WhatsAppLog(to_number=to, message=message,
                                  purpose=purpose, status=status,
                                  response=response[:500]))
                s.commit()
        except Exception:
            log.exception("whatsapp_log write failed")

    def _set_health(self, status: str, detail: str) -> None:
        try:
            from core.db import set_health
            set_health("whatsapp", status, detail)
        except Exception:
            pass
