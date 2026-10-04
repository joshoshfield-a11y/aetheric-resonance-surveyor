"""Public-radio stream channel.

Samples a few seconds of a *public* broadcast radio stream (resolved via
the free, keyless RadioBrowser directory, with a small list of stable
direct stream URLs as fallback), decodes it with ffmpeg, and runs the
same spectral screening as the acoustic channel: spikes > 6 dB above a
rolling local median at the pre-registered null frequencies, plus
spectral flatness / peak-frequency / RMS descriptors.

A spike flag means "a narrowband spectral spike was present in the
captured public broadcast at a watched frequency" -- a screening
statement about the captured audio, nothing more.

Offline behaviour: if no stream can be captured (network down, ffmpeg
missing, all sources dead), the channel marks the reading
``offline=True`` and reports which sources were attempted.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any, Optional

import numpy as np

from . import ChannelReading, utc_now_iso
from .audio_spectrum import DEFAULT_NULL_FREQS, detect_spikes, fft_spectrum

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

CHANNEL = "radio_stream"
CAPTURE_SECONDS = 10.0
SAMPLE_RATE = 44100

# Verified reachable 2026-10-04 (Radio Swiss Jazz, public broadcaster SRG).
# SomaFM is a widely mirrored public stream; kept as fallback.
CANDIDATE_STREAMS: tuple[tuple[str, str], ...] = (
    ("Radio Swiss Jazz", "https://stream.srg-ssr.ch/m/rsj/mp3_128"),
    ("SomaFM Groove Salad", "https://ice1.somafm.com/groovesalad-128-mp3"),
)

RADIOBROWSER_URL = ("https://de1.api.radio-browser.info/json/stations/"
                    "byname/{name}")
RADIOBROWSER_FALLBACK_NAMES = ("radio swiss jazz", "soma fm")

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                        "radio_stream_fixture.json")


def _resolve_radiobrowser(name: str, timeout: float = 10.0) -> Optional[tuple[str, str]]:
    """Resolve a public stream URL via the RadioBrowser directory."""
    if requests is None:
        return None
    try:
        resp = requests.get(
            RADIOBROWSER_URL.format(name=name),
            headers={"User-Agent": "ARS/5.0 (research instrument)"},
            timeout=timeout)
        resp.raise_for_status()
        items = resp.json()
        if items:
            it = items[0]
            url = it.get("url_resolved") or it.get("url")
            if url:
                return (str(it.get("name", name)), str(url))
    except Exception:
        pass
    return None


def _capture(url: str, seconds: float = CAPTURE_SECONDS,
             sr: int = SAMPLE_RATE) -> np.ndarray:
    """Capture ``seconds`` of mono audio from a stream URL via ffmpeg."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError("ffmpeg not found on PATH")
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error",
         "-t", str(seconds), "-i", url,
         "-ac", "1", "-ar", str(sr), "-f", "f32le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=seconds + 25.0, check=False)
    if proc.returncode != 0 or not proc.stdout:
        raise RuntimeError(
            f"ffmpeg capture failed: {(proc.stderr or b'')[:200]!r}")
    x = np.frombuffer(proc.stdout, dtype=np.float32).astype(np.float64)
    if x.size < sr:  # less than one second of audio is not a capture
        raise RuntimeError(f"capture too short: {x.size} samples")
    return x


def _analyze(samples: np.ndarray, sr: int) -> dict[str, Any]:
    rms = float(np.sqrt(np.mean(samples ** 2)))
    freqs, spec = fft_spectrum(samples, sr)
    peak_idx = int(np.argmax(spec[1:])) + 1
    peak_freq = float(freqs[peak_idx])
    # spectral flatness (geometric / arithmetic mean of the power spectrum)
    power = spec ** 2 + 1e-18
    flatness = float(np.exp(np.mean(np.log(power))) / np.mean(power))
    spikes = detect_spikes(samples, sr, DEFAULT_NULL_FREQS)
    return {
        "captured_seconds": round(samples.shape[0] / float(sr), 3),
        "sample_rate": sr,
        "rms": round(rms, 6),
        "peak_freq_hz": round(peak_freq, 2),
        "spectral_flatness": round(flatness, 4),
        "spikes": spikes,
        "null_freqs_hz": list(DEFAULT_NULL_FREQS),
    }


def _load_fixture() -> dict[str, Any]:
    with open(_FIXTURE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def collect(streams: Optional[list[tuple[str, str]]] = None,
            seconds: float = CAPTURE_SECONDS,
            timeout: float = 10.0) -> ChannelReading:
    """Capture one public-radio sample (network with offline fallback)."""
    candidates = list(streams) if streams is not None \
        else list(CANDIDATE_STREAMS)
    attempted: list[str] = []
    errors: list[str] = []
    for name, url in candidates:
        attempted.append(f"{name} <{url}>")
        try:
            samples = _capture(url, seconds=seconds)
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        metrics = _analyze(samples, SAMPLE_RATE)
        metrics["stream_name"] = name
        metrics["stream_url"] = url
        spikes = metrics["spikes"]
        flag = bool(spikes)
        notes = ("spikes>6dB at " + ", ".join(f"{s['freq_hz']:.0f} Hz"
                                             for s in spikes)) if spikes \
            else f"no spectral flags in {seconds:.0f}s capture"
        return ChannelReading(
            timestamp_utc=utc_now_iso(), channel=CHANNEL,
            metrics=metrics, anomaly_flag=flag, notes=notes,
            offline=False)

    # hardcoded list exhausted: try the public RadioBrowser directory once
    for rb_name in RADIOBROWSER_FALLBACK_NAMES:
        resolved = _resolve_radiobrowser(rb_name, timeout=timeout)
        if not resolved:
            continue
        name, url = resolved
        attempted.append(f"{name} <{url}> (radiobrowser)")
        try:
            samples = _capture(url, seconds=seconds)
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}: {exc}")
            continue
        metrics = _analyze(samples, SAMPLE_RATE)
        metrics["stream_name"] = name
        metrics["stream_url"] = url
        spikes = metrics["spikes"]
        flag = bool(spikes)
        notes = ("spikes>6dB at " + ", ".join(f"{s['freq_hz']:.0f} Hz"
                                             for s in spikes)) if spikes \
            else f"no spectral flags in {seconds:.0f}s capture"
        return ChannelReading(
            timestamp_utc=utc_now_iso(), channel=CHANNEL,
            metrics=metrics, anomaly_flag=flag, notes=notes,
            offline=False)

    fx = _load_fixture()
    return ChannelReading(
        timestamp_utc=utc_now_iso(), channel=CHANNEL,
        metrics={"attempted": attempted,
                 "errors": [e[:160] for e in errors],
                 "fixture_note": fx.get("note")},
        anomaly_flag=False,
        notes="offline: no public stream captured -- "
              + ("; ".join(errors[:2]) if errors else "no sources attempted"),
        offline=True,
    )
