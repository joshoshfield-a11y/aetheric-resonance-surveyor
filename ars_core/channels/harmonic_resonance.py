"""Harmonic-resonance channel: the unknown-code listener.

Where the beacon channel listens for one *pre-registered* harmonic code
(the 13-node lattice), this channel listens for *unfamiliar* harmonic
structure: sets of spectral peaks in small-integer ratio (p:q, p,q <= 8)
sharing a common fundamental -- the classic signature of a designed
oscillator, as opposed to broadband noise.

Method (documented honestly):
  1. Capture two ~10 s samples of a public broadcast stream (~25 s apart)
     via the radio_stream capture machinery.
  2. In each capture, find spectral peaks (local maxima > median + 12 dB,
     50-4000 Hz) and test them against a harmonic series on the lowest
     strong peak: a harmonic "hit" is a peak within +/-1% of k*f0, k=2..8.
  3. FLAG only on persistence: the same fundamental (within +/-2%) with
     >= 3 harmonic hits in *both* captures. A passing musical phrase will
     not repeat its exact harmonic skeleton half a minute later; a
     continuous carrier will. (A sustained musical drone still can --
     the flag is a screening trigger, not an identification.)

The flag means "a persistent, unfamiliar harmonic code was present in
two consecutive public-broadcast captures." Nothing more.

Offline behaviour: if no stream can be captured, the channel marks the
reading ``offline=True`` and reports the attempt. No spectrum is ever
fabricated.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Optional

import numpy as np

from . import ChannelReading, utc_now_iso
from .audio_spectrum import fft_spectrum
from .radio_stream import (CANDIDATE_STREAMS, CAPTURE_SECONDS,
                           RADIOBROWSER_FALLBACK_NAMES, SAMPLE_RATE,
                           _capture, _resolve_radiobrowser)

CHANNEL = "harmonic_resonance"
PEAK_FLOOR_DB = 12.0
PEAK_MIN_HZ = 50.0
PEAK_MAX_HZ = 4000.0
PEAK_MIN_SEP_HZ = 15.0
HARMONIC_TOL = 0.01          # +/-1% of k*f0
MAX_HARMONIC = 8
MIN_HARMONICS = 3            # per capture, to count as a code
FUND_MATCH_TOL = 0.02        # +/-2% between the two captures
CAPTURE_GAP_S = 25.0

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                        "harmonic_resonance_fixture.json")


def find_peaks(samples: np.ndarray, sr: int,
               top_n: int = 12) -> list[tuple[float, float]]:
    """Strongest spectral peaks as (freq_hz, dB) sorted by power desc."""
    freqs, spec = fft_spectrum(samples, sr)
    db = 20.0 * np.log10(spec + 1e-12)
    band = (freqs >= PEAK_MIN_HZ) & (freqs <= PEAK_MAX_HZ)
    thr = float(np.median(db[band])) + PEAK_FLOOR_DB
    idx = np.where(band)[0]
    peaks: list[tuple[float, float]] = []
    for i in idx[1:-1]:
        if db[i] > thr and db[i] >= db[i - 1] and db[i] >= db[i + 1]:
            f = float(freqs[i])
            if not peaks or f - peaks[-1][0] > PEAK_MIN_SEP_HZ:
                peaks.append((f, float(db[i])))
            elif float(db[i]) > peaks[-1][1]:
                peaks[-1] = (f, float(db[i]))
    peaks.sort(key=lambda t: -t[1])
    return peaks[:top_n]


def analyze_harmonicity(samples: np.ndarray,
                        sr: int) -> dict[str, Any]:
    """Harmonic-series analysis of one capture.

    Returns the fundamental candidate (lowest strong peak), the harmonic
    hits k*f0 present within tolerance, a harmonicity score, and the
    small-integer ratio pairs among the top peaks.
    """
    peaks = find_peaks(samples, sr)
    if not peaks:
        return {"fundamental_hz": None, "n_peaks": 0, "harmonics": [],
                "n_harmonics": 0,
                "harmonicity_score": 0.0, "ratio_pairs": []}
    f0 = min(p[0] for p in peaks)
    harmonics = []
    for k in range(2, MAX_HARMONIC + 1):
        target = k * f0
        hit = next((p for p in peaks
                    if abs(p[0] - target) / target <= HARMONIC_TOL), None)
        if hit is not None:
            harmonics.append({"k": k, "freq_hz": round(hit[0], 2),
                              "db": round(hit[1], 1)})
    # small-integer ratio pairs among top peaks (both sides <= 8)
    pairs = []
    for i in range(len(peaks)):
        for j in range(i + 1, len(peaks)):
            a, b = max(peaks[i][0], peaks[j][0]), min(peaks[i][0], peaks[j][0])
            for p in range(1, 9):
                for q in range(1, 9):
                    if abs(a / b - p / q) / (p / q) <= HARMONIC_TOL:
                        pairs.append({"ratio": f"{p}:{q}",
                                      "f_hi": round(a, 1), "f_lo": round(b, 1)})
                        break
                else:
                    continue
                break
    score = round(len(harmonics) / (MAX_HARMONIC - 1), 3)
    return {"fundamental_hz": round(f0, 2),
            "n_peaks": len(peaks),
            "harmonics": harmonics,
            "n_harmonics": len(harmonics),
            "harmonicity_score": score,
            "ratio_pairs": pairs[:12]}


def _capture_stream(streams: Optional[list[tuple[str, str]]],
                    seconds: float, timeout: float
                    ) -> tuple[str, str, np.ndarray]:
    """Capture from the first working stream; (name, url, samples)."""
    candidates = list(streams) if streams is not None \
        else list(CANDIDATE_STREAMS)
    errors = []
    for name, url in candidates:
        try:
            return name, url, _capture(url, seconds=seconds)
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}")
    for rb_name in RADIOBROWSER_FALLBACK_NAMES:
        resolved = _resolve_radiobrowser(rb_name, timeout=timeout)
        if not resolved:
            continue
        name, url = resolved
        try:
            return name, url, _capture(url, seconds=seconds)
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}")
    raise RuntimeError("no public stream captured: " + "; ".join(errors[:3]))


def _load_fixture() -> dict[str, Any]:
    with open(_FIXTURE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def collect(streams: Optional[list[tuple[str, str]]] = None,
            seconds: float = CAPTURE_SECONDS,
            timeout: float = 10.0) -> ChannelReading:
    """Two captures, persistence-gated harmonic-code screen."""
    offline = False
    try:
        name1, url1, s1 = _capture_stream(streams, seconds, timeout)
        time.sleep(CAPTURE_GAP_S)
        _name2, _url2, s2 = _capture_stream(streams, seconds, timeout)
    except Exception as exc:
        fx = _load_fixture()
        return ChannelReading(
            timestamp_utc=utc_now_iso(), channel=CHANNEL,
            metrics={"fixture_note": fx.get("note"),
                     "error": f"{type(exc).__name__}: {exc}"[:200]},
            anomaly_flag=False,
            notes=f"offline: {exc}"[:200],
            offline=True)

    a1 = analyze_harmonicity(s1, SAMPLE_RATE)
    a2 = analyze_harmonicity(s2, SAMPLE_RATE)
    f1, f2 = a1["fundamental_hz"], a2["fundamental_hz"]
    persistent = (
        f1 is not None and f2 is not None
        and abs(f1 - f2) / max(f1, f2) <= FUND_MATCH_TOL
        and a1["n_harmonics"] >= MIN_HARMONICS
        and a2["n_harmonics"] >= MIN_HARMONICS
    )
    flag = bool(persistent)
    metrics = {
        "stream_name": name1,
        "stream_url": url1,
        "capture_seconds": seconds,
        "capture_gap_s": CAPTURE_GAP_S,
        "capture_1": a1,
        "capture_2": a2,
        "persistent": persistent,
        "min_harmonics": MIN_HARMONICS,
    }
    if flag:
        notes = (f"persistent harmonic code: f0~{f1:.1f} Hz, "
                 f"{a1['n_harmonics']}/{a2['n_harmonics']} harmonics in "
                 f"two captures {CAPTURE_GAP_S:.0f}s apart")
    else:
        notes = ("no persistent harmonic code "
                 f"(f0 {f1} vs {f2} Hz; "
                 f"{a1['n_harmonics']}/{a2['n_harmonics']} harmonics)")
    return ChannelReading(
        timestamp_utc=utc_now_iso(), channel=CHANNEL,
        metrics=metrics, anomaly_flag=flag, notes=notes,
        offline=offline)
