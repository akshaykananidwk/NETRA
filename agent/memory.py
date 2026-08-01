"""Short-term conversation memory — last N turns per speaker."""
from __future__ import annotations

import threading
from collections import deque
from typing import Dict, List

MAX_TURNS = 6


class ConversationMemory:
    def __init__(self, max_turns: int = MAX_TURNS) -> None:
        self._lock = threading.Lock()
        self._turns: Dict[str, deque] = {}
        self.max_turns = max_turns

    @staticmethod
    def key_for(speaker: dict, channel: str) -> str:
        pid = speaker.get("person_id")
        return f"p{pid}" if pid else f"ch:{channel}"

    def add(self, key: str, user_text: str, agent_text: str) -> None:
        with self._lock:
            dq = self._turns.setdefault(key, deque(maxlen=self.max_turns))
            dq.append((user_text, agent_text))

    def messages(self, key: str) -> List[dict]:
        """Prior turns as alternating user/assistant messages."""
        with self._lock:
            dq = list(self._turns.get(key, ()))
        out: List[dict] = []
        for user_text, agent_text in dq:
            out.append({"role": "user", "content": user_text})
            out.append({"role": "assistant", "content": agent_text})
        return out

    def clear(self, key: str) -> None:
        with self._lock:
            self._turns.pop(key, None)
