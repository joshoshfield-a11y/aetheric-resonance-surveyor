"""RNG entropy screening channel.

Draws N bytes from ``os.urandom`` (and optionally ``/dev/hwrng`` when the
host exposes one) and runs a chi-square uniformity test over 256 bins plus a
lag-1 serial-correlation test.

HONESTY NOTE: os.urandom is a CSPRNG; it will pass these tests essentially
always, and a failure would far more likely indicate an implementation or
sampling bug than any external influence. This channel exists as a
pre-registered screening instrument under the project's Tier-2 criterion
(p < 0.001), not as a detector of anything specific.
"""

from __future__ import annotations

import math
import os

from . import ChannelReading, utc_now_iso

CHANNEL = "rng_entropy"
DEFAULT_N_BYTES = 100_000
CHI2_P_THRESHOLD = 0.001  # pre-registered Tier-2 criterion
SERIAL_P_THRESHOLD = 0.001
HWRNG_PATH = "/dev/hwrng"


def _chi2_uniform(data: bytes) -> tuple[float, float]:
    """Chi-square statistic and p-value for uniformity over 256 byte bins.

    p-value uses the Wilson-Hilferty normal approximation for 255 dof;
    accurate to ~1e-3 near the tails, sufficient for a screening threshold.
    """
    counts = [0] * 256
    for b in data:
        counts[b] += 1
    n = len(data)
    expected = n / 256.0
    chi2 = sum((c - expected) ** 2 / expected for c in counts)
    dof = 255
    # Wilson-Hilferty transform
    z = ((chi2 / dof) ** (1.0 / 3.0) - (1.0 - 2.0 / (9.0 * dof))) \
        / math.sqrt(2.0 / (9.0 * dof))
    p = 0.5 * math.erfc(z / math.sqrt(2.0))
    return chi2, p


def _serial_correlation(data: bytes) -> tuple[float, float]:
    """Lag-1 serial correlation coefficient of the byte stream and its
    approximate p-value (r is ~N(0, 1/sqrt(n-1)) under the null)."""
    n = len(data)
    if n < 3:
        return 0.0, 1.0
    xs = data[:-1]
    ys = data[1:]
    mx = sum(xs) / (n - 1)
    my = sum(ys) / (n - 1)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx == 0 or dy == 0:
        return 0.0, 1.0
    r = num / (dx * dy)
    z = r * math.sqrt(n - 2)
    p = math.erfc(abs(z) / math.sqrt(2.0))
    return r, p


def _read_hwrng(n: int) -> bytes | None:
    """Bytes from /dev/hwrng if present and readable, else None."""
    try:
        if os.path.exists(HWRNG_PATH) and os.access(HWRNG_PATH, os.R_OK):
            with open(HWRNG_PATH, "rb", buffering=0) as fh:
                return fh.read(n)
    except Exception:
        return None
    return None


def collect(n_bytes: int = DEFAULT_N_BYTES) -> ChannelReading:
    """Collect one entropy-screening reading. Never requires network."""
    sources: dict[str, dict[str, float]] = {}
    flag = False
    notes = []

    os_bytes = os.urandom(n_bytes)
    chi2, p = _chi2_uniform(os_bytes)
    r, p_ser = _serial_correlation(os_bytes)
    sources["os.urandom"] = {
        "n_bytes": n_bytes,
        "chi2": round(chi2, 3),
        "chi2_p": p,
        "serial_r": r,
        "serial_p": p_ser,
    }
    if p < CHI2_P_THRESHOLD or p_ser < SERIAL_P_THRESHOLD:
        flag = True
        notes.append("os.urandom screening statistic below threshold")

    hw = _read_hwrng(min(n_bytes, 65_536))
    if hw:
        chi2_h, p_h = _chi2_uniform(hw)
        r_h, p_hser = _serial_correlation(hw)
        sources["/dev/hwrng"] = {
            "n_bytes": len(hw),
            "chi2": round(chi2_h, 3),
            "chi2_p": p_h,
            "serial_r": r_h,
            "serial_p": p_hser,
        }
        if p_h < CHI2_P_THRESHOLD or p_hser < SERIAL_P_THRESHOLD:
            flag = True
            notes.append("hwrng screening statistic below threshold")

    return ChannelReading(
        timestamp_utc=utc_now_iso(),
        channel=CHANNEL,
        metrics=sources,
        anomaly_flag=flag,
        notes="; ".join(notes) if notes else
        "screening statistics within expected range (a pass says nothing "
        "about anomalous influence; see module docstring)",
        offline=False,
    )
