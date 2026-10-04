"""Bitcoin substrate channel (digital-domain pulse).

Polls the free, keyless mempool.space API for the state of the Bitcoin
network: chain tip height, recommended fee rates, mempool backlog, and
3-day average network hash rate. Treated as a pulse reading of the
global distributed-computation substrate (per the Heimdall sensor plan),
nothing more.

Flags on fee-market congestion: fastest fee >= 50 sat/vB or a mempool
backlog > 150,000 transactions -- both rare, both verifiable on-chain
conditions. Hash rate is reported without a flag (it is noisy and
self-normalizing).

Offline behaviour: on any failure the channel loads a small bundled
fixture (recorded 2026-10-04) and marks the reading ``offline=True``.
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

CHANNEL = "bitcoin_substrate"
BASE_URL = "https://mempool.space/api"
FEE_CONGESTION_SATVB = 50
MEMPOOL_CONGESTION_TXS = 150_000

_FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures",
                        "bitcoin_substrate_fixture.json")


def _get(path: str, timeout: float) -> Any:
    if requests is None:
        raise RuntimeError("requests library not available")
    resp = requests.get(BASE_URL + path, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _load_fixture() -> dict[str, Any]:
    with open(_FIXTURE, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _analyze(height: int, fees: dict[str, Any], mempool: dict[str, Any],
             hashrate: dict[str, Any]) -> dict[str, Any]:
    fastest = int(fees.get("fastestFee", 0) or 0)
    tx_count = int(mempool.get("count", 0) or 0)
    vsize_mb = float(mempool.get("vsize", 0) or 0) / 1e6
    cur_hr_ehs = float(hashrate.get("currentHashrate", 0) or 0) / 1e18
    hr_series = hashrate.get("hashrates") or []
    hr_3d_ehs = (sum(float(p.get("avgHashrate", 0) or 0)
                     for p in hr_series) / max(1, len(hr_series)) / 1e18
                 if hr_series else 0.0)

    congested_fee = fastest >= FEE_CONGESTION_SATVB
    congested_mempool = tx_count > MEMPOOL_CONGESTION_TXS
    flag = congested_fee or congested_mempool
    notes_parts = []
    if congested_fee:
        notes_parts.append(f"fee congestion: fastest {fastest} sat/vB")
    if congested_mempool:
        notes_parts.append(f"mempool backlog: {tx_count} txs")
    return {
        "metrics": {
            "tip_height": height,
            "fastest_fee_satvb": fastest,
            "halfhour_fee_satvb": int(fees.get("halfHourFee", 0) or 0),
            "hour_fee_satvb": int(fees.get("hourFee", 0) or 0),
            "mempool_tx_count": tx_count,
            "mempool_vsize_mb": round(vsize_mb, 2),
            "hashrate_current_ehs": round(cur_hr_ehs, 1),
            "hashrate_3d_avg_ehs": round(hr_3d_ehs, 1),
            "fee_congestion_threshold_satvb": FEE_CONGESTION_SATVB,
            "mempool_congestion_threshold_txs": MEMPOOL_CONGESTION_TXS,
        },
        "anomaly_flag": flag,
        "notes": "; ".join(notes_parts) if notes_parts
                 else f"quiet: fees {fastest} sat/vB, {tx_count} txs in mempool",
    }


def collect(timeout: float = 15.0) -> ChannelReading:
    """Collect one Bitcoin-substrate reading (network with fixture)."""
    offline = False
    note = ""
    try:
        height = int(_get("/blocks/tip/height", timeout))
        fees = _get("/v1/fees/recommended", timeout)
        mempool = _get("/mempool", timeout)
        hashrate = _get("/v1/mining/hashrate/3d", timeout)
    except Exception as exc:
        fx = _load_fixture()
        height = int(fx["tip_height"])
        fees = fx["fees_recommended"]
        mempool = fx["mempool"]
        hashrate = fx["hashrate_3d"]
        offline = True
        note = f"offline fixture used ({type(exc).__name__}: {exc})"

    out = _analyze(height, fees, mempool, hashrate)
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
