"""Phone voice upload — audio decode + LAN URL."""
import io
import struct
import wave

import numpy as np


def make_wav(rate=44100, seconds=1.0, stereo=False):
    buf = io.BytesIO()
    n = int(rate * seconds)
    t = np.arange(n) / rate
    tone = (np.sin(2 * np.pi * 440 * t) * 12000).astype(np.int16)
    if stereo:
        tone = np.column_stack([tone, tone]).ravel()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2 if stereo else 1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(tone.tobytes())
    return buf.getvalue()


def test_decode_wav_to_16k_mono():
    from api.routes_agent import decode_audio_to_pcm16k
    pcm = decode_audio_to_pcm16k(make_wav(rate=44100, seconds=1.0))
    samples = len(pcm) // 2
    assert abs(samples - 16000) < 1600          # ~1 s at 16 kHz
    arr = np.frombuffer(pcm, dtype=np.int16)
    assert arr.std() > 500                       # real signal, not silence


def test_decode_stereo_48k():
    from api.routes_agent import decode_audio_to_pcm16k
    pcm = decode_audio_to_pcm16k(make_wav(rate=48000, seconds=0.5, stereo=True))
    samples = len(pcm) // 2
    assert abs(samples - 8000) < 800


def test_lan_ip_returns_ipv4():
    from config import get_lan_ip
    ip = get_lan_ip()
    parts = ip.split(".")
    assert len(parts) == 4 and all(p.isdigit() for p in parts)
