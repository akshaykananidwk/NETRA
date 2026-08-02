"""Phase 4 — Meeting mode.

During a meeting:
- continuous 16 kHz WAV recording → data/meetings/{id}/audio.wav
- rolling transcription (the existing VAD→Whisper pipeline keeps running);
  every segment is stored with a timestamp and a speaker label
- speaker labels via ECAPA: matched voice prints get the person's name,
  unknown voices are clustered online into SPK_1, SPK_2, …
- participants recorded from faces seen and voices matched
- the agent stays SILENT (no greetings) unless the wake word is used

On end (command, or auto after N minutes of no faces + no speech):
- full transcript → LLM → JSON {summary, key_points, decisions, tasks,
  followups, sentiment}
- extracted tasks inserted, reminders auto-created (due −1 day, 10:00)
- WhatsApp summary to the admin, spoken confirmation
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timedelta
from typing import List, Optional

import numpy as np

from config import settings
from core.db import (Meeting, Person, SessionLocal, Task, TranscriptSegment,
                     MeetingParticipant, now_local)

log = logging.getLogger("krishna.meeting")

SUMMARY_PROMPT = """આ ગુજરાતી મીટિંગ ટ્રાન્સક્રિપ્ટ વાંચીને ફક્ત JSON આપ (બીજું કંઈ નહીં):
{"summary": "ટૂંકો સારાંશ (2-4 વાક્ય, ગુજરાતીમાં)",
 "key_points": ["મુદ્દો", ...],
 "decisions": ["નિર્ણય", ...],
 "tasks": [{"title": "કામ", "assignee": "નામ કે null", "due_at": "YYYY-MM-DDTHH:MM કે null", "priority": "low|medium|high|urgent"}],
 "followups": ["ફોલો-અપ", ...],
 "sentiment": "positive|neutral|negative"}

આજની તારીખ: {today}

ટ્રાન્સક્રિપ્ટ:
{transcript}"""

CLUSTER_THRESHOLD = 0.60


class SpeakerLabeler:
    """Online clustering of unknown voices → SPK_1, SPK_2, …"""

    def __init__(self) -> None:
        self._embs: List[np.ndarray] = []
        self._labels: List[str] = []

    def label(self, emb: Optional[np.ndarray], known_name: Optional[str]) -> str:
        if known_name:
            return known_name
        if emb is None:
            return "SPK_?"
        if self._embs:
            sims = np.stack(self._embs) @ emb
            idx = int(np.argmax(sims))
            if float(sims[idx]) >= CLUSTER_THRESHOLD:
                return self._labels[idx]
        label = f"SPK_{len(set(self._labels)) + 1}"
        self._embs.append(emb)
        self._labels.append(label)
        return label


class MeetingManager:
    def __init__(self, audio_worker=None, brain=None, whatsapp=None,
                 reminders=None, greeter=None, bus=None) -> None:
        self.audio = audio_worker
        self.brain = brain              # reused for LLM access
        self.whatsapp = whatsapp
        self.reminders = reminders
        self.greeter = greeter
        self.bus = bus
        self.orch = None
        self.meeting_id: Optional[int] = None
        self._started_ts = 0.0
        self._last_activity = 0.0
        self._labeler = SpeakerLabeler()
        self._participants: set = set()
        self._watchdog: Optional[asyncio.Task] = None

    @property
    def active(self) -> bool:
        return self.meeting_id is not None

    # ── start ─────────────────────────────────────────────────────────────
    async def start(self, title: Optional[str] = None) -> str:
        if self.active:
            return "મીટિંગ પહેલેથી ચાલુ છે."
        title = title or f"મીટિંગ {now_local().strftime('%d-%m %H:%M')}"

        def _create() -> int:
            with SessionLocal() as s:
                m = Meeting(title=title, started_at=now_local(),
                            status="recording")
                s.add(m)
                s.commit()
                return m.id
        mid = await asyncio.to_thread(_create)

        folder = settings.meetings_dir / str(mid)
        folder.mkdir(parents=True, exist_ok=True)
        audio_ok = False
        if self.audio is not None:
            audio_ok = self.audio.start_meeting_recording(folder / "audio.wav")
            if audio_ok:
                def _save_path():
                    with SessionLocal() as s:
                        m = s.get(Meeting, mid)
                        m.audio_path = f"meetings/{mid}/audio.wav"
                        s.commit()
                await asyncio.to_thread(_save_path)

        self.meeting_id = mid
        self._started_ts = time.time()
        self._last_activity = time.time()
        self._labeler = SpeakerLabeler()
        self._participants = set()
        self._watchdog = asyncio.create_task(self._auto_end_watchdog())

        if self.orch:
            self.orch._log_activity("📋", f"મીટિંગ શરૂ: {title}")
            await self.orch.hub.broadcast("meeting.started",
                                          {"meeting_id": mid, "title": title})
        # DPDP: audible recording announcement
        if self.greeter:
            await self.greeter.say("મીટિંગ રેકોર્ડિંગ ચાલુ છે.")
        return ("મીટિંગ નોંધવાનું ચાલુ કર્યું છે."
                if audio_ok else
                "મીટિંગ નોંધું છું (માઇક રેકોર્ડિંગ ઉપલબ્ધ નથી, ફક્ત ટ્રાન્સક્રિપ્ટ).")

    # ── live events (called by the orchestrator) ──────────────────────────
    async def on_transcript(self, d: dict) -> None:
        if not self.active:
            return
        text = (d.get("text") or "").strip()
        if not text:
            return
        self._last_activity = time.time()
        emb = None
        if d.get("speaker_emb"):
            emb = np.frombuffer(d["speaker_emb"], dtype=np.float32)
        label = self._labeler.label(emb, d.get("speaker_name"))
        end_ms = int((time.time() - self._started_ts) * 1000)
        start_ms = max(0, end_ms - int((d.get("duration_sec") or 0) * 1000))
        mid = self.meeting_id

        def _store() -> Optional[int]:
            with SessionLocal() as s:
                pid = d.get("speaker_person_id")
                if pid is not None and s.get(Person, pid) is None:
                    pid = None
                s.add(TranscriptSegment(
                    meeting_id=mid, speaker_label=label, person_id=pid,
                    start_ms=start_ms, end_ms=end_ms, text=text,
                    confidence=d.get("confidence")))
                s.commit()
                return pid
        pid = await asyncio.to_thread(_store)
        if pid:
            await self._add_participant(pid, "voice")
        if self.orch:
            await self.orch.hub.broadcast("meeting.segment", {
                "meeting_id": mid, "speaker_label": label, "text": text,
                "start_ms": start_ms})

    async def on_face(self, person_id: int) -> None:
        if self.active:
            self._last_activity = time.time()
            await self._add_participant(person_id, "face")

    async def _add_participant(self, person_id: int, detected_by: str) -> None:
        if person_id in self._participants:
            return
        self._participants.add(person_id)
        mid = self.meeting_id

        def _store():
            with SessionLocal() as s:
                if s.get(Person, person_id) is None:
                    return
                s.add(MeetingParticipant(meeting_id=mid, person_id=person_id,
                                         detected_by=detected_by))
                s.commit()
        await asyncio.to_thread(_store)

    # ── auto end ──────────────────────────────────────────────────────────
    async def _auto_end_watchdog(self) -> None:
        try:
            while self.active:
                await asyncio.sleep(30)
                idle = time.time() - self._last_activity
                faces = bool(self.orch and self.orch.present_names())
                if faces:
                    self._last_activity = time.time()
                    continue
                if idle >= settings.meeting_auto_end_min * 60:
                    log.info("meeting auto-end after %.0fs idle", idle)
                    await self.end(auto=True)
                    return
        except asyncio.CancelledError:
            pass

    # ── end + summarise ───────────────────────────────────────────────────
    async def end(self, meeting_id: Optional[int] = None,
                  auto: bool = False) -> str:
        if not self.active:
            return "કોઈ મીટિંગ ચાલુ નથી."
        mid = self.meeting_id
        self.meeting_id = None
        if self._watchdog and not auto:
            self._watchdog.cancel()
        if self.audio is not None:
            self.audio.stop_meeting_recording()

        def _close():
            with SessionLocal() as s:
                m = s.get(Meeting, mid)
                if m:
                    m.ended_at = now_local()
                    m.status = "processing"
                    s.commit()
        await asyncio.to_thread(_close)
        if self.orch:
            await self.orch.hub.broadcast("meeting.ended", {"meeting_id": mid})

        # summarise in the background — never block the reply
        asyncio.create_task(self._summarize_and_finalize(mid))
        return "મીટિંગ પૂરી થઈ. સારાંશ બનાવી રહ્યો છું…"

    async def _summarize_and_finalize(self, mid: int) -> None:
        try:
            result = await self._summarize(mid)
            task_count = await asyncio.to_thread(self._finalize, mid, result)
            if self.greeter:
                if task_count:
                    await self.greeter.say(
                        f"મીટિંગ પૂરી થઈ. {task_count} ટાસ્ક બન્યા છે અને "
                        "રિમાઇન્ડર સેટ કરી દીધા છે.")
                else:
                    await self.greeter.say("મીટિંગનો સારાંશ તૈયાર છે.")
            if self.orch:
                self.orch._log_activity("📋",
                                        f"મીટિંગ સારાંશ તૈયાર ({task_count} ટાસ્ક)")
                await self.orch.hub.broadcast("meeting.summary",
                                              {"meeting_id": mid,
                                               "tasks": task_count})
        except Exception:
            log.exception("meeting summarise failed")
            def _fail():
                with SessionLocal() as s:
                    m = s.get(Meeting, mid)
                    if m:
                        m.status = "failed"
                        s.commit()
            await asyncio.to_thread(_fail)

    def _transcript_text(self, mid: int) -> str:
        with SessionLocal() as s:
            rows = (s.query(TranscriptSegment)
                     .filter(TranscriptSegment.meeting_id == mid)
                     .order_by(TranscriptSegment.start_ms).all())
            return "\n".join(f"[{r.speaker_label}] {r.text}" for r in rows)

    async def _summarize(self, mid: int) -> Optional[dict]:
        transcript = await asyncio.to_thread(self._transcript_text, mid)
        if not transcript.strip():
            return None
        prompt = SUMMARY_PROMPT.replace("{today}", str(now_local())[:16]) \
                               .replace("{transcript}", transcript[:24000])
        raw = await self._llm(prompt)
        if raw is None:
            return None
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        try:
            return json.loads(m.group())
        except ValueError:
            log.warning("summary JSON parse failed: %s", raw[:200])
            return None

    async def _llm(self, prompt: str) -> Optional[str]:
        # anthropic → ollama, mirroring the brain's chain
        if settings.anthropic_api_key and self.brain is not None:
            try:
                client = self.brain._client()
                resp = await client.with_options(timeout=60).messages.create(
                    model=settings.anthropic_model, max_tokens=2048,
                    messages=[{"role": "user", "content": prompt}])
                return " ".join(b.text for b in resp.content
                                if b.type == "text")
            except Exception as e:
                log.warning("anthropic summary failed: %s", str(e)[:150])
        try:
            import requests as _rq

            def _call():
                r = _rq.post(settings.ollama_host.rstrip("/") + "/api/chat",
                             json={"model": settings.ollama_model,
                                   "stream": False,
                                   "messages": [{"role": "user",
                                                 "content": prompt}]},
                             timeout=120)
                r.raise_for_status()
                return r.json()["message"]["content"]
            return await asyncio.to_thread(_call)
        except Exception as e:
            log.warning("ollama summary failed: %s", str(e)[:150])
            return None

    def _finalize(self, mid: int, result: Optional[dict]) -> int:
        """Store summary, create tasks + reminders, WhatsApp the admin."""
        task_count = 0
        summary_text = None
        with SessionLocal() as s:
            m = s.get(Meeting, mid)
            if m is None:
                return 0
            if result:
                m.summary = result.get("summary")
                m.key_points = json.dumps(result.get("key_points") or [],
                                          ensure_ascii=False)
                m.decisions = json.dumps(result.get("decisions") or [],
                                         ensure_ascii=False)
                summary_text = m.summary
            m.status = "done"
            s.commit()

        task_titles = []
        if result:
            from actions.tasks import create_task
            for t in (result.get("tasks") or [])[:15]:
                title = (t.get("title") or "").strip()
                if not title:
                    continue
                due = None
                if t.get("due_at"):
                    try:
                        due = datetime.fromisoformat(
                            str(t["due_at"]).replace("Z", "")).replace(tzinfo=None)
                    except ValueError:
                        due = None
                created = create_task(title=title, assignee=t.get("assignee"),
                                      due_at=due,
                                      priority=t.get("priority", "medium"),
                                      source="meeting", meeting_id=mid)
                task_count += 1
                task_titles.append(title)
                # auto reminder: due −1 day at 10:00
                if due is not None and self.reminders is not None:
                    remind_at = (due - timedelta(days=1)).replace(
                        hour=10, minute=0, second=0)
                    if remind_at > now_local():
                        self.reminders.create(message=title,
                                              remind_at=remind_at,
                                              channel="all",
                                              task_id=created["id"])

        if self.whatsapp is not None and settings.wa_admin_number \
                and summary_text:
            from actions.whatsapp import TEMPLATES
            msg = TEMPLATES["meeting"].format(
                date=now_local().strftime("%d-%m-%Y"),
                summary=summary_text,
                task_list="\n".join(f"• {t}" for t in task_titles) or "—")
            self.whatsapp.send_text(settings.wa_admin_number, msg,
                                    purpose="summary")
        return task_count
