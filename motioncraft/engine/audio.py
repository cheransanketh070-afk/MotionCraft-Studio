"""Procedural music & sound-design synthesiser (numpy only).

Builds a unique soundtrack per plan: chord pad, bass, kick/hat groove, plucked arpeggio,
risers and impact hits on every shot transition. 100% generated locally — no samples.
"""
from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

SR = 44100
SCALES = {"major": [0, 2, 4, 5, 7, 9, 11], "minor": [0, 2, 3, 5, 7, 8, 10]}
PROG = [0, 5, 2, 6]      # i - VI - III - VII  (scale degrees)


def _f(m: float) -> float:
    return 440.0 * 2 ** ((m - 69) / 12)


def _add(buf: np.ndarray, start: float, sig: np.ndarray, gain=1.0, pan=0.0):
    i = int(start * SR)
    if i >= buf.shape[1] or i < 0:
        return
    n = min(len(sig), buf.shape[1] - i)
    buf[0, i:i + n] += sig[:n] * gain * (1 - max(0, pan))
    buf[1, i:i + n] += sig[:n] * gain * (1 + min(0, pan))


def _tone(freq, dur, harm=(1, .4, .2), detune=0.0):
    t = np.arange(int(dur * SR)) / SR
    s = sum(a * np.sin(2 * np.pi * freq * (1 + detune) * (k + 1) * t) for k, a in enumerate(harm))
    return s / sum(harm)


def synth(duration: float, bpm: int, root: int, scale: str, wave_kind: str, seed: int,
          hits: list[float]) -> np.ndarray:
    rng = np.random.default_rng(seed)
    n = int(duration * SR)
    buf = np.zeros((2, n), np.float64)
    sc = SCALES[scale]
    beat = 60.0 / bpm
    bar = beat * 4
    synthy = wave_kind == "synth"

    def note(deg, octave=0):
        o, d = divmod(deg, 7)
        return root + sc[d] + 12 * (o + octave)

    # pad ---------------------------------------------------------------
    t = 0.0
    bi = 0
    while t < duration:
        deg = PROG[bi % 4]
        for j, off in enumerate((0, 2, 4)):
            m = note(deg + off, 1)
            seg = min(bar + 0.6, duration - t + 0.3)
            for det, pan in ((-0.003, -0.5), (0.003, 0.5)):
                tone = _tone(_f(m), seg, (1, .5, .3, .15) if synthy else (1, .25, .08), det)
                e = np.minimum(1, np.arange(len(tone)) / (0.35 * SR)) * np.minimum(1, (len(tone) - np.arange(len(tone))) / (0.5 * SR))
                _add(buf, t, tone * e, 0.085, pan)
        t += bar
        bi += 1

    # bass + kick + hat + arp -------------------------------------------
    nb = int(duration / beat) + 1
    for b in range(nb):
        tb = b * beat
        deg = PROG[(b // 4) % 4]
        fade_in = min(1.0, tb / 1.0)
        # kick
        kt = np.arange(int(0.28 * SR)) / SR
        kick = np.sin(2 * np.pi * (45 * kt + (110 / 28) * (1 - np.exp(-28 * kt))))
        _add(buf, tb, kick * np.exp(-kt * 11), (0.55 if synthy else 0.4) * fade_in)
        # bass
        bt = np.arange(int(beat * 0.95 * SR)) / SR
        bass = np.sin(2 * np.pi * _f(note(deg, -1)) * bt) * np.exp(-bt * 3.2)
        if b % 2 == 0 or synthy:
            _add(buf, tb, bass, 0.34 * fade_in)
        # off-beat hat
        if tb > bar:
            ht = np.arange(int(0.07 * SR)) / SR
            noise = rng.standard_normal(len(ht))
            hat = np.diff(noise, prepend=0) * np.exp(-ht * 70)
            _add(buf, tb + beat / 2, hat, 0.10, 0.25)
        # arpeggio pluck (8ths)
        for k in range(2):
            ta = tb + k * beat / 2
            if ta < bar * 0.5:
                continue
            at = np.arange(int(0.35 * SR)) / SR
            d = [0, 2, 4, 7, 4, 2][(b * 2 + k) % 6]
            pl = np.sin(2 * np.pi * _f(note(deg + d, 2)) * at) + 0.3 * np.sin(4 * np.pi * _f(note(deg + d, 2)) * at)
            _add(buf, ta, pl * np.exp(-at * 9), 0.075, -0.3 if k else 0.3)

    # transition risers + impacts ----------------------------------------
    for h in hits:
        rl = 0.9
        rt = np.arange(int(rl * SR)) / SR
        noise = rng.standard_normal(len(rt))
        rise = np.diff(noise, prepend=0) * (rt / rl) ** 2.5
        _add(buf, max(0, h - rl + 0.1), rise, 0.12)
        it = np.arange(int(1.1 * SR)) / SR
        imp = np.sin(2 * np.pi * 38 * it) * np.exp(-it * 3.5) + 0.25 * rng.standard_normal(len(it)) * np.exp(-it * 14)
        _add(buf, max(0, h - 0.05), imp, 0.55)
    # final hit
    et = np.arange(int(1.2 * SR)) / SR
    _add(buf, max(0, duration - 1.25), np.sin(2 * np.pi * _f(note(0, 0)) * et) * np.exp(-et * 3), 0.2)

    # cheap stereo reverb ------------------------------------------------
    wet = np.zeros_like(buf)
    for d, g in ((0.029, .32), (0.047, .26), (0.071, .20), (0.113, .14)):
        k = int(d * SR)
        wet[0, k:] += buf[1, :-k] * g
        wet[1, k:] += buf[0, :-k] * g
    buf += wet * 0.55

    # master: fades, soft clip, normalise ----------------------------------
    env = np.minimum(1, np.arange(n) / (0.15 * SR)) * np.minimum(1, (n - np.arange(n)) / (0.9 * SR))
    buf *= env
    peak = np.abs(buf).max() or 1.0
    buf = np.tanh(buf / peak * 1.6) / np.tanh(1.6) * 0.89
    return (buf * 32767).astype(np.int16)


def write_wav(path: Path, pcm: np.ndarray) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(pcm.T.copy().tobytes())
