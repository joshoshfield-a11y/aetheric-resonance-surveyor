"""Test suite for ars_core.

Runs under pytest (``python -m pytest tests/ -q``) and under plain Python
(``python tests/test_ars_core.py``). No network and no microphone required:
every channel is exercised through its offline fallback path.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import numpy as np

from ars_core import CALIBRATION_DISCLAIMER
from ars_core.beacon import (DEFAULT_SR, LATTICE_FREQS, N_NODES,
                             REFERENCE_AMPLITUDES, decode_reply,
                             detect_lattice, encode_reply, goertzel_power,
                             synthesize_lattice)
from ars_core.channels import ChannelReading, journal, noaa_geomag, \
    rng_entropy, usgs_seismic, audio_spectrum
from ars_core.tiers import evaluate_tiers


# --------------------------------------------------------------------------
# beacon synthesis / detection
# --------------------------------------------------------------------------

def _estimated_freq(samples, sr, approx):
    """Peak frequency within +/-1 Hz of ``approx`` at 0.05 Hz resolution."""
    best_f, best_p = approx, -1.0
    f = approx - 1.0
    while f <= approx + 1.0 + 1e-9:
        p = goertzel_power(samples, sr, f)
        if p > best_p:
            best_f, best_p = f, p
        f += 0.05
    return best_f


def test_lattice_synthesis_exactness():
    sig = synthesize_lattice()
    assert sig.dtype == np.float32
    assert sig.shape[0] == 13 * DEFAULT_SR  # duration exactly 13.000 s

    # each node's carrier within 0.5 Hz of spec (Goertzel refinement)
    for k, f0 in enumerate(LATTICE_FREQS):
        seg = sig[k * DEFAULT_SR:(k + 1) * DEFAULT_SR].astype(np.float64)
        est = _estimated_freq(seg, DEFAULT_SR, f0)
        assert abs(est - f0) <= 0.5, f"node {k}: {est} Hz vs {f0} Hz"

    # envelope zeros at integer seconds: local amplitude near zero
    half = int(0.005 * DEFAULT_SR)
    peak = float(np.max(np.abs(sig)))
    for s in range(14):
        c = s * DEFAULT_SR
        lo, hi = max(0, c - half), min(sig.shape[0], c + half)
        assert float(np.max(np.abs(sig[lo:hi]))) < 0.02 * peak + 1e-3, \
            f"envelope not zero at t={s}s"

    # per-node amplitudes track the reference vector
    amps = []
    for k, f0 in enumerate(LATTICE_FREQS):
        seg = sig[k * DEFAULT_SR:(k + 1) * DEFAULT_SR].astype(np.float64)
        t = np.arange(seg.shape[0]) / DEFAULT_SR
        c = abs(np.dot(seg, np.exp(-2j * np.pi * f0 * t))) / (seg.shape[0] / 2.0)
        amps.append(2.0 * c)  # undo sin^2 lobe mean of 1/2
    assert np.corrcoef(amps, REFERENCE_AMPLITUDES)[0, 1] > 0.999


def test_detect_lattice_recovers_synth():
    sig = synthesize_lattice()
    res = detect_lattice(sig, DEFAULT_SR)
    assert res.confidence > 0.9, f"confidence {res.confidence}"
    assert res.amplitude_correlation_r > 0.99
    assert res.envelope_correlation > 0.9
    # negative control: white noise must not false-positive
    noise = np.random.default_rng(1).standard_normal(sig.shape[0])
    rn = detect_lattice(noise.astype(np.float32) * 0.2, DEFAULT_SR)
    assert rn.confidence < 0.6, f"noise confidence {rn.confidence}"


def test_reply_roundtrip_random_trits():
    rng = np.random.default_rng(2024)
    for _ in range(5):
        msg = [int(rng.integers(0, 3)) for _ in range(N_NODES)]
        assert decode_reply(encode_reply(msg), DEFAULT_SR) == msg


# --------------------------------------------------------------------------
# rng channel
# --------------------------------------------------------------------------

def test_rng_chi_square_sane():
    reading = rng_entropy.collect(n_bytes=50_000)
    src = reading.metrics["os.urandom"]
    assert 0.0 <= src["chi2_p"] <= 1.0
    # os.urandom failing chi-square would be extraordinary; require sanity
    assert src["chi2_p"] > 1e-6
    assert 0.0 <= src["serial_p"] <= 1.0
    assert reading.channel == "rng_entropy"


# --------------------------------------------------------------------------
# journal
# --------------------------------------------------------------------------

def test_journal_chain_and_tamper_detection():
    with tempfile.TemporaryDirectory() as td:
        path = os.path.join(td, "journal.jsonl")
        e1 = journal.add_entry("prediction: test event", path=path, tag="pred")
        e2 = journal.add_entry("outcome observed", path=path, tag="outcome")
        assert e2["prev_hash"] == e1["hash"]
        assert journal.verify_chain(path)

        # tamper: rewrite first entry's content but keep hashes
        lines = open(path, encoding="utf-8").read().splitlines()
        import json
        entry = json.loads(lines[0])
        entry["content"] = "retroactively edited prediction"
        lines[0] = json.dumps(entry)
        open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
        assert not journal.verify_chain(path)


# --------------------------------------------------------------------------
# tiers evaluator
# --------------------------------------------------------------------------

def _rng_reading(p):
    return ChannelReading(timestamp_utc="2024-01-01T00:00:00Z",
                          channel="rng_entropy",
                          metrics={"os.urandom": {"chi2_p": p, "serial_p": 0.5}},
                          anomaly_flag=p < 0.001)


def test_tiers_evaluator_logic():
    # clean readings -> no flags, disclaimer present
    v = evaluate_tiers([_rng_reading(0.4)])
    assert not v.tier1_hits and not v.tier2_hits
    assert v.summary.endswith(CALIBRATION_DISCLAIMER)

    # RNG p<0.001 hits tier-1 single-detection AND tier-2 (one channel only)
    v = evaluate_tiers([_rng_reading(1e-5)])
    assert "t1_rng_chi2" in v.tier1_hits
    assert "t2_rng" in v.tier2_hits
    assert not v.tier2_corroborated  # needs >= 2 independent channels

    # second independent channel corroborates tier 2
    audio = ChannelReading(timestamp_utc="2024-01-01T00:00:00Z",
                           channel="audio_spectrum",
                           metrics={"coherent_13hz": {"detected": True}})
    v = evaluate_tiers([_rng_reading(1e-5), audio])
    assert v.tier2_corroborated
    assert set(v.tier2_channels) == {"rng_entropy", "audio_spectrum"}

    # manual attestations: prediction validation is tier 1; somatic is tier 3
    v = evaluate_tiers([], manual_attestations=["prediction_validated",
                                                "somatic_event"])
    assert "t1_prediction_validated" in v.tier1_hits
    assert "t3_somatic" in v.tier3_hits
    assert v.summary.endswith(CALIBRATION_DISCLAIMER)


# --------------------------------------------------------------------------
# offline channel paths (no network)
# --------------------------------------------------------------------------

def test_usgs_offline_fixture(monkeypatch=None):
    # force the network path to fail regardless of connectivity
    usgs_seismic.requests = None
    try:
        r = usgs_seismic.collect()
    finally:
        import importlib
        importlib.reload(usgs_seismic)
    assert r.offline
    assert r.channel == "usgs_seismic"
    assert not r.anomaly_flag  # fixture is calm: z well below 3
    assert "rate_zscore" in r.metrics


def test_noaa_offline_fixture():
    noaa_geomag.requests = None
    try:
        r = noaa_geomag.collect()
    finally:
        import importlib
        importlib.reload(noaa_geomag)
    assert r.offline
    assert r.metrics["kp_latest"] < 5.0
    assert not r.anomaly_flag


def test_audio_spectrum_spike_and_coherent():
    sr = 44100
    # Tier-1: 6 dB spike at a null frequency (280 Hz)
    t = np.arange(2 * sr) / sr
    x = 0.3 * np.sin(2 * np.pi * 280.0 * t) \
        + 0.001 * np.random.default_rng(3).standard_normal(t.shape[0])
    spikes = audio_spectrum.detect_spikes(x, sr)
    assert any(abs(s["freq_hz"] - 280.0) < 1e-6 for s in spikes)

    # Tier-2: coherent 13.00 Hz tone, 12 s, drift << 0.1 Hz
    t = np.arange(12 * sr) / sr
    x = 0.3 * np.sin(2 * np.pi * 13.00 * t) \
        + 0.01 * np.random.default_rng(4).standard_normal(t.shape[0])
    coh = audio_spectrum.coherent_tone_13hz(x, sr)
    assert coh["detected"], coh
    assert coh["drift_hz"] < 0.1


def test_mic_capture_informative_error():
    import importlib.util
    if importlib.util.find_spec("sounddevice") is not None:
        return  # hardware stack present: nothing to assert in CI
    try:
        audio_spectrum.capture_mic(seconds=0.1)
    except RuntimeError as exc:
        assert "sounddevice" in str(exc)
    else:
        raise AssertionError("capture_mic should raise without sounddevice")


# --------------------------------------------------------------------------
# plain-python runner
# --------------------------------------------------------------------------

def _run_all():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS {name}")
        except Exception as exc:
            failed += 1
            print(f"FAIL {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} tests passed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(_run_all())
