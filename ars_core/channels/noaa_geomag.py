"""NOAA geomagnetic (planetary K-index) channel.

Polls NOAA SWPC for the 3-hour planetary K-index. Flags when the latest
Kp >= 5 (NOAA G1 storm threshold) or when Kp jumped by >= 2 within the
recent window. Falls back to a bundled fixture when offline.
"""

from __future__ import annotations

import json
import os
from typing import Any

from . import ChannelReading, utc_now_iso

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

CHANNEL = "noaa_geomag"
KP_URL = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
KP_STORM_THRESHOLD = 5.0
KP_JUMP_THRESHOLD = 2.0

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                        "noaa_geomag_fixture.json")


def _fetch_kp_series(timeout: float = 10.0) -> list[dict[str, Any]]:
    """List of {time_tag, kp} dicts, oldest first. Raises on any failure."""
    if requests is None:
        raise RuntimeError("requests library not available")
    resp = requests.get(KP_URL, timeout=timeout)
    resp.raise_for_status()
    rows = resp.json()
    if not isinstance(rows, list) or len(rows) < 2:
        raise RuntimeError("unexpected K-index payload")
    if isinstance(rows[0], dict):
        # current SWPC format: list of {"time_tag": ..., "Kp": ...}
        out = [{"time_tag": str(r["time_tag"]), "kp": float(r["Kp"])}
               for r in rows]
    else:
        # legacy format: header row followed by arrays
        header, data = rows[0], rows[1:]
        i_time = header.index("time_tag")
        i_kp = header.index("Kp")
        out = [{"time_tag": str(r[i_time]), "kp": float(r[i_kp])}
               for r in data]
    out.sort(key=lambda d: d["time_tag"])
    return out


def _load_fixture() -> list[dict[str, Any]]:
    with open(_FIXTURE, "r", encoding="utf-8") as fh:
        return json.load(fh)["kp_series"]


def collect(timeout: float = 10.0) -> ChannelReading:
    """Collect one geomagnetic reading (network with fixture fallback)."""
    offline = False
    note = ""
    try:
        series = _fetch_kp_series(timeout)
    except Exception as exc:
        series = _load_fixture()
        offline = True
        note = f"offline fixture used ({type(exc).__name__}: {exc})"

    latest = series[-1]
    kp_now = float(latest["kp"])
    # 1-hour jump: Kp is 3-hourly, so "1-hr jump" is approximated by the
    # delta between the two most recent 3-hour synoptic values (documented
    # approximation -- SWPC publishes no finer planetary cadence).
    kp_prev = float(series[-2]["kp"]) if len(series) > 1 else kp_now
    jump = kp_now - kp_prev

    storm = kp_now >= KP_STORM_THRESHOLD
    jump_flag = jump >= KP_JUMP_THRESHOLD
    flag = storm or jump_flag
    if storm:
        note = (note + " " if note else "") + f"Kp {kp_now} >= storm threshold"
    if jump_flag:
        note = (note + " " if note else "") + f"Kp jump +{jump} in one step"
    return ChannelReading(
        timestamp_utc=utc_now_iso(),
        channel=CHANNEL,
        metrics={
            "kp_latest": kp_now,
            "kp_previous": kp_prev,
            "kp_jump": jump,
            "kp_time_tag": latest["time_tag"],
            "storm_threshold": KP_STORM_THRESHOLD,
        },
        anomaly_flag=flag,
        notes=note or "quiet",
        offline=offline,
    )
