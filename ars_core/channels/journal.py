"""Append-only JSONL event journal with a SHA-256 hash chain.

Each entry commits to the previous entry's hash, its own UTC timestamp, and
its content: ``sha256(prev_hash | timestamp | content)``. This makes the
journal a pre-commitment device -- a prediction, dream log, or somatic event
written down *before* an outcome cannot be retroactively edited without
breaking the chain. Verification is purely local and deterministic.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Optional

from . import ChannelReading, utc_now_iso

CHANNEL = "journal"
GENESIS_HASH = "0" * 64
DEFAULT_PATH = os.path.join(os.path.dirname(__file__), "fixtures",
                            "..", "..", "ars_journal.jsonl")
DEFAULT_PATH = os.path.abspath(DEFAULT_PATH)


def _entry_hash(prev_hash: str, timestamp: str, content: str) -> str:
    """Chain hash over prev_hash + timestamp + content (UTF-8, | separated)."""
    h = hashlib.sha256()
    h.update(prev_hash.encode("utf-8"))
    h.update(b"|")
    h.update(timestamp.encode("utf-8"))
    h.update(b"|")
    h.update(content.encode("utf-8"))
    return h.hexdigest()


def _last_hash(path: str) -> str:
    last = GENESIS_HASH
    if not os.path.exists(path):
        return last
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            last = json.loads(line)["hash"]
    return last


def add_entry(content: str, path: str = DEFAULT_PATH,
              tag: Optional[str] = None) -> dict[str, Any]:
    """Append one committed entry; returns the entry dict."""
    ts = utc_now_iso()
    prev = _last_hash(path)
    h = _entry_hash(prev, ts, content)
    entry = {"timestamp_utc": ts, "tag": tag, "content": content,
             "prev_hash": prev, "hash": h}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def verify_chain(path: str = DEFAULT_PATH) -> bool:
    """True iff every entry's hash links correctly from genesis."""
    if not os.path.exists(path):
        return True  # empty journal is trivially valid
    prev = GENESIS_HASH
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            if entry.get("prev_hash") != prev:
                return False
            expect = _entry_hash(prev, entry["timestamp_utc"],
                                 entry["content"])
            if entry.get("hash") != expect:
                return False
            prev = entry["hash"]
    return True


def read_entries(path: str = DEFAULT_PATH) -> list[dict[str, Any]]:
    """All entries in append order (empty list if the file is absent)."""
    if not os.path.exists(path):
        return []
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def collect(path: str = DEFAULT_PATH) -> ChannelReading:
    """Journal status as a channel reading (count + chain integrity)."""
    entries = read_entries(path)
    ok = verify_chain(path)
    return ChannelReading(
        timestamp_utc=utc_now_iso(),
        channel=CHANNEL,
        metrics={
            "path": path,
            "entry_count": len(entries),
            "chain_valid": ok,
            "last_hash": entries[-1]["hash"] if entries else GENESIS_HASH,
        },
        anomaly_flag=not ok,
        notes="hash chain intact" if ok else
        "HASH CHAIN BROKEN -- journal was modified after the fact",
        offline=False,
    )
