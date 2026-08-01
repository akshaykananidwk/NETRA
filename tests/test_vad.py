"""VAD segmentation — energy-gate backend (deterministic, no C extension)."""
import numpy as np

from audio.vad import VadSegmenter

SR = 16000
FRAME = SR * 30 // 1000          # 480 samples / 30 ms


def silence_frame():
    return np.zeros(FRAME, dtype=np.int16).tobytes()


def loud_frame(seed=0):
    rng = np.random.default_rng(seed)
    return (rng.normal(0, 4000, FRAME).clip(-32000, 32000)
            .astype(np.int16).tobytes())


def make_segmenter(**kw):
    return VadSegmenter(sample_rate=SR, use_webrtc=False, **kw)


def feed_all(seg, frames):
    out = []
    for f in frames:
        r = seg.feed(f)
        if r is not None:
            out.append(r)
    return out


def test_speech_between_silence_yields_one_segment():
    seg = make_segmenter()
    frames = ([silence_frame()] * 20
              + [loud_frame(i) for i in range(30)]     # 900 ms speech
              + [silence_frame()] * 20)                 # closes segment
    segments = feed_all(seg, frames)
    assert len(segments) == 1
    dur_ms = len(segments[0]) / 2 / SR * 1000
    assert dur_ms >= 900                                # speech + padding


def test_short_blip_is_discarded():
    seg = make_segmenter(min_speech_ms=400)
    frames = ([silence_frame()] * 20
              + [loud_frame(i) for i in range(8)]       # only 240 ms
              + [silence_frame()] * 20)
    assert feed_all(seg, frames) == []


def test_pure_silence_yields_nothing():
    seg = make_segmenter()
    assert feed_all(seg, [silence_frame()] * 100) == []


def test_max_length_force_closes():
    seg = make_segmenter(max_segment_sec=1.0)
    frames = [loud_frame(i) for i in range(80)]         # 2.4 s continuous
    segments = feed_all(seg, frames)
    assert len(segments) >= 1
    assert len(segments[0]) / 2 / SR <= 1.05


def test_reset_clears_partial_speech():
    seg = make_segmenter()
    for i in range(15):                                 # mid-utterance…
        seg.feed(loud_frame(i))
    seg.reset()                                         # ducking kicked in
    assert feed_all(seg, [silence_frame()] * 30) == []
