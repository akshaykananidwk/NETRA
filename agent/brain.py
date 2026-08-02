"""The agent brain — LLM with tool use, layered fallbacks.

Chain: Anthropic (cloud) → Ollama (local) → rule-based intent matcher.
The office never goes fully dead: even with no internet and no local LLM,
the eight most common commands still work through the rules.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

import requests as _requests

from config import settings
from core.db import Conversation, SessionLocal, set_health
from agent.memory import ConversationMemory
from agent.prompts import PERSONA, build_context
from agent.rules import match_intent
from agent.tools import TOOLS, ToolExecutor

log = logging.getLogger("krishna.brain")

MAX_TOOL_ROUNDS = 5
FALLBACK_REPLY = ("માફ કરશો, અત્યારે હું બરાબર વિચારી શકતો નથી. "
                  "થોડી વારમાં ફરી પ્રયત્ન કરો.")


class AgentBrain:
    def __init__(self, executor: ToolExecutor,
                 memory: Optional[ConversationMemory] = None,
                 orchestrator=None) -> None:
        self.executor = executor
        self.memory = memory or ConversationMemory()
        self.orchestrator = orchestrator
        self._anthropic = None

    # ── public ────────────────────────────────────────────────────────────
    async def process(self, text: str, speaker: dict,
                      channel: str = "voice") -> dict:
        """Returns {reply, tool_calls, provider, latency_ms}."""
        t0 = time.time()
        text = text.strip()
        reply, tool_calls, provider = None, [], None

        providers = (["anthropic", "ollama"]
                     if settings.llm_provider == "anthropic"
                     else ["ollama", "anthropic"])
        for prov in providers:
            try:
                if prov == "anthropic":
                    if not settings.anthropic_api_key:
                        continue
                    reply, tool_calls = await self._run_anthropic(text, speaker)
                else:
                    reply, tool_calls = await self._run_ollama(text, speaker)
                provider = prov
                set_health("llm", "ok", prov)
                break
            except Exception as e:
                log.warning("%s failed: %s", prov, str(e)[:200])
                set_health("llm", "degraded", f"{prov}: {str(e)[:150]}")

        if reply is None:
            reply, tool_calls = self._run_rules(text, speaker)
            provider = "rules"

        latency_ms = int((time.time() - t0) * 1000)
        key = self.memory.key_for(speaker, channel)
        self.memory.add(key, text, reply)
        await asyncio.to_thread(self._log_conversation, speaker, channel,
                                text, reply, tool_calls, latency_ms)
        log.info("[%s %dms] %s -> %s", provider, latency_ms, text[:80],
                 reply[:80])
        return {"reply": reply, "tool_calls": tool_calls,
                "provider": provider, "latency_ms": latency_ms}

    # ── anthropic ─────────────────────────────────────────────────────────
    def _client(self):
        if self._anthropic is None:
            from anthropic import AsyncAnthropic
            self._anthropic = AsyncAnthropic(
                api_key=settings.anthropic_api_key,
                timeout=settings.llm_timeout_sec, max_retries=1)
        return self._anthropic

    async def _run_anthropic(self, text: str, speaker: dict) -> tuple:
        client = self._client()
        key = self.memory.key_for(speaker, "voice")
        system = [
            # frozen persona first — prompt-cached across calls
            {"type": "text", "text": PERSONA,
             "cache_control": {"type": "ephemeral"}},
            # volatile context after the cache breakpoint
            {"type": "text", "text": build_context(speaker, self.orchestrator)},
        ]
        messages = self.memory.messages(key) + [{"role": "user", "content": text}]
        tool_calls = []

        for _round in range(MAX_TOOL_ROUNDS):
            response = await client.messages.create(
                model=settings.anthropic_model,
                max_tokens=1024,
                system=system,
                tools=TOOLS,
                messages=messages,
            )
            if response.stop_reason != "tool_use":
                reply = " ".join(b.text for b in response.content
                                 if b.type == "text").strip()
                return (reply or FALLBACK_REPLY), tool_calls

            messages.append({"role": "assistant", "content": response.content})
            results = []
            for block in response.content:
                if block.type != "tool_use":
                    continue
                result = await asyncio.to_thread(
                    self.executor.execute, block.name, dict(block.input),
                    speaker)
                tool_calls.append({"name": block.name,
                                   "input": dict(block.input),
                                   "result": result})
                results.append({"type": "tool_result",
                                "tool_use_id": block.id, "content": result})
            messages.append({"role": "user", "content": results})

        # ran out of rounds — speak the last tool result rather than nothing
        if tool_calls:
            return tool_calls[-1]["result"], tool_calls
        return FALLBACK_REPLY, tool_calls

    # ── ollama (local, with function calling — qwen2.5 etc.) ─────────────
    @staticmethod
    def _ollama_tools() -> list:
        return [{"type": "function",
                 "function": {"name": t["name"],
                              "description": t["description"],
                              "parameters": t["input_schema"]}}
                for t in TOOLS]

    async def _run_ollama(self, text: str, speaker: dict) -> tuple:
        # rules first: instant + reliable for the common commands, and it
        # spares the local model a round trip
        matched = match_intent(text)
        if matched:
            name, tool_input = matched
            result = await asyncio.to_thread(self.executor.execute, name,
                                             tool_input, speaker)
            return result, [{"name": name, "input": tool_input,
                             "result": result}]

        key = self.memory.key_for(speaker, "voice")
        messages = [{"role": "system",
                     "content": PERSONA + "\n\n"
                     + build_context(speaker, self.orchestrator)}]
        messages += self.memory.messages(key)
        messages.append({"role": "user", "content": text})
        tool_calls = []

        def _chat(msgs, with_tools):
            payload = {"model": settings.ollama_model, "stream": False,
                       "messages": msgs}
            if with_tools:
                payload["tools"] = self._ollama_tools()
            resp = _requests.post(
                settings.ollama_host.rstrip("/") + "/api/chat",
                json=payload, timeout=settings.llm_timeout_sec + 25)
            resp.raise_for_status()
            return resp.json()["message"]

        use_tools = True
        for _round in range(MAX_TOOL_ROUNDS):
            try:
                message = await asyncio.to_thread(_chat, messages, use_tools)
            except _requests.HTTPError:
                if not use_tools:
                    raise
                use_tools = False        # model without tool support
                continue
            calls = message.get("tool_calls") or []
            if not calls:
                reply = (message.get("content") or "").strip()
                return (reply or FALLBACK_REPLY), tool_calls
            messages.append(message)
            for call in calls:
                fn = call.get("function", {})
                name = fn.get("name", "")
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                result = await asyncio.to_thread(self.executor.execute,
                                                 name, args, speaker)
                tool_calls.append({"name": name, "input": args,
                                   "result": result})
                messages.append({"role": "tool", "content": result})

        if tool_calls:
            return tool_calls[-1]["result"], tool_calls
        return FALLBACK_REPLY, tool_calls

    # ── rules (final fallback) ────────────────────────────────────────────
    def _run_rules(self, text: str, speaker: dict) -> tuple:
        matched = match_intent(text)
        if matched is None:
            return FALLBACK_REPLY, []
        name, tool_input = matched
        result = self.executor.execute(name, tool_input, speaker)
        return result, [{"name": name, "input": tool_input, "result": result}]

    # ── logging ───────────────────────────────────────────────────────────
    def _log_conversation(self, speaker: dict, channel: str, user_text: str,
                          agent_text: str, tool_calls: list,
                          latency_ms: int) -> None:
        try:
            with SessionLocal() as s:
                person_id = speaker.get("person_id")
                if person_id is not None:
                    from core.db import Person
                    if s.get(Person, person_id) is None:
                        person_id = None
                s.add(Conversation(
                    person_id=person_id, channel=channel,
                    user_text=user_text, agent_text=agent_text,
                    tool_calls=json.dumps(tool_calls, ensure_ascii=False)[:2000]
                    if tool_calls else None,
                    latency_ms=latency_ms))
                s.commit()
        except Exception:
            log.exception("conversation log failed")
