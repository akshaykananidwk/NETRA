"""Extract a person's name from a noisy Gujarati STT reply.

"મારું નામ રમેશ પટેલ છે" → "રમેશ પટેલ".  The full LLM does this in Phase 3;
this rule-based version handles the common reply shapes.
"""
from __future__ import annotations

import re
from typing import Optional

# filler words that surround a name in typical replies
_STOPWORDS = {
    # Gujarati
    "મારું", "મારુ", "મારો", "મરું", "નામ", "છે", "છું", "હું", "મને", "તો",
    "જી", "હા", "ના", "ઓકે", "એ", "ને", "કહો", "કહે", "બોલો", "સાહેબ",
    # Hindi spillover from STT
    "मेरा", "नाम", "है", "मैं", "हूँ", "हू",
    # English spillover
    "my", "name", "is", "i", "am", "the", "a", "it's", "its", "this",
    "myself", "sir", "hello", "hi", "namaste",
}

_HONORIFIC_SUFFIXES = ("ભાઈ", "ભાઇ", "બહેન", "બેન", "જી")


def extract_name(text: str) -> Optional[str]:
    """Best-effort name from a transcript. None when nothing name-like is left."""
    if not text:
        return None
    tokens = re.findall(r"[A-Za-z઀-૿ऀ-ॿ]+", text)
    words = []
    for tok in tokens:
        if tok.lower() in _STOPWORDS or tok in _STOPWORDS:
            continue
        words.append(tok)
    if not words:
        return None
    name = " ".join(words[:3]).strip()
    return name or None


def with_honorific(name: str) -> str:
    """"રમેશ" → "રમેશભાઈ" (leave it alone if an honorific is already there)."""
    first = name.split()[0]
    if first.endswith(_HONORIFIC_SUFFIXES):
        return first
    return first + "ભાઈ"
