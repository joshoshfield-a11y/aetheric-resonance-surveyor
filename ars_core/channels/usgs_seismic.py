"""USGS seismicity channel.

Polls the USGS fdsnws event API for M>=2.5 events over a recent window and
compares the observed event rate against a 30-day baseline window using a
Poisson-normal z-score. A flag means "recent seismicity rate is unusually
high relative to the baseline window" -- a routine statistical statement.

Offline behaviour: if the network is unreachable (or ``requests`` is
missing), the channel loads a small bundled fixture and marks the reading
``offline=True``.
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

CHANNEL = "usgs_seismic"
FDSN_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
MIN_MAGNITUDE = 2.5
RECENT_HOURS = 24
BASELINE_DAYS = 30
Z_THRESHOLD = 3.0

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                        "usgs_seismic_fixture.json")


def _fetch_count(start_iso: str, end_iso: str, timeout: float = 10.0) -> int:
    """Number of M>=2.5 events in [start, end). Raises on any failure."""
    if requests is None:
        raise RuntimeError("requests library not available")
    resp = requests.get(FDSN_URL, params={
        "format": "geojson",
        "starttime": start_iso,
        "endtime": end_iso,
        "minmagnitude": MIN_MAGNITUDE,
    }, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    return len(data.get("features", []))


def _load_fixture() -> dict[str, Any]:
    with open(_FIXTURE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _rate_zscore(recent_count: int, recent_days: float,
                 baseline_count: int, baseline_days: float) -> float:
    """Normal approximation to the Poisson rate-ratio test.

    Under the null (equal rates), the difference of rates has variance
    lambda*(1/d1 + 1/d2) with lambda estimated from the pooled counts.
    """
    lam = (recent_count + baseline_count) / (recent_days + baseline_days)
    var = lam * (1.0 / recent_days + 1.0 / baseline_days)
    if var <= 0:
        return 0.0
    r1 = recent_count / recent_days
    r2 = baseline_count / baseline_days
    return (r1 - r2) / math.sqrt(var)


def collect(now: Optional[Any] = None, timeout: float = 10.0) -> ChannelReading:
    """Collect one seismicity reading (network with fixture fallback)."""
    import datetime as _dt

    now = now or _dt.datetime.now(_dt.timezone.utc)
    recent_start = now - _dt.timedelta(hours=RECENT_HOURS)
    baseline_start = now - _dt.timedelta(days=BASELINE_DAYS)
    fmt = "%Y-%m-%dT%H:%M:%S"
    offline = False
    note = ""
    try:
        recent = _fetch_count(recent_start.strftime(fmt), now.strftime(fmt),
                              timeout)
        baseline = _fetch_count(baseline_start.strftime(fmt),
                                now.strftime(fmt), timeout)
    except Exception as exc:  # offline fallback keeps instrument usable
        fx = _load_fixture()
        recent = int(fx["recent_count"])
        baseline = int(fx["baseline_count"])
        offline = True
        note = f"offline fixture used ({type(exc).__name__}: {exc})"

    recent_days = RECENT_HOURS / 24.0
    baseline_days = float(BASELINE_DAYS)
    r_recent = recent / recent_days
    r_base = baseline / baseline_days
    z = _rate_zscore(recent, recent_days, baseline, baseline_days)
    flag = z > Z_THRESHOLD
    if flag:
        note = (note + " " if note else "") + \
            f"event-rate z-score {z:.2f} exceeds {Z_THRESHOLD}"
    return ChannelReading(
        timestamp_utc=utc_now_iso(),
        channel=CHANNEL,
        metrics={
            "recent_count": recent,
            "recent_rate_per_day": round(r_recent, 3),
            "baseline_count": baseline,
            "baseline_rate_per_day": round(r_base, 3),
            "rate_zscore": round(z, 3),
            "min_magnitude": MIN_MAGNITUDE,
        },
        anomaly_flag=flag,
        notes=note or "within baseline",
        offline=offline,
    )
