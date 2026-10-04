"""Command-line interface for the Aetheric Resonance Surveyor.

Subcommands
-----------
survey    run all channels once (or in a loop), print a table, log to journal
beacon    tx / rx / listen / reply / decode for the 13-node lattice
journal   add / verify the hash-chained event journal
report    summarize the journal and last readings
selftest  synthesis/detection/reply/rng/journal roundtrip checks
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Optional, Sequence

import numpy as np

from . import CALIBRATION_DISCLAIMER, __version__
from .beacon import (DEFAULT_SR, decode_reply, decode_reply_checked, detect_lattice, encode_reply,
                     load_wav, save_wav, synthesize_lattice)
from .channels import ChannelReading, journal as journal_ch
from .channels import usgs_seismic, noaa_geomag, rng_entropy, audio_spectrum
from .channels import noaa_solar, openmeteo_weather, radio_stream
from .channels import bitcoin_substrate, harmonic_resonance
from .channels import geometric_resonance
from .alpha142 import evaluate
from .tiers import evaluate_tiers


# --------------------------------------------------------------------------
# survey
# --------------------------------------------------------------------------

def _run_channels_once(simulate: bool) -> list[ChannelReading]:
    readings = []
    if simulate:
        # deterministic synthetic stand-ins; never touch the network
        from .channels import utc_now_iso
        readings.append(ChannelReading(
            timestamp_utc=utc_now_iso(), channel="usgs_seismic",
            metrics={"recent_rate_per_day": 47.0,
                     "baseline_rate_per_day": 47.4, "rate_zscore": -0.05},
            anomaly_flag=False, notes="simulated", offline=True))
        readings.append(ChannelReading(
            timestamp_utc=utc_now_iso(), channel="noaa_geomag",
            metrics={"kp_latest": 2.0, "kp_jump": -0.33},
            anomaly_flag=False, notes="simulated", offline=True))
        readings.append(ChannelReading(
            timestamp_utc=utc_now_iso(), channel="noaa_solar",
            metrics={"current_flux_wm2": 1.9e-7, "current_class": "B1.9",
                     "x_flares_24h": 0, "m_flares_24h": 0,
                     "max_flare_24h": "C3.1"},
            anomaly_flag=False, notes="simulated", offline=True))
        readings.append(ChannelReading(
            timestamp_utc=utc_now_iso(), channel="openmeteo_weather",
            metrics={"n_stations": 8, "n_extreme": 0, "max_abs_z": 0.42},
            anomaly_flag=False, notes="simulated", offline=True))
        readings.append(ChannelReading(
            timestamp_utc=utc_now_iso(), channel="bitcoin_substrate",
            metrics={"tip_height": 0, "fastest_fee_satvb": 1,
                     "mempool_tx_count": 50000, "hashrate_3d_avg_ehs": 0.0},
            anomaly_flag=False, notes="simulated", offline=True))
        readings.append(ChannelReading(
            timestamp_utc=utc_now_iso(), channel="radio_stream",
            metrics={"stream_name": "simulated", "spikes": [],
                     "captured_seconds": 0.0},
            anomaly_flag=False, notes="simulated", offline=True))
        readings.append(ChannelReading(
            timestamp_utc=utc_now_iso(), channel="harmonic_resonance",
            metrics={"persistent": False,
                     "capture_1": {"fundamental_hz": 110.0, "n_harmonics": 2},
                     "capture_2": {"fundamental_hz": 110.0, "n_harmonics": 2}},
            anomaly_flag=False, notes="simulated", offline=True))
        readings.append(ChannelReading(
            timestamp_utc=utc_now_iso(), channel="geometric_resonance",
            metrics={"n_events": 120,
                     "spatial": {"R": 0.62, "z": -8.1},
                     "temporal": {"fap": 0.4, "flag": False}},
            anomaly_flag=False, notes="simulated", offline=True))
    else:
        readings.append(usgs_seismic.collect())
        readings.append(noaa_geomag.collect())
        readings.append(noaa_solar.collect())
        readings.append(openmeteo_weather.collect())
        readings.append(bitcoin_substrate.collect())
        readings.append(radio_stream.collect())
        readings.append(harmonic_resonance.collect())
        readings.append(geometric_resonance.collect())
    readings.append(rng_entropy.collect(n_bytes=20_000))
    readings.append(audio_spectrum.collect())
    return readings


def _print_table(readings: Sequence[ChannelReading]) -> None:
    print(f"{'channel':<16} {'flag':<5} {'offline':<7} notes")
    print("-" * 72)
    for r in readings:
        print(f"{r.channel:<16} {str(r.anomaly_flag):<5} "
              f"{str(r.offline):<7} {r.notes[:48]}")


def cmd_survey(args: argparse.Namespace) -> int:
    journal_path = args.journal
    iterations = 1
    interval = 0.0
    if args.minutes:
        interval = 60.0 * args.minutes / max(1, args.loops)
        iterations = max(1, args.loops)
    for i in range(iterations):
        readings = _run_channels_once(args.simulate)
        print(f"--- survey pass {i + 1}/{iterations} ---")
        _print_table(readings)
        verdict = evaluate_tiers(readings)
        print(verdict.summary)
        entry = journal_ch.add_entry(
            "survey: " + "; ".join(
                f"{r.channel} flag={r.anomaly_flag}" for r in readings),
            path=journal_path, tag="survey")
        print(f"journal entry {entry['hash'][:12]}... committed")
        if i + 1 < iterations:
            time.sleep(interval)
    return 0


# --------------------------------------------------------------------------
# beacon
# --------------------------------------------------------------------------

def cmd_beacon_tx(args: argparse.Namespace) -> int:
    sig = synthesize_lattice(sr=args.sr)
    save_wav(args.out, sig, args.sr)
    print(f"wrote {args.out}: 13-node lattice, {len(sig)} samples @ {args.sr} Hz")
    return 0


def _report_detection(res) -> None:
    print(f"duration           : {res.duration_s:.3f} s @ {res.sample_rate} Hz")
    print(f"amplitude vector r : {res.amplitude_correlation_r:.4f} "
          f"(p={res.amplitude_correlation_p:.2e})")
    print(f"envelope corr      : {res.envelope_correlation:.4f}")
    print(f"SNR (template)     : {res.snr_db:.2f} dB")
    print(f"confidence         : {res.confidence:.3f}")
    print(f"note               : {res.notes}")


def cmd_beacon_rx(args: argparse.Namespace) -> int:
    samples, sr = load_wav(args.infile)
    res = detect_lattice(samples, sr)
    _report_detection(res)
    return 0


def cmd_beacon_listen(args: argparse.Namespace) -> int:
    try:
        samples, sr = audio_spectrum.capture_mic(seconds=args.seconds)
    except RuntimeError as exc:
        print(f"listen unavailable: {exc}")
        return 2
    _report_detection(detect_lattice(samples, sr))
    return 0


def cmd_beacon_reply(args: argparse.Namespace) -> int:
    symbols = [int(c) for c in args.symbols.strip()]
    sig = encode_reply(symbols, sr=args.sr)
    save_wav(args.out, sig, args.sr)
    print(f"wrote {args.out}: reply lattice encoding trits "
          f"{''.join(map(str, symbols))}")
    return 0


def cmd_beacon_decode(args: argparse.Namespace) -> int:
    samples, sr = load_wav(args.infile)
    symbols, snr_db = decode_reply_checked(samples, sr)
    if symbols is None:
        print(f"NO SIGNAL — loudest window only {snr_db:.1f} dB above the "
              f"envelope-null noise floor (< 6 dB gate). Decode refused: "
              f"classifying silence would fabricate trits.")
        return 2
    print("decoded trits:", "".join(map(str, symbols)),
          f"(SNR {snr_db:.1f} dB; verify by repetition)")
    return 0


# --------------------------------------------------------------------------
# journal / report
# --------------------------------------------------------------------------

def cmd_journal_add(args: argparse.Namespace) -> int:
    entry = journal_ch.add_entry(args.text, path=args.journal, tag=args.tag)
    print(f"committed {entry['timestamp_utc']} hash={entry['hash'][:16]}...")
    print("note: this timestamp is a pre-commitment; it cannot be edited "
          "without breaking the chain.")
    return 0


def cmd_journal_verify(args: argparse.Namespace) -> int:
    ok = journal_ch.verify_chain(args.journal)
    print("hash chain VALID" if ok else "hash chain BROKEN (tampering detected)")
    return 0 if ok else 1


def cmd_report(args: argparse.Namespace) -> int:
    entries = journal_ch.read_entries(args.journal)
    ok = journal_ch.verify_chain(args.journal)
    print(f"journal: {args.journal}")
    print(f"entries: {len(entries)}   chain valid: {ok}")
    for e in entries[-args.tail:]:
        tag = f"[{e['tag']}] " if e.get("tag") else ""
        print(f"  {e['timestamp_utc']} {tag}{e['content'][:70]}")
    print(CALIBRATION_DISCLAIMER)
    return 0 if ok else 1


# --------------------------------------------------------------------------
# selftest
# --------------------------------------------------------------------------

def _check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}"
          + (f" -- {detail}" if detail else ""))
    return ok


def cmd_selftest(args: argparse.Namespace) -> int:
    print(f"ARS selftest (v{__version__})")
    all_ok = True

    sig = synthesize_lattice()
    res = detect_lattice(sig, DEFAULT_SR)
    all_ok &= _check("beacon synth->detect roundtrip",
                     res.confidence > 0.9,
                     f"confidence={res.confidence:.3f}")

    rng = np.random.default_rng(142)
    msg = [int(rng.integers(0, 3)) for _ in range(13)]
    back = decode_reply(encode_reply(msg), DEFAULT_SR)
    all_ok &= _check("reply encode/decode roundtrip", back == msg,
                     "".join(map(str, msg)))

    reading = rng_entropy.collect(n_bytes=20_000)
    p = min(s["chi2_p"] for s in reading.metrics.values())
    all_ok &= _check("rng chi-square sane on os.urandom",
                     0.0 <= p <= 1.0 and not reading.anomaly_flag,
                     f"p={p:.4f}")

    import tempfile, os
    with tempfile.TemporaryDirectory() as td:
        jp = os.path.join(td, "j.jsonl")
        journal_ch.add_entry("selftest entry", path=jp, tag="selftest")
        ok = journal_ch.verify_chain(jp)
        with open(jp, "a", encoding="utf-8") as fh:
            fh.write('{"timestamp_utc":"2024-01-01T00:00:00Z","tag":null,'
                     '"content":"tampered","prev_hash":"0","hash":"0"}\n')
        bad = journal_ch.verify_chain(jp)
    all_ok &= _check("journal hash-chain verify + tamper detection",
                     ok and not bad)

    noise = np.random.default_rng(7).standard_normal(13 * DEFAULT_SR)
    rn = detect_lattice(noise.astype(np.float32), DEFAULT_SR)
    all_ok &= _check("noise rejection (negative control)",
                     rn.confidence < 0.6, f"confidence={rn.confidence:.3f}")

    report = evaluate(_run_channels_once(simulate=True))
    all_ok &= _check("composite evaluate + tiers run",
                     report.summary.endswith(CALIBRATION_DISCLAIMER))

    print("SELFTEST:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="ars", description="Aetheric Resonance Surveyor -- anomaly-signal "
                                "monitoring instrument (statistical outputs only)")
    p.add_argument("--version", action="version", version=f"ars {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("survey", help="run all channels and log to journal")
    sp.add_argument("--minutes", type=float, default=0.0,
                    help="total survey duration (0 = single pass)")
    sp.add_argument("--loops", type=int, default=1,
                    help="number of passes within --minutes")
    sp.add_argument("--simulate", action="store_true",
                    help="use synthetic channel data (no network)")
    sp.add_argument("--journal", default=journal_ch.DEFAULT_PATH)
    sp.set_defaults(func=cmd_survey)

    bp = sub.add_parser("beacon", help="beacon lattice operations")
    bsub = bp.add_subparsers(dest="beacon_cmd", required=True)
    tx = bsub.add_parser("tx", help="synthesize the beacon to a WAV file")
    tx.add_argument("--out", required=True)
    tx.add_argument("--sr", type=int, default=DEFAULT_SR)
    tx.set_defaults(func=cmd_beacon_tx)
    rx = bsub.add_parser("rx", help="detect the lattice in a WAV file")
    rx.add_argument("--in", dest="infile", required=True)
    rx.set_defaults(func=cmd_beacon_rx)
    ls = bsub.add_parser("listen", help="detect from the microphone (optional)")
    ls.add_argument("--seconds", type=float, default=15.0)
    ls.set_defaults(func=cmd_beacon_listen)
    rp = bsub.add_parser("reply", help="encode a 13-trit reply to WAV")
    rp.add_argument("--symbols", required=True,
                    help="13 characters, each 0/1/2")
    rp.add_argument("--out", required=True)
    rp.add_argument("--sr", type=int, default=DEFAULT_SR)
    rp.set_defaults(func=cmd_beacon_reply)
    dc = bsub.add_parser("decode", help="decode a reply WAV to 13 trits")
    dc.add_argument("--in", dest="infile", required=True)
    dc.set_defaults(func=cmd_beacon_decode)

    jp = sub.add_parser("journal", help="hash-chained event journal")
    jsub = jp.add_subparsers(dest="journal_cmd", required=True)
    ja = jsub.add_parser("add", help="commit an entry")
    ja.add_argument("text")
    ja.add_argument("--tag", default=None)
    ja.add_argument("--journal", default=journal_ch.DEFAULT_PATH)
    ja.set_defaults(func=cmd_journal_add)
    jv = jsub.add_parser("verify", help="verify the hash chain")
    jv.add_argument("--journal", default=journal_ch.DEFAULT_PATH)
    jv.set_defaults(func=cmd_journal_verify)

    rep = sub.add_parser("report", help="summarize journal and readings")
    rep.add_argument("--journal", default=journal_ch.DEFAULT_PATH)
    rep.add_argument("--tail", type=int, default=10)
    rep.set_defaults(func=cmd_report)

    st = sub.add_parser("selftest", help="run built-in roundtrip checks")
    st.set_defaults(func=cmd_selftest)
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
