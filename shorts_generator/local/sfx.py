"""Tiny procedural sound effects (numpy only) — copyright-free by construction.

Every sound is synthesized from noise/sine waves at render time, so there are
no third-party samples involved. Levels are deliberately low: they sit under
the streamer's voice, never on top of it.
"""
import wave
from typing import List, Tuple

import numpy as np

SR = 48000


def _bandpass_sweep(n: int, f0: float, f1: float, q: float = 1.2, seed: int = 1) -> np.ndarray:
    """White noise through a 2-pole resonant band-pass whose centre sweeps f0→f1."""
    rng = np.random.default_rng(seed)
    x = rng.standard_normal(n)
    y = np.zeros(n)
    freqs = np.geomspace(f0, f1, n)
    y1 = y2 = 0.0
    for i in range(n):
        w = 2 * np.pi * freqs[i] / SR
        r = np.exp(-w / (2 * q))
        b1, b2 = 2 * r * np.cos(w), -r * r
        v = (1 - r) * x[i] + b1 * y1 + b2 * y2
        y2, y1 = y1, v
        y[i] = v
    return y / (np.abs(y).max() + 1e-9)


def whoosh(dur: float = 0.42) -> np.ndarray:
    n = int(SR * dur)
    t = np.linspace(0, 1, n)
    env = np.sin(np.pi * np.clip(t * 1.15, 0, 1)) ** 2  # swell up then away
    return _bandpass_sweep(n, 350, 3200) * env


def impact(dur: float = 0.55) -> np.ndarray:
    n = int(SR * dur)
    t = np.arange(n) / SR
    freq = 38 + 52 * np.exp(-t * 18)                      # pitch drop 90 Hz → 38 Hz
    body = np.sin(2 * np.pi * np.cumsum(freq) / SR) * np.exp(-t * 7)
    click = np.random.default_rng(7).standard_normal(n) * np.exp(-t * 120) * 0.35
    s = body + click
    return s / (np.abs(s).max() + 1e-9)


def pop(dur: float = 0.09) -> np.ndarray:
    n = int(SR * dur)
    t = np.arange(n) / SR
    freq = 1100 - 4500 * t
    s = np.sin(2 * np.pi * np.cumsum(freq) / SR) * np.exp(-t * 45)
    return s / (np.abs(s).max() + 1e-9)


def scratch(dur: float = 0.32) -> np.ndarray:
    """Record-scratch stop for freeze frames: noise through a band-pass sweeping down then up."""
    n = int(SR * dur)
    half = n // 2
    s = np.r_[_bandpass_sweep(half, 2600, 700, q=3, seed=3), _bandpass_sweep(n - half, 700, 1800, q=3, seed=4)]
    t = np.linspace(0, 1, n)
    return s * np.minimum(1, t * 25) * (1 - t) ** 0.6


def buzz(dur: float = 0.45) -> np.ndarray:
    """'Wrong answer' error buzz: two detuned square waves at ~110 Hz, softened."""
    t = np.arange(int(SR * dur)) / SR
    sq = np.sign(np.sin(2 * np.pi * 110 * t)) + np.sign(np.sin(2 * np.pi * 116.5 * t))
    sq = np.convolve(sq, np.ones(24) / 24, "same")  # take the harsh edge off
    s = sq * np.minimum(1, t * 60) * np.minimum(1, (dur - t) * 20)
    return s / (np.abs(s).max() + 1e-9)


def ding(dur: float = 0.7) -> np.ndarray:
    """Bright success chime: two bell partials with exponential decay."""
    t = np.arange(int(SR * dur)) / SR
    s = (np.sin(2 * np.pi * 1318.5 * t) + 0.6 * np.sin(2 * np.pi * 1975.5 * t)) * np.exp(-t * 6)
    return s / (np.abs(s).max() + 1e-9)


SOUNDS = {"whoosh": (whoosh, 0.20), "impact": (impact, 0.30), "pop": (pop, 0.12),
          "scratch": (scratch, 0.22), "buzz": (buzz, 0.16), "ding": (ding, 0.14)}


def render_track(events: List[Tuple[float, str]], duration: float, path: str) -> str:
    """Write a stereo 48 kHz WAV of `duration` seconds with the events placed on it.

    events: (time_in_seconds, sound_name). Whooshes are centred on their time
    (they lead into a cut); impacts/pops start exactly at it.
    """
    total = int(SR * duration) + 1
    track = np.zeros(total)
    cache = {}
    for at, name in events:
        fn, gain = SOUNDS[name]
        if name not in cache:
            cache[name] = fn()
        s = cache[name] * gain
        start = int(SR * at) - (len(s) // 2 if name == "whoosh" else 0)
        a, b = max(0, start), min(total, start + len(s))
        if b > a:
            track[a:b] += s[a - start:b - start]
    pcm = (np.clip(track, -1, 1) * 32767).astype(np.int16)
    stereo = np.repeat(pcm[:, None], 2, axis=1)
    with wave.open(path, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(stereo.tobytes())
    return path
