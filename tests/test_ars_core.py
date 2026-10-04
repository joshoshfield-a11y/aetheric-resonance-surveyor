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
    rng_entropy, usgs_seismic, audio_spectrum, noaa_solar, \
    openmeteo_weather, radio_stream, bitcoin_substrate, \
    harmonic_resonance, geometric_resonance
from ars_core.tiers import evaluate_tiers
from datetime import datetime, timezone


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
# new channels: solar X-ray, weather grid, public radio, bitcoin substrate
# --------------------------------------------------------------------------

def test_noaa_solar_offline_fixture():
    noaa_solar.requests = None
    try:
        r = noaa_solar.collect()
    finally:
        import importlib
        importlib.reload(noaa_solar)
    assert r.offline
    assert r.channel == "noaa_solar"
    assert r.metrics["current_class"].startswith("B")
    assert r.metrics["x_flares_24h"] == 0
    assert not r.anomaly_flag  # recorded fixture is solar-quiet


def test_noaa_solar_class_of_flux():
    assert noaa_solar.class_of_flux(2.4e-4) == "X2.4"
    assert noaa_solar.class_of_flux(5.0e-6) == "C5.0"
    assert noaa_solar.class_of_flux(1.94e-7) == "B1.9"
    assert noaa_solar.class_of_flux(1.2e-8) == "A1.2"


def test_noaa_solar_flag_logic():
    now = datetime(2026, 10, 4, 5, 0, tzinfo=timezone.utc)
    xray = [{"time_tag": "2026-10-04T05:00:00Z", "flux": 1.2e-4,
             "satellite": 18}]

    def flare(mc, mt="2026-10-04T04:00:00Z"):
        return {"max_time": mt, "max_class": mc, "max_xrlong": 1.2e-4}

    out = noaa_solar._analyze([flare("X1.2")], xray, now)
    assert out["anomaly_flag"] and out["metrics"]["x_flares_24h"] == 1
    out = noaa_solar._analyze([flare("M1.0"), flare("M2.1"), flare("M5.0")],
                              xray, now)
    assert out["anomaly_flag"] and out["metrics"]["m_flares_24h"] == 3
    out = noaa_solar._analyze([flare("C2.0"), flare("M1.0")], xray, now)
    assert not out["anomaly_flag"]
    # stale flares (outside the 24h window) must not count
    out = noaa_solar._analyze([flare("X9.9", "2026-09-20T04:00:00Z")],
                              xray, now)
    assert not out["anomaly_flag"]
    assert out["metrics"]["x_flares_24h"] == 0


def test_openmeteo_offline_fixture():
    openmeteo_weather.requests = None
    try:
        r = openmeteo_weather.collect()
    finally:
        import importlib
        importlib.reload(openmeteo_weather)
    assert r.offline
    assert r.channel == "openmeteo_weather"
    assert r.metrics["n_stations"] == 8
    assert "max_abs_z" in r.metrics
    assert all(s["name"] for s in r.metrics["stations"])
    # flag must be consistent with the extreme-station count, whatever the
    # recorded weather was doing (no calm-weather assumption baked in)
    assert r.anomaly_flag == (r.metrics["n_extreme"] >= 2)


def test_openmeteo_zscore_and_flag():
    series = [1013.0 + (i % 5) * 0.2 for i in range(48)]
    calm_z = openmeteo_weather._pressure_z(1013.4, series)
    assert abs(calm_z) < 1.0
    storm_z = openmeteo_weather._pressure_z(1045.0, series)
    assert storm_z > 3.0

    def st(name, p):
        return {"name": name,
                "current": {"pressure_msl": p, "temperature_2m": 20.0,
                            "wind_speed_10m": 10.0},
                "hourly": {"pressure_msl": series}}

    out = openmeteo_weather._analyze([st("a", 1045.0), st("b", 1045.0),
                                      st("c", 1013.4)])
    assert out["anomaly_flag"] and out["metrics"]["n_extreme"] == 2
    out = openmeteo_weather._analyze([st("a", 1013.4), st("b", 1013.4),
                                      st("c", 1013.4)])
    assert not out["anomaly_flag"]


def test_radio_stream_offline_no_fabrication():
    # streams=[] skips candidates; requests=None kills the directory
    # fallback -> must report offline, never invent spectrum
    radio_stream.requests = None
    try:
        r = radio_stream.collect(streams=[])
    finally:
        import importlib
        importlib.reload(radio_stream)
    assert r.offline
    assert r.channel == "radio_stream"
    assert not r.anomaly_flag
    assert "no public stream captured" in r.notes


def test_radio_stream_spike_detection():
    sr = 44100
    t = np.arange(3 * sr) / sr
    x = 0.25 * np.sin(2 * np.pi * 420.0 * t) \
        + 0.001 * np.random.default_rng(11).standard_normal(t.shape[0])
    metrics = radio_stream._analyze(x, sr)
    assert any(abs(s["freq_hz"] - 420.0) < 1e-6 for s in metrics["spikes"])
    assert metrics["rms"] > 0
    assert 0.0 <= metrics["spectral_flatness"] <= 1.0


def test_bitcoin_offline_fixture():
    bitcoin_substrate.requests = None
    try:
        r = bitcoin_substrate.collect()
    finally:
        import importlib
        importlib.reload(bitcoin_substrate)
    assert r.offline
    assert r.channel == "bitcoin_substrate"
    assert r.metrics["tip_height"] > 900_000
    assert r.metrics["hashrate_3d_avg_ehs"] > 100.0
    assert not r.anomaly_flag  # recorded fixture is fee-quiet


def test_bitcoin_congestion_flag():
    out = bitcoin_substrate._analyze(
        969801,
        {"fastestFee": 62, "halfHourFee": 40, "hourFee": 20},
        {"count": 160000, "vsize": 9e7},
        {"currentHashrate": 9.5e20, "hashrates": []})
    assert out["anomaly_flag"]
    assert "fee congestion" in out["notes"]
    out = bitcoin_substrate._analyze(
        969801,
        {"fastestFee": 2, "halfHourFee": 1, "hourFee": 1},
        {"count": 77002, "vsize": 4.1e7},
        {"currentHashrate": 9.5e20, "hashrates": []})
    assert not out["anomaly_flag"]


def test_tiers_new_environmental_rules():
    def rd(ch, metrics):
        return ChannelReading(timestamp_utc="2024-01-01T00:00:00Z",
                              channel=ch, metrics=metrics)

    v = evaluate_tiers([rd("usgs_seismic", {"rate_zscore": 4.2})])
    assert "t1_quake_rate" in v.tier1_hits
    v = evaluate_tiers([rd("noaa_geomag", {"kp_latest": 7.33})])
    assert "t1_geomag_storm_major" in v.tier1_hits
    v = evaluate_tiers([rd("noaa_solar",
                           {"x_flares_24h": 1, "m_flares_24h": 0})])
    assert "t1_solar_x" in v.tier1_hits
    v = evaluate_tiers([rd("radio_stream",
                           {"spikes": [{"freq_hz": 280.0,
                                        "excess_db": 9.1}]})])
    assert "t1_null_spike" in v.tier1_hits

    # quiet readings -> no tier-1 hits
    v = evaluate_tiers([rd("usgs_seismic", {"rate_zscore": 0.5}),
                        rd("noaa_geomag", {"kp_latest": 2.0}),
                        rd("noaa_solar", {"x_flares_24h": 0,
                                          "m_flares_24h": 1})])
    assert not v.tier1_hits


# --------------------------------------------------------------------------
# harmonic + geometric resonance channels
# --------------------------------------------------------------------------

def _synth_harmonic_series(sr=44100, seconds=3.0, f0=110.0):
    t = np.arange(int(sr * seconds)) / sr
    x = sum(0.2 * np.sin(2 * np.pi * f0 * k * t) for k in (1, 2, 3, 4))
    x = x + 0.002 * np.random.default_rng(21).standard_normal(t.shape[0])
    return x, sr


def test_harmonic_synth_series_detected():
    x, sr = _synth_harmonic_series()
    a = harmonic_resonance.analyze_harmonicity(x, sr)
    assert abs(a["fundamental_hz"] - 110.0) < 2.0, a
    assert a["n_harmonics"] >= 3, a
    assert a["harmonicity_score"] >= 3 / 7


def test_harmonic_noise_not_a_code():
    sr = 44100
    x = np.random.default_rng(22).standard_normal(3 * sr)
    a = harmonic_resonance.analyze_harmonicity(x, sr)
    assert a["n_harmonics"] < 3, a


def test_harmonic_offline_no_fabrication():
    # the stream resolver lives in radio_stream: null both modules
    harmonic_resonance.requests = None
    radio_stream.requests = None
    try:
        r = harmonic_resonance.collect(streams=[])
    finally:
        import importlib
        importlib.reload(harmonic_resonance)
        importlib.reload(radio_stream)
    assert r.offline
    assert r.channel == "harmonic_resonance"
    assert not r.anomaly_flag


def test_clark_evans_regular_grid():
    g = np.linspace(-5, 5, 8)
    lon, lat = np.meshgrid(g, g)
    ce = geometric_resonance.clark_evans(lon.ravel(), lat.ravel())
    assert ce["R"] > 1.5, ce
    assert ce["z"] > 3.09, ce


def test_clark_evans_clustered_not_regular():
    rng = np.random.default_rng(23)
    centers = [(-3, -3), (3, 3), (-3, 3), (3, -3), (0, 0)]
    pts = np.vstack([np.array(c) + 0.05 * rng.standard_normal((20, 2))
                     for c in centers])
    ce = geometric_resonance.clark_evans(pts[:, 0], pts[:, 1])
    assert ce["R"] < 1.0, ce
    assert ce["z"] < 3.09, ce


def test_lomb_scargle_finds_periodicity():
    # 10 days of events, rate modulated at 24 h
    rng = np.random.default_rng(24)
    times = []
    for h in range(240):
        n = 4 if (h % 24) < 6 else 0
        times += list((h + rng.random(n)) * 3600.0)
    times = np.array(sorted(times))
    ls = geometric_resonance.lomb_scargle_screen(times, 0.0, 10 * 86400.0)
    assert ls["available"]
    assert ls["flag"], ls
    assert abs(ls["best_period_h"] - 24.0) < 2.0, ls


def test_lomb_scargle_quiet_on_uniform():
    rng = np.random.default_rng(25)
    times = np.sort(rng.random(300) * 10 * 86400.0)
    ls = geometric_resonance.lomb_scargle_screen(times, 0.0, 10 * 86400.0)
    assert ls["available"]
    assert not ls["flag"], ls


def test_geometric_offline_fixture():
    geometric_resonance.requests = None
    try:
        r = geometric_resonance.collect()
    finally:
        import importlib
        importlib.reload(geometric_resonance)
    assert r.offline
    assert r.channel == "geometric_resonance"
    assert r.metrics["n_events"] == 120
    assert "R" in r.metrics["spatial"]
    assert "fap" in r.metrics["temporal"] or \
        "note" in r.metrics["temporal"]
    # flag must match the sub-screen outcomes, whatever the fixture holds
    spatial_hit = r.metrics["spatial"].get("z", 0) > 3.09
    temporal_hit = bool(r.metrics["temporal"].get("flag"))
    assert r.anomaly_flag == (spatial_hit or temporal_hit)


def test_tiers_harmonic_geometric_rules():
    def rd(ch, metrics):
        return ChannelReading(timestamp_utc="2024-01-01T00:00:00Z",
                              channel=ch, metrics=metrics)

    v = evaluate_tiers([rd("harmonic_resonance", {"persistent": True})])
    assert "t1_harmonic_code" in v.tier1_hits
    v = evaluate_tiers([rd("harmonic_resonance", {"persistent": False})])
    assert "t1_harmonic_code" not in v.tier1_hits

    v = evaluate_tiers([rd("geometric_resonance",
                           {"spatial": {"z": 4.5},
                            "temporal": {"flag": False}})])
    assert "t1_geometric_regular" in v.tier1_hits
    v = evaluate_tiers([rd("geometric_resonance",
                           {"spatial": {"z": -8.0},
                            "temporal": {"flag": True,
                                         "best_period_h": 24.0}})])
    assert "t1_geometric_regular" in v.tier1_hits
    v = evaluate_tiers([rd("geometric_resonance",
                           {"spatial": {"z": -8.0},
                            "temporal": {"flag": False}})])
    assert "t1_geometric_regular" not in v.tier1_hits


def test_decode_reply_checked_refuses_silence_and_noise():
    """The gated decoder must refuse silence/noise, accept real signals."""
    import numpy as np
    from ars_core.beacon import (DEFAULT_SR, decode_reply_checked,
                                 encode_reply)
    silence = np.zeros(13 * DEFAULT_SR)
    trits, snr = decode_reply_checked(silence)
    assert trits is None and snr <= 0.0
    rng = np.random.default_rng(7)
    noise = 0.001 * rng.standard_normal(13 * DEFAULT_SR)
    trits, snr = decode_reply_checked(noise)
    assert trits is None and snr < 6.0
    msg = [2, 0, 1, 2, 2, 0, 0, 1, 0, 1, 2, 0, 1]
    trits, snr = decode_reply_checked(encode_reply(msg))
    assert trits == msg and snr >= 20.0


# --------------------------------------------------------------------------
# plain-python runner (kept last so every test_ above is collected)
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
