"""Audio spectrum analysis channel.

Analyzes a WAV file (or a synthesized test tone) for:

* FFT spectrum and per-frequency power,
* Goertzel power at the 13 lattice carrier frequencies,
* a coherent 13.00 Hz tone detector (Tier-2: drift < 0.1 Hz over > 10 s),
* 40 Hz gamma-band power (for EEG WAV imports),
* spectral spikes > 6 dB above a rolling median at arbitrary "null"
  frequencies (Tier-1 criterion).

Microphone capture is OPTIONAL via the ``sounddevice`` package; when it is
not importable, :func:`capture_mic` raises an informative error. ``collect``
never requires hardware.
"""

from __future__ import annotations

import math
from typing import Optional, Sequence

import numpy as np

from . import ChannelReading, utc_now_iso
from ..beacon import LATTICE_FREQS, goertzel_power, load_wav

CHANNEL = "audio_spectrum"
SPIKE_DB_THRESHOLD = 6.0
GAMMA_FREQ_HZ = 40.0
GAMMA_BAND_HZ = 2.0
COHERENT_FREQ_HZ = 13.00
COHERENT_DRIFT_HZ = 0.1
COHERENT_MIN_SECONDS = 10.0
DEFAULT_NULL_FREQS = (175.0, 280.0, 420.0, 630.0, 840.0)


def synth_test_tone(sr: int = 44100, seconds: float = 2.0,
                    freq: float = 440.0) -> tuple[np.ndarray, int]:
    """Deterministic test tone used when no WAV is supplied."""
    t = np.arange(int(sr * seconds), dtype=np.float64) / sr
    return 0.2 * np.sin(2 * np.pi * freq * t), sr


def fft_spectrum(samples: np.ndarray, sr: int) -> tuple[np.ndarray, np.ndarray]:
    """One-sided amplitude spectrum (freqs, magnitude)."""
    x = np.asarray(samples, dtype=np.float64)
    n = x.shape[0]
    win = np.hanning(n)
    spec = np.abs(np.fft.rfft(x * win)) * 2.0 / max(1, win.sum())
    freqs = np.fft.rfftfreq(n, d=1.0 / sr)
    return freqs, spec


def lattice_goertzel(samples: np.ndarray, sr: int) -> dict[float, float]:
    """Goertzel power at each lattice carrier over the whole recording."""
    x = np.asarray(samples, dtype=np.float64)
    return {float(f): goertzel_power(x, sr, f) for f in LATTICE_FREQS}


def gamma_band_power(samples: np.ndarray, sr: int,
                     center: float = GAMMA_FREQ_HZ,
                     halfwidth: float = GAMMA_BAND_HZ) -> float:
    """Mean spectral power in the gamma band around 40 Hz (EEG WAV imports)."""
    freqs, spec = fft_spectrum(samples, sr)
    mask = (freqs >= center - halfwidth) & (freqs <= center + halfwidth)
    if not np.any(mask):
        return 0.0
    return float(np.mean(spec[mask] ** 2))


def detect_spikes(samples: np.ndarray, sr: int,
                  null_freqs: Sequence[float] = DEFAULT_NULL_FREQS,
                  threshold_db: float = SPIKE_DB_THRESHOLD,
                  median_halfwidth_hz: float = 50.0) -> list[dict[str, float]]:
    """Spikes > ``threshold_db`` above a rolling local median at given freqs.

    The reference level is the median spectral amplitude within +/-50 Hz
    (excluding the bin itself), so a narrow spike stands out against its
    own local floor.
    """
    freqs, spec = fft_spectrum(samples, sr)
    db = 20.0 * np.log10(spec + 1e-12)
    hits = []
    for f0 in null_freqs:
        mask = (freqs >= f0 - median_halfwidth_hz) & \
               (freqs <= f0 + median_halfwidth_hz)
        if not np.any(mask):
            continue
        idx = int(np.argmin(np.abs(freqs - f0)))
        local = db[mask & (np.arange(db.size) != idx)]
        floor = float(np.median(local)) if local.size else float(db[idx])
        excess = float(db[idx] - floor)
        if excess > threshold_db:
            hits.append({"freq_hz": float(f0), "excess_db": round(excess, 2),
                         "floor_db": round(floor, 2)})
    return hits


def coherent_tone_13hz(samples: np.ndarray, sr: int,
                       freq: float = COHERENT_FREQ_HZ,
                       max_drift_hz: float = COHERENT_DRIFT_HZ,
                       min_seconds: float = COHERENT_MIN_SECONDS
                       ) -> dict[str, float | bool]:
    """Tier-2 coherent-tone detector near 13.00 Hz.

    Tracks the instantaneous phase of the analytic narrow-band component by
    correlating 1 s windows against quadrature references; drift is the
    spread of per-window frequency estimates. A hit requires the recording
    to be at least ``min_seconds`` long, drift below ``max_drift_hz``, and
    the tone to carry a visible share of total power.
    """
    x = np.asarray(samples, dtype=np.float64)
    duration = x.shape[0] / float(sr)
    result = {"detected": False, "duration_s": duration,
              "drift_hz": float("nan"), "tone_power_fraction": 0.0}
    if duration < min_seconds:
        result["note"] = "recording shorter than minimum coherent window"
        return result

    win = int(sr)  # 1 s windows; per-window absolute phase -> drift estimate
    nwins = x.shape[0] // win
    est_freqs, powers = [], []
    prev_phase = None
    for i in range(nwins):
        seg = x[i * win:(i + 1) * win]
        t = (np.arange(win, dtype=np.float64) + i * win) / sr
        c = np.dot(seg, np.exp(-2j * np.pi * freq * t)) / (win / 2.0)
        powers.append(abs(c) ** 2)
        phase = float(np.angle(c))
        if prev_phase is not None:
            dphi = (phase - prev_phase + np.pi) % (2 * np.pi) - np.pi
            est_freqs.append(freq + dphi / (2 * np.pi * 1.0))  # 1 s spacing
        prev_phase = phase
    drift = float(np.std(est_freqs)) if est_freqs else float("nan")
    total = float(np.mean(x ** 2)) + 1e-15
    frac = float(np.mean(powers) / 2.0 / total)
    detected = (not math.isnan(drift)) and drift < max_drift_hz and frac > 0.1
    result.update({"detected": bool(detected), "drift_hz": drift,
                   "tone_power_fraction": frac})
    return result


def capture_mic(seconds: float = 5.0, sr: int = 44100) -> tuple[np.ndarray, int]:
    """Record from the default microphone (requires ``sounddevice``).

    Raises
    ------
    RuntimeError
        If ``sounddevice`` is not installed or capture fails.
    """
    try:
        import sounddevice as sd  # type: ignore
    except Exception as exc:
        raise RuntimeError(
            "microphone capture requires the optional 'sounddevice' package; "
            "install it and ensure an input device is available"
        ) from exc
    try:
        rec = sd.rec(int(seconds * sr), samplerate=sr, channels=1,
                     dtype="float64", blocking=True)
    except Exception as exc:
        raise RuntimeError(f"microphone capture failed: {exc}") from exc
    return np.asarray(rec).reshape(-1), sr


def collect(wav_path: Optional[str] = None,
            null_freqs: Sequence[float] = DEFAULT_NULL_FREQS) -> ChannelReading:
    """Analyze a WAV file (or synthesized tone when omitted). Offline-safe."""
    source = "synthetic_test_tone"
    if wav_path:
        samples, sr = load_wav(wav_path)
        source = wav_path
    else:
        samples, sr = synth_test_tone()

    lattice = lattice_goertzel(samples, sr)
    gamma = gamma_band_power(samples, sr)
    spikes = detect_spikes(samples, sr, null_freqs)
    coherent = coherent_tone_13hz(samples, sr)

    flag = bool(spikes) or bool(coherent.get("detected"))
    notes_parts = []
    if spikes:
        notes_parts.append(
            "spikes>6dB at " + ", ".join(f"{s['freq_hz']:.0f} Hz" for s in spikes))
    if coherent.get("detected"):
        notes_parts.append("coherent 13.00 Hz tone within drift tolerance")
    return ChannelReading(
        timestamp_utc=utc_now_iso(),
        channel=CHANNEL,
        metrics={
            "source": source,
            "sample_rate": sr,
            "duration_s": round(samples.shape[0] / float(sr), 3),
            "lattice_goertzel_power": {str(k): v for k, v in lattice.items()},
            "gamma_40hz_power": gamma,
            "spikes": spikes,
            "coherent_13hz": coherent,
        },
        anomaly_flag=flag,
        notes="; ".join(notes_parts) if notes_parts else "no spectral flags",
        offline=False,
    )
