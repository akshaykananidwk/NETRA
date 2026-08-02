"""Rule-based intent matcher — the last-resort fallback when both LLMs are
unreachable, covering the most common office commands so the system never
goes fully dead. Matches Gujarati (and rough transliteration) patterns.
"""
from __future__ import annotations

import re
from datetime import timedelta
from typing import Optional, Tuple

from core.db import now_local

GU_NUMBERS = {"એક": 1, "બે": 2, "ત્રણ": 3, "ચાર": 4, "પાંચ": 5, "છ": 6,
              "સાત": 7, "આઠ": 8, "નવ": 9, "દસ": 10,
              "ek": 1, "be": 2, "tran": 3, "char": 4, "panch": 5}


def _qty(text: str) -> int:
    m = re.search(r"\d+", text)
    if m:
        return max(1, min(int(m.group()), 20))
    for word, n in GU_NUMBERS.items():
        if word in text:
            return n
    return 1


def match_intent(text: str) -> Optional[Tuple[str, dict]]:
    """Returns (tool_name, tool_input) or None."""
    t = text.strip().lower()

    # refreshments — "બે ચા મંગાવ", "કોફી લાવ"
    if re.search(r"(મંગાવ|મગાવ|લાવ|મોકલ|order)", t):
        items = []
        # ચા must stand alone — not inside ચાર/ચાવી/ચાલુ
        if re.search(r"(chai|cha)\b|(?<![઀-૿])ચા(?![઀-૿])",
                     t):
            items.append({"item": "chai", "qty": _qty(t)})
        if re.search(r"(કોફી|coffee)", t):
            items.append({"item": "coffee", "qty": _qty(t)})
        if re.search(r"(પાણી|water)", t):
            items.append({"item": "water", "qty": _qty(t)})
        if items:
            return "order_refreshment", {"items": items}

    # pause / resume
    if re.search(r"(બંધ\s*થઈ\s*જા|બંધ\s*થા|ચૂપ|shut\s*up|stop listening)", t):
        return "set_listening_state", {"state": "paused"}
    if re.search(r"(ચાલુ\s*થા|જાગ|પાછો\s*આવ|start listening)", t):
        return "set_listening_state", {"state": "active"}

    # meetings
    if re.search(r"મીટિંગ.*(ચાલુ|શરૂ)|start.*meeting", t):
        return "start_meeting", {}
    if re.search(r"મીટિંગ.*(પૂરી|બંધ|પતી)|end.*meeting", t):
        return "end_meeting", {}

    # visitor log — "આજે કેટલા લોકો આવ્યા?"
    if re.search(r"(કેટલા|કોણ).*(આવ્ય|મુલાકાત)|મુલાકાત.*(કેટલ|કોણ)|visitor", t):
        return "get_visitor_log", {}

    # collection
    if re.search(r"(કલેક્શન|collection|વકરો)", t):
        return "get_collection_report", {}

    # tasks — "આજના બાકી કામ કયા છે?"
    if re.search(r"(કામ|ટાસ્ક|task).*(બાકી|કયા|શું|લિસ્ટ)|બાકી.*(કામ|ટાસ્ક)", t):
        rng = "today" if "આજ" in t else "all"
        return "list_tasks", {"date_range": rng}

    # system status
    if re.search(r"(સિસ્ટમ|system).*(કેમ|સ્ટેટસ|ચાલ)|બધું\s*બરાબર", t):
        return "system_status", {}

    # simple reminder — "યાદ કરાવજે" without LLM gets a default slot tomorrow 10:00
    if re.search(r"(યાદ\s*કરાવ|રિમાઇન્ડર|remind)", t):
        tomorrow = (now_local() + timedelta(days=1)).replace(
            hour=10, minute=0, second=0)
        msg = re.sub(r"(કૃષ્ણ|krishna)[,\s]*", "", text).strip()
        return "create_reminder", {"message": msg or "રિમાઇન્ડર",
                                   "remind_at": tomorrow.isoformat(),
                                   "channel": "voice"}
    return None
