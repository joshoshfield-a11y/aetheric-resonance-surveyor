"""Geometric-resonance channel: the structure listener.

Where other channels watch *how much* is happening, this one watches
*how events are arranged* -- the geometry of occurrence. Two sub-screens:

1. **Spatial regularity (Clark-Evans).** USGS epicenters (M>=2.5, trailing
   30 days) are tested against complete spatial randomness. Natural
   seismicity clusters on faults (R < 1, expected); a significantly
   *regular* / lattice-like arrangement (R > 1, one-sided p < 0.001) is
   the geometric anomaly -- the direction nature does not produce.

2. **Temporal periodicity (Lomb-Scargle).** The same events, binned
   hourly, are tested for significant periodicity at 2 h .. 15 d. The
   false-alarm probability uses the standard exponential approximation;
   the flag reports the period honestly (12 h / 24 h tidal components
   are natural and named as such by the operator, not the instrument).

A flag means "the arrangement of recent earthquakes is geometrically
unlikely under the stated null." It is a screening trigger, never an
identification.

Offline behaviour: on any failure the channel loads a bundled fixture
(recorded 2026-10-04, 120 real M>=4.5 events) and marks the reading
``offline=True``.
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import os
from typing import Any, Optional

import numpy as np

from . import ChannelReading, utc_now_iso

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

CHANNEL = "geometric_resonance"
FDSN_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
MIN_MAGNITUDE = 2.5
WINDOW_DAYS = 30
MAX_EVENTS = 3000
MIN_EVENTS = 10
CE_Z_THRESHOLD = 3.09        # one-sided p < 0.001, regularity direction
LS_FAP_THRESHOLD = 0.001
LS_MIN_PERIOD_H = 2.0
LS_MAX_PERIOD_H = 15 * 24.0

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                        "geometric_resonance_fixture.json")


# --------------------------------------------------------------------------
# spatial: Clark-Evans nearest-neighbour regularity
# --------------------------------------------------------------------------

def _haversine_km(lon1: np.ndarray, lat1: np.ndarray,
                  lon2: np.ndarray, lat2: np.ndarray) -> np.ndarray:
    r = 6371.0
    p1, p2 = np.radians(lat1), np.radians(lat2)
    dp = np.radians(lat2 - lat1)
    dl = np.radians(lon2 - lon1)
    a = np.sin(dp / 2.0) ** 2 + np.cos(p1) * np.cos(p2) * np.sin(dl / 2.0) ** 2
    return 2.0 * r * np.arcsin(np.sqrt(a))


def clark_evans(lons: np.ndarray, lats: np.ndarray) -> dict[str, float]:
    """Clark-Evans R and z for epicenter regularity.

    R = observed mean NN distance / expected under CSR. R < 1 clustered
    (normal for quakes), R = 1 random, R > 1 regular/lattice-like.
    """
    n = lons.shape[0]
    lon = lons[:, None]
    lat = lats[:, None]
    d = _haversine_km(lon, lat, lon.T, lat.T)
    np.fill_diagonal(d, np.inf)
    nn = d.min(axis=1)
    d_obs = float(nn.mean())
    # study area: bounding box in km^2 (guards against degenerate boxes)
    lat0 = float(np.radians(lats.mean()))
    w_km = float((lons.max() - lons.min()) * 111.32 * math.cos(lat0))
    h_km = float((lats.max() - lats.min()) * 110.57)
    area = max(w_km * h_km, 1.0)
    rho = n / area
    d_exp = 1.0 / (2.0 * math.sqrt(rho))
    se = 0.26136 / math.sqrt(n * rho)
    R = d_obs / d_exp if d_exp > 0 else 0.0
    z = (d_obs - d_exp) / se if se > 0 else 0.0
    return {"n": float(n), "area_km2": round(area, 1),
            "mean_nn_km": round(d_obs, 3),
            "expected_nn_km": round(d_exp, 3),
            "R": round(R, 4), "z": round(z, 3)}


# --------------------------------------------------------------------------
# temporal: Lomb-Scargle periodicity of the event rate
# --------------------------------------------------------------------------

def lomb_scargle_screen(times_s: np.ndarray,
                        t0: float, t1: float) -> dict[str, Any]:
    """Significant periodicity in the hourly event rate, 2 h .. 15 d.

    Returns best period + false-alarm probability (exponential
    approximation). Requires scipy; reports unavailable without it.
    """
    try:
        from scipy import signal as _sig
    except Exception:
        return {"available": False,
                "note": "scipy not installed; temporal screen skipped"}
    nbins = int((t1 - t0) // 3600)
    if nbins < 48:
        return {"available": True, "note": "window too short",
                "fap": 1.0, "flag": False}
    edges = t0 + np.arange(nbins + 1) * 3600.0
    counts, _ = np.histogram(times_s, bins=edges)
    y = counts - counts.mean()
    var = float(y.var())
    if var <= 0:
        return {"available": True, "note": "no rate variance",
                "fap": 1.0, "flag": False}
    t = np.arange(nbins, dtype=np.float64)  # hours
    periods = np.linspace(LS_MIN_PERIOD_H, LS_MAX_PERIOD_H, 2000)
    w = 2.0 * np.pi / periods
    P = _sig.lombscargle(t, y, w, precenter=False)
    z = P / var                      # Exp(1) under the null
    M = float(w.shape[0])
    fap = 1.0 - (1.0 - math.exp(-float(z.max()))) ** M
    best_period = float(periods[int(np.argmax(P))])
    return {"available": True, "best_period_h": round(best_period, 2),
            "best_power_z": round(float(z.max()), 2),
            "fap": float(fap), "flag": bool(fap < LS_FAP_THRESHOLD),
            "n_frequencies": int(M)}


# --------------------------------------------------------------------------
# fetch + collect
# --------------------------------------------------------------------------

def _fetch_events(now: _dt.datetime,
                 timeout: float = 25.0) -> list[dict[str, Any]]:
    if requests is None:
        raise RuntimeError("requests library not available")
    fmt = "%Y-%m-%dT%H:%M:%S"
    resp = requests.get(FDSN_URL, params={
        "format": "geojson",
        "starttime": (now - _dt.timedelta(days=WINDOW_DAYS)).strftime(fmt),
        "endtime": now.strftime(fmt),
        "minmagnitude": MIN_MAGNITUDE,
        "limit": MAX_EVENTS,
        "orderby": "time",
    }, timeout=timeout)
    resp.raise_for_status()
    feats = resp.json().get("features", [])
    out = []
    for f in feats:
        try:
            coords = f["geometry"]["coordinates"]
            out.append({"t": float(f["properties"]["time"]) / 1000.0,
                        "lon": float(coords[0]), "lat": float(coords[1]),
                        "mag": f["properties"].get("mag")})
        except Exception:
            continue
    return out


def _load_fixture() -> list[dict[str, Any]]:
    with open(_FIXTURE, "r", encoding="utf-8") as fh:
        return json.load(fh)["events"]


def _analyze(events: list[dict[str, Any]],
             now: _dt.datetime) -> dict[str, Any]:
    notes: list[str] = []
    metrics: dict[str, Any] = {"n_events": len(events),
                               "window_days": WINDOW_DAYS,
                               "min_magnitude": MIN_MAGNITUDE}
    flag = False
    if len(events) < MIN_EVENTS:
        notes.append(f"insufficient events ({len(events)} < {MIN_EVENTS})")
        metrics["spatial"] = {"note": "insufficient events"}
        metrics["temporal"] = {"note": "insufficient events"}
        return {"metrics": metrics, "anomaly_flag": False,
                "notes": "; ".join(notes)}

    lons = np.array([e["lon"] for e in events])
    lats = np.array([e["lat"] for e in events])
    ce = clark_evans(lons, lats)
    metrics["spatial"] = ce
    spatial_flag = ce["z"] > CE_Z_THRESHOLD
    if spatial_flag:
        notes.append(f"spatial regularity: Clark-Evans R={ce['R']}, "
                     f"z={ce['z']} (p<0.001, regular direction)")
        flag = True
    else:
        notes.append(f"spatial: R={ce['R']} "
                     f"({'clustered' if ce['R'] < 1 else 'random-ish'})")

    times = np.array(sorted(e["t"] for e in events))
    t1 = now.timestamp()
    t0 = t1 - WINDOW_DAYS * 86400.0
    ls = lomb_scargle_screen(times, t0, t1)
    metrics["temporal"] = ls
    if ls.get("flag"):
        notes.append(f"temporal periodicity: P={ls['best_period_h']} h, "
                     f"FAP={ls['fap']:.2e}")
        flag = True
    elif ls.get("available"):
        notes.append(f"temporal: no significant periodicity "
                     f"(best P={ls.get('best_period_h')} h, "
                     f"FAP={ls.get('fap', 1.0):.2f})")
    else:
        notes.append("temporal: " + str(ls.get("note")))
    return {"metrics": metrics, "anomaly_flag": flag,
            "notes": "; ".join(notes)}


def collect(now: Optional[_dt.datetime] = None,
            timeout: float = 25.0) -> ChannelReading:
    """Collect one geometric-structure reading (network + fixture)."""
    now = now or _dt.datetime.now(_dt.timezone.utc)
    offline = False
    note = ""
    try:
        events = _fetch_events(now, timeout)
    except Exception as exc:
        events = _load_fixture()
        offline = True
        note = f"offline fixture used ({type(exc).__name__}: {exc})"
    out = _analyze(events, now)
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
