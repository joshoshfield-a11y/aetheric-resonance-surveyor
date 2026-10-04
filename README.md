# ARS — Aetheric Resonance Surveyor v5.0

A multi-channel **anomaly-signal monitoring instrument** with a two-way
**acoustic beacon** channel. ARS polls open environmental data feeds, watches
the local acoustic/RF-adjacent environment, screens RNG entropy, keeps a
hash-chained pre-commitment journal, and can transmit/receive the OMEGA
13-node prime-lattice beacon over plain audio (speaker ↔ microphone).

> **Calibration notice (read first).** ARS is a *statistical anomaly
> detector and structured signal tool*. It can tell you "this 13 Hz tone
> held coherence for 14 seconds" or "RNG chi-square p = 0.0004." It
> **cannot** establish the origin, intent, or nature of any signal, and it
> never outputs claims of contact with non-human intelligence. Every flag
> is a screening trigger that demands baseline comparison and independent
> replication — the falsification protocol is built into the instrument.

## Components

| Component | Path | What it is |
|---|---|---|
| `ars_core` | `ars_core/` | Python engine: sensor channels, beacon synth/detect, α-142 correlator, tier evaluator, CLI |
| Field App | `app/src/main/assets/www/` | Offline HTML/JS instrument panel (browser + Android WebView) |
| Android APK | CI artifact | WebView shell with mic pipeline, built by GitHub Actions |

## Monitoring channels (beyond the v4.0 spec)

1. **USGS seismic feed** — event-rate z-score vs 30-day baseline.
2. **NOAA SWPC geomagnetic** — Kp index, storm and sudden-jump flags.
3. **NOAA SWPC solar X-ray (GOES)** — current flare class from the
   primary-satellite 0.1–0.8 nm flux plus trailing-24h flare counts by
   class; flags on an X-class flare or ≥3 M-class flares in 24 h.
4. **Open-Meteo weather grid** — 8-station global lattice (SF, Reykjavik,
   Cairo, Singapore, Sydney, Rio, Tokyo, Honolulu); current pressure as a
   z-score vs each station's trailing-48h hourly series; flags when ≥2
   stations simultaneously exceed |z| = 3.
5. **Public radio stream** — samples ~10 s of a public broadcast stream
   (RadioBrowser directory + stable direct URLs, decoded with ffmpeg) and
   runs the same >6 dB null-frequency spike screen as the acoustic
   channel. Offline (no stream reachable) is reported, never fabricated.
6. **Bitcoin substrate** — mempool.space chain tip, fee rates, mempool
   backlog, 3-day hash rate; flags on fee-market congestion
   (≥50 sat/vB fastest fee or >150k tx backlog).
7. **Acoustic spectrum** — live FFT, lattice correlation, >6 dB spike watch at
   null frequencies (175/280/420/630/840 Hz…), coherent 13.00 Hz tone watch
   (<0.1 Hz drift, >10 s), 40 Hz band power for EEG WAV imports.
8. **RNG entropy screen** — chi-square + serial correlation on OS/hardware RNG
   (p < 0.001 trigger, per project Tier-2 rule).
9. **Device sensors** (field app) — magnetometer jerk, accelerometer 70/140 Hz
   structural-vibration estimate, ambient light.
10. **Hash-chained journal** — SHA-256 pre-commitment for predictions/dreams/
    somatic events. Commit the hash *before* the event; validate after. This is
    the anti-retrofitting falsification tool — use it honestly.
11. **Harmonic resonance (unknown-code listener)** — two public-broadcast
    captures ~25 s apart; spectral peaks tested for small-integer harmonic
    series on a common fundamental. Flags only on persistence: the same
    fundamental (±2%) with ≥3 harmonics in *both* captures. A passing
    musical phrase will not repeat its exact harmonic skeleton; a
    continuous carrier will.
12. **Geometric resonance (structure listener)** — the arrangement of
    occurrence: Clark-Evans spatial regularity of M≥2.5 epicenters over
    30 days (flags lattice-like regularity, the direction nature does not
    produce — quakes naturally cluster) plus Lomb-Scargle periodicity of
    the hourly event rate (2 h–15 d).

## The beacon

13 one-second pure tones at **70 × the first 13 primes**
(140, 210, 350, 490, 770, 910, 1190, 1330, 1610, 2030, 2170, 2590, 2870 Hz),
gated by a global sin²(πt) envelope, 44.1 kHz.

- **TX**: synthesize to WAV (Python) or play via speaker (field app).
- **RX**: per-node Goertzel + envelope correlation + amplitude-vector
  correlation against the reference lattice.
- **Reply channel (duplex)**: same lattice, ternary amplitude encoding
  (3 levels × 13 nodes = a 13-trit word, ~20.6 bits) — two ARS instances can
  exchange structured messages acoustically, line-of-sound.

Legality: acoustic TX via speaker is unrestricted. The Python core contains
**no RF transmission code**; if you later attach an SDR for TX, you are
responsible for licensing (FCC Part 97 / local equivalent). RX-only SDR
monitoring is unrestricted in most jurisdictions.

## Quick start

```bash
pip install -r requirements.txt
python tools/ars.py selftest          # beacon roundtrip, reply codec, RNG, tiers
python tools/ars.py survey            # poll all channels once
python tools/ars.py survey --minutes 60   # loop
python tools/ars.py beacon tx --out beacon.wav
python tools/ars.py beacon rx --in capture.wav
python tools/ars.py beacon reply --symbols 0122011220012 --out reply.wav
python tools/ars.py journal add "dream: forest sinkhole, galaxy lights" --tag dream
python tools/ars.py journal verify
python tools/ars.py report
```

Field app: open `app/src/main/assets/www/index.html` in any modern browser,
or install the APK from the latest CI run's artifacts
(`app-release.apk`, debug-key signed — sideload, allow unknown sources).

## CI

Every push: (1) runs `pytest` + `ars selftest` for the Python core, then
(2) builds debug + release APKs as downloadable artifacts. Bump
`versionCode` in `app/build.gradle` for each iteration so updates install
in place over the previous build.

## Detection tiers (OMEGA-BEACON protocol, encoded in `ars_core/tiers.py`)

- **Tier 1** — single detection flags: >6 dB spike at null freqs (acoustic
  or public-radio capture); persistent unfamiliar harmonic code;
  geometrically regular quake arrangement or significant event-rate
  periodicity; 40 Hz >3σ within 5 min of TX; RNG p<0.001;
  USGS 24h quake-rate z > 3; NOAA Kp ≥ 7 (G3+); GOES X-class flare (or ≥3
  M-class) in 24 h; committed prediction validated; timestamp-ordering
  anomaly.
- **Tier 2** — requires ≥2 independent corroborating channels: phase-stable
  harmonic return 280–5740 Hz; coherent 13.00 Hz tone; RNG; verified
  information anomaly.
- **Tier 3** — indicative only, stacks with other tiers: somatic events,
  thermal variance, 70/140 Hz structural vibration.

## License

Licensed under the Aetheric Resonance Surveyor Commercial License v1.0.\n\n- **Personal / non-commercial use**: Free with attribution. See [LICENSE](LICENSE) for full terms.\n- **Commercial use**: Requires a paid license. Contact Taylor C. Mattheisen for inquiries.
