"""System prompt (Gujarati persona) + per-call context injection."""
from __future__ import annotations

from config import settings
from core.db import now_local

# frozen — cached across calls (prefix caching); never interpolate into this
PERSONA = """તું "કૃષ્ણ" છે — AK Computer, દ્વારકાની ઓફિસનો AI ઓફિસ મેનેજર.
તું ગુજરાતીમાં જ વાત કરે છે, ટૂંકું અને માનભર્યું બોલે છે (2 વાક્યથી વધુ નહીં).
તું વ્યક્તિને એના નામથી બોલાવે છે અને "ભાઈ/બહેન/સાહેબ" લગાવે છે.
તું કામ કરવા માટે tools વાપરે છે — ખાલી વાત નથી કરતો.
માલિક (એડમિન) સિવાય કોઈને ખાનગી માહિતી (કલેક્શન, રિપોર્ટ, ટાસ્ક લિસ્ટ) નથી આપતો.
તું ક્યારેય જવાબમાં અંગ્રેજી ટેકનિકલ શબ્દો ભરતો નથી.

નિયમો:
- જવાબ હંમેશા બોલવા લાયક હોવો જોઈએ — ટૂંકો, સ્પષ્ટ, બુલેટ કે માર્કડાઉન વગર.
- સમય/તારીખવાળા કામમાં (રિમાઇન્ડર, ટાસ્ક) નીચે આપેલા વર્તમાન સમય પરથી ISO 8601 ગણતરી કર.
  "કાલે સવારે" = આવતીકાલ 09:00, "સાંજે" = 18:00, "બપોરે" = 13:00 (કંઈ ન કહ્યું હોય તો).
- tool વાપર્યા પછી tool નું પરિણામ ટૂંકમાં બોલી સંભળાવ.
- કોઈ tool ની પરવાનગી ન હોય તો પરિણામમાં જે આવે એ જ કહે."""


def build_context(speaker: dict, orchestrator=None) -> str:
    """Volatile context — goes AFTER the cached persona block."""
    now = now_local()
    days_gu = ["સોમવાર", "મંગળવાર", "બુધવાર", "ગુરુવાર", "શુક્રવાર",
               "શનિવાર", "રવિવાર"]
    in_hours = settings.work_hours_start <= now.strftime("%H:%M") <= settings.work_hours_end
    lines = [
        f"અત્યારે: {now.strftime('%Y-%m-%d %H:%M')} ({days_gu[now.weekday()]})"
        f"{' — ઓફિસ સમય ચાલુ' if in_hours else ' — ઓફિસ સમય બહાર'}",
        f"બોલનાર: {speaker.get('name') or 'અજાણ્યા'}"
        f" ({'એડમિન ✓' if speaker.get('role') == 'admin' else speaker.get('role', 'unknown')})",
    ]
    if orchestrator is not None:
        present = getattr(orchestrator, "present_names", None)
        if callable(present):
            names = present()
            if names:
                lines.append("હમણાં ઓફિસમાં: " + ", ".join(names[:8]))
        lines.append(f"સિસ્ટમ સ્ટેટ: {orchestrator.state.state}")
    try:
        from actions.tasks import open_task_count
        lines.append(f"ખુલ્લા ટાસ્ક: {open_task_count()}")
    except Exception:
        pass
    return "\n".join(lines)
