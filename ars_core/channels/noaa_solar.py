"""NOAA solar (GOES X-ray flare) channel.

Polls NOAA SWPC for the GOES primary-satellite 0.1-0.8 nm X-ray flux and
the 7-day flare event list. Reports the current solar X-ray flux as a
GOES flare class (A/B/C/M/X) and counts flares by class over the trailing
24 hours.

Flags when the trailing 24 h contain an X-class flare or >= 3 M-class
flares -- a genuinely unusual solar outburst rate. The flag is a
statistical screening statement, not a space-weather forecast.

Offline behaviour: on any failure the channel loads a small bundled
fixture (recorded 2026-10-04) and marks the reading ``offline=True``.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from typing import Any, Optional

from . import ChannelReading, utc_now_iso

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

CHANNEL = "noaa_solar"
FLARES_URL = "https://services.swpc.noaa.gov/json/goes/primary/xray-flares-7-day.json"
XRAYS_URL = "https://services.swpc.noaa.gov/json/goes/primary/xrays-7-day.json"
WINDOW_HOURS = 24
X_FLARE_FLAG = 1      # >= this many X-class flares in the window -> flag
M_FLARE_FLAG = 3      # >= this many M-class flares in the window -> flag

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                        "noaa_solar_fixture.json")

# GOES class boundaries in W/m^2 (0.1-0.8 nm band).
_CLASS_EDGES = (("X", 1e-4), ("M", 1e-5), ("C", 1e-6), ("B", 1e-7),
                ("A", 1e-8))


def class_of_flux(flux_wm2: float) -> str:
    """GOES flare class string for an X-ray flux, e.g. 'M2.4', 'B1.9'."""
    for letter, edge in _CLASS_EDGES:
        if flux_wm2 >= edge:
            return f"{letter}{flux_wm2 / edge:.1f}"
    return f"<A{flux_wm2 / 1e-8:.1f}"


def class_letter(goes_class: str) -> str:
    """First letter of a GOES class string ('M5.3' -> 'M')."""
    return goes_class.strip().upper()[:1] if goes_class else "?"


def _fetch_json(url: str, timeout: float) -> Any:
    if requests is None:
        raise RuntimeError("requests library not available")
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _parse_time(value: str) -> _dt.datetime:
    return _dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _analyze(flares: list[dict[str, Any]],
             xrays: list[dict[str, Any]],
             now: _dt.datetime) -> dict[str, Any]:
    cutoff = now - _dt.timedelta(hours=WINDOW_HOURS)
    counts = {"A": 0, "B": 0, "C": 0, "M": 0, "X": 0}
    max_class: Optional[str] = None
    max_flux = 0.0
    for fl in flares:
        try:
            mt = _parse_time(str(fl.get("max_time") or fl.get("time_tag")))
        except Exception:
            continue
        if mt < cutoff:
            continue
        letter = class_letter(str(fl.get("max_class", "")))
        if letter in counts:
            counts[letter] += 1
        fx = float(fl.get("max_xrlong") or 0.0)
        if fx > max_flux:
            max_flux = fx
            max_class = str(fl.get("max_class"))

    latest = xrays[-1] if xrays else {}
    flux = float(latest.get("flux") or 0.0)
    current_class = class_of_flux(flux)

    x24 = counts["X"]
    m24 = counts["M"]
    flag = (x24 >= X_FLARE_FLAG) or (m24 >= M_FLARE_FLAG)
    notes_parts = []
    if x24 >= X_FLARE_FLAG:
        notes_parts.append(f"{x24} X-class flare(s) in {WINDOW_HOURS}h")
    if m24 >= M_FLARE_FLAG:
        notes_parts.append(f"{m24} M-class flares in {WINDOW_HOURS}h")
    return {
        "metrics": {
            "current_flux_wm2": flux,
            "current_class": current_class,
            "satellite": latest.get("satellite"),
            "flux_time_tag": latest.get("time_tag"),
            "flares_24h": counts,
            "x_flares_24h": x24,
            "m_flares_24h": m24,
            "max_flare_24h": max_class,
            "max_flux_24h_wm2": max_flux,
            "window_hours": WINDOW_HOURS,
        },
        "anomaly_flag": flag,
        "notes": "; ".join(notes_parts) if notes_parts
                 else f"quiet: current {current_class}, "
                      f"{sum(counts.values())} flares in {WINDOW_HOURS}h",
    }


def _load_fixture() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    with open(_FIXTURE, "r", encoding="utf-8") as fh:
        fx = json.load(fh)
    flares = fx["flares_tail"]
    xrays = fx["xrays_tail"]
    return flares, xrays


def collect(now: Optional[_dt.datetime] = None,
            timeout: float = 15.0) -> ChannelReading:
    """Collect one solar X-ray reading (network with fixture fallback)."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    offline = False
    note = ""
    try:
        flares = _fetch_json(FLARES_URL, timeout)
        xrays = _fetch_json(XRAYS_URL, timeout)
        if not isinstance(flares, list) or not isinstance(xrays, list):
            raise RuntimeError("unexpected GOES payload shape")
    except Exception as exc:
        flares, xrays = _load_fixture()
        offline = True
        note = f"offline fixture used ({type(exc).__name__}: {exc})"

    out = _analyze(flares, xrays, now)
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
