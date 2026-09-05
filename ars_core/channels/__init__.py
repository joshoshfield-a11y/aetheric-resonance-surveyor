"""Monitoring channels.

Every channel exposes ``collect()`` returning a :class:`ChannelReading`.
All network and hardware access is guarded: on any failure the channel falls
back to a bundled offline fixture (or a documented synthetic source) so the
instrument remains testable with no network and no microphone.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from typing import Any


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string with 'Z' suffix."""
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


@dataclass
class ChannelReading:
    """One observation from one channel.

    ``anomaly_flag`` only ever means "this channel's screening statistic
    crossed its pre-registered threshold". It is not a claim about causes.
    """
    timestamp_utc: str
    channel: str
    metrics: dict[str, Any]
    anomaly_flag: bool = False
    notes: str = ""
    offline: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "timestamp_utc": self.timestamp_utc,
            "channel": self.channel,
            "metrics": self.metrics,
            "anomaly_flag": self.anomaly_flag,
            "notes": self.notes,
            "offline": self.offline,
        }
