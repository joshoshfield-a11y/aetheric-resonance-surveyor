"""Open-Meteo global weather-grid channel.

Polls the free, keyless Open-Meteo forecast API for a fixed 8-station
global lattice (both hemispheres, all longitudes) and reports each
station's current mean-sea-level pressure as a z-score against its own
trailing-48-hour hourly series.

Flags when >= 2 stations simultaneously show |z| > 3 -- a coherent
planetary-scale pressure swing relative to each station's own recent
baseline. The flag is a statistical screening statement, not a weather
forecast.

Offline behaviour: on any failure the channel loads a small bundled
fixture (recorded 2026-10-04) and marks the reading ``offline=True``.
"""

from __future__ import annotations

import json
import math
import os
from typing import Any, Optional

from . import ChannelReading, utc_now_iso

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

CHANNEL = "openmeteo_weather"
BASE_URL = "https://api.open-meteo.com/v1/forecast"

# (name, latitude, longitude): one station per broad region.
STATIONS: tuple[tuple[str, float, float], ...] = (
    ("san_francisco", 37.77, -122.42),
    ("reykjavik", 64.15, -21.94),
    ("cairo", 30.04, 31.24),
    ("singapore", 1.35, 103.82),
    ("sydney", -33.87, 151.21),
    ("rio_de_janeiro", -22.91, -43.17),
    ("tokyo", 35.68, 139.69),
    ("honolulu", 21.31, -157.86),
)

BASELINE_HOURS = 48
Z_THRESHOLD = 3.0
EXTREME_STATIONS_FLAG = 2  # >= this many stations with |z| > 3 -> flag

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                        "openmeteo_weather_fixture.json")


def _fetch(timeout: float = 20.0) -> list[dict[str, Any]]:
    if requests is None:
        raise RuntimeError("requests library not available")
    params = {
        "latitude": ",".join(str(s[1]) for s in STATIONS),
        "longitude": ",".join(str(s[2]) for s in STATIONS),
        "current": "pressure_msl,temperature_2m,wind_speed_10m",
        "hourly": "pressure_msl",
        "past_days": 2,
        "timezone": "UTC",
    }
    resp = requests.get(BASE_URL, params=params, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, list) or len(data) != len(STATIONS):
        raise RuntimeError("unexpected Open-Meteo payload shape")
    out = []
    for name, payload in zip((s[0] for s in STATIONS), data):
        out.append({"name": name,
                    "latitude": payload.get("latitude"),
                    "longitude": payload.get("longitude"),
                    "current": payload.get("current", {}),
                    "hourly": payload.get("hourly", {})})
    return out


def _load_fixture() -> list[dict[str, Any]]:
    with open(_FIXTURE, "r", encoding="utf-8") as fh:
        fx = json.load(fh)
    out = []
    for i, st in enumerate(fx["stations"]):
        name = st.get("name") or (
            STATIONS[i][0] if i < len(STATIONS) else f"station_{i}")
        out.append({**st, "name": name})
    return out


def _pressure_z(current_hpa: float,
                series: list[Optional[float]]) -> float:
    """z-score of the current pressure vs the trailing-48h hourly series."""
    vals = [v for v in series[-BASELINE_HOURS:]
            if isinstance(v, (int, float))]
    if len(vals) < 12:
        return 0.0
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    if var <= 0:
        return 0.0
    return (current_hpa - mean) / math.sqrt(var)


def _analyze(stations: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for st in stations:
        cur = st.get("current", {}) or {}
        hourly = st.get("hourly", {}) or {}
        p_now = cur.get("pressure_msl")
        series = hourly.get("pressure_msl") or []
        z = _pressure_z(float(p_now), series) \
            if isinstance(p_now, (int, float)) else 0.0
        rows.append({
            "name": st.get("name"),
            "pressure_hpa": p_now,
            "temp_c": cur.get("temperature_2m"),
            "wind_kmh": cur.get("wind_speed_10m"),
            "pressure_z_48h": round(z, 3),
        })
    extreme = [r for r in rows if abs(r["pressure_z_48h"]) > Z_THRESHOLD]
    max_abs_z = max((abs(r["pressure_z_48h"]) for r in rows), default=0.0)
    flag = len(extreme) >= EXTREME_STATIONS_FLAG
    if flag:
        notes = ("coherent pressure swing: "
                 + ", ".join(f"{r['name']} z={r['pressure_z_48h']:.2f}"
                             for r in extreme))
    else:
        notes = (f"grid calm: max |z|={max_abs_z:.2f} across "
                 f"{len(rows)} stations")
    return {
        "metrics": {
            "stations": rows,
            "n_stations": len(rows),
            "n_extreme": len(extreme),
            "max_abs_z": round(max_abs_z, 3),
            "z_threshold": Z_THRESHOLD,
            "baseline_hours": BASELINE_HOURS,
        },
        "anomaly_flag": flag,
        "notes": notes,
    }


def collect(timeout: float = 20.0) -> ChannelReading:
    """Collect one weather-grid reading (network with fixture fallback)."""
    offline = False
    note = ""
    try:
        stations = _fetch(timeout)
    except Exception as exc:
        stations = _load_fixture()
        offline = True
        note = f"offline fixture used ({type(exc).__name__}: {exc})"

    out = _analyze(stations)
    notes = out["notes"]
    if offline:
        notes = (note + " -- " if note else "") + notes
    return ChannelReading(
        timestamp_utc=utc_now_iso(),
        channel=CHANNEL,
        metrics=out["metrics"],
        anomaly_flag=out["anomaly_flag"],
        notes=notes,
        offline=offline,
    )
