"""OMEGA-BEACON quick-reference tier rules, encoded as data + evaluator.

Tier semantics (pre-registered):

* Tier 1 -- a single qualifying detection flags the survey.
* Tier 2 -- requires corroboration: hits on >= 2 independent channels.
* Tier 3 -- indicative only; hits stack but never constitute a flag alone.

Every verdict ends with the calibration disclaimer: anomaly scores are
statistical statements, not evidence of origin or intent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Sequence

from . import CALIBRATION_DISCLAIMER
from .beacon import DetectionResult
from .channels import ChannelReading

NULL_FREQS_HZ = (175.0, 280.0, 420.0, 630.0, 840.0)
RNG_P_THRESHOLD = 0.001
BEACON_CORROBORATION_CONFIDENCE = 0.9


@dataclass(frozen=True)
class TierRule:
    """One pre-registered detection rule."""
    rule_id: str
    tier: int
    description: str
    manual: bool  # True -> requires human attestation, never auto-derived


TIER_RULES: tuple[TierRule, ...] = (
    # --- Tier 1: single detection flags -----------------------------------
    TierRule("t1_null_spike", 1,
             "EM/acoustic spike >6 dB at null frequency "
             "(175/280/420/630/840 Hz ...)", manual=False),
    TierRule("t1_gamma_40hz", 1,
             "40 Hz gamma-band power >3 sigma within 5 min of beacon TX",
             manual=False),
    TierRule("t1_rng_chi2", 1,
             "RNG chi-square uniformity p < 0.001", manual=False),
    TierRule("t1_prediction_validated", 1,
             "hash-chain-committed prediction later validated",
             manual=True),
    TierRule("t1_timestamp_anomaly", 1,
             "recording-timestamp anomaly", manual=True),
    # --- Tier 2: needs >= 2 independent channels ---------------------------
    TierRule("t2_harmonic_return", 2,
             "harmonic return 280-5740 Hz, phase-stable", manual=False),
    TierRule("t2_coherent_13hz", 2,
             "coherent 13.00 Hz tone, drift <0.1 Hz over >10 s",
             manual=False),
    TierRule("t2_rng", 2,
             "RNG chi-square uniformity p < 0.001", manual=False),
    TierRule("t2_info_leak", 2, "verified information leak", manual=True),
    # --- Tier 3: indicative only, stacks -----------------------------------
    TierRule("t3_somatic", 3,
             "subjective somatic event with timestamp", manual=True),
    TierRule("t3_thermal", 3, "thermal variance", manual=True),
    TierRule("t3_vibration", 3,
             "structured vibration at 70/140 Hz", manual=False),
)

_RULES_BY_ID = {r.rule_id: r for r in TIER_RULES}

# Manual attestations may name rule ids directly or use these aliases.
ATTESTATION_ALIASES = {
    "prediction_validated": "t1_prediction_validated",
    "timestamp_anomaly": "t1_timestamp_anomaly",
    "info_leak_verified": "t2_info_leak",
    "somatic_event": "t3_somatic",
    "thermal_variance": "t3_thermal",
    "vibration_70_140": "t3_vibration",
}


@dataclass
class TierVerdict:
    """Outcome of the tier evaluation."""
    tier1_hits: list[str] = field(default_factory=list)
    tier2_hits: list[str] = field(default_factory=list)
    tier2_channels: list[str] = field(default_factory=list)
    tier2_corroborated: bool = False
    tier3_hits: list[str] = field(default_factory=list)
    summary: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "tier1_hits": self.tier1_hits,
            "tier2_hits": self.tier2_hits,
            "tier2_channels": self.tier2_channels,
            "tier2_corroborated": self.tier2_corroborated,
            "tier3_hits": self.tier3_hits,
            "summary": self.summary,
        }


# --------------------------------------------------------------------------
# Automatic rule checks over channel readings. Each returns True on a hit.
# --------------------------------------------------------------------------

def _any_metric(readings: Sequence[ChannelReading],
                pred: Callable[[ChannelReading], bool]) -> bool:
    return any(pred(r) for r in readings)


def _check_t1_null_spike(rs: Sequence[ChannelReading]) -> bool:
    return _any_metric(rs, lambda r: r.channel == "audio_spectrum"
                       and bool(r.metrics.get("spikes")))


def _check_t1_gamma(rs: Sequence[ChannelReading]) -> bool:
    # requires a pre-registered baseline sigma in the reading's metrics
    return _any_metric(rs, lambda r: r.channel == "audio_spectrum"
                       and float(r.metrics.get("gamma_sigma_excess", 0.0)) > 3.0)


def _rng_p_below(rs: Sequence[ChannelReading]) -> bool:
    for r in rs:
        if r.channel != "rng_entropy":
            continue
        for src in r.metrics.values():
            if isinstance(src, dict) and \
                    float(src.get("chi2_p", 1.0)) < RNG_P_THRESHOLD:
                return True
    return False


def _check_t2_coherent(rs: Sequence[ChannelReading]) -> bool:
    return _any_metric(rs, lambda r: r.channel == "audio_spectrum"
                       and bool(r.metrics.get("coherent_13hz", {})
                                .get("detected")))


def _check_t3_vibration(rs: Sequence[ChannelReading]) -> bool:
    return _any_metric(rs, lambda r: bool(r.metrics.get("vibration_70_140")))


# rule_id -> (check function, contributing channel label for corroboration)
_AUTO_CHECKS: dict[str, tuple[Callable[[Sequence[ChannelReading]], bool], str]] = {
    "t1_null_spike": (_check_t1_null_spike, "audio_spectrum"),
    "t1_gamma_40hz": (_check_t1_gamma, "audio_spectrum"),
    "t1_rng_chi2": (_rng_p_below, "rng_entropy"),
    "t2_coherent_13hz": (_check_t2_coherent, "audio_spectrum"),
    "t2_rng": (_rng_p_below, "rng_entropy"),
    "t3_vibration": (_check_t3_vibration, "vibration_sensor"),
}


def evaluate_tiers(readings: Sequence[ChannelReading],
                   detection: Optional[DetectionResult] = None,
                   manual_attestations: Sequence[str] = ()) -> TierVerdict:
    """Evaluate all tier rules against readings, detection, and attestations.

    ``manual_attestations`` are strings: either rule ids or aliases (see
    :data:`ATTESTATION_ALIASES`). Attestations are trusted as given; auditing
    them is a human responsibility, and the summary says so.
    """
    hits: dict[str, str] = {}  # rule_id -> contributing channel label

    for rule_id, (check, channel) in _AUTO_CHECKS.items():
        try:
            if check(readings):
                hits[rule_id] = channel
        except Exception:
            continue  # a malformed reading never fabricates a hit

    # a high-confidence lattice detection corroborates the harmonic-return
    # rule: the lattice carriers (280-2870 Hz) sit inside 280-5740 Hz and the
    # template match implies phase stability over the 13 s window
    if detection is not None and \
            detection.confidence >= BEACON_CORROBORATION_CONFIDENCE:
        hits["t2_harmonic_return"] = "beacon_detector"

    for att in manual_attestations:
        rule_id = ATTESTATION_ALIASES.get(att, att)
        rule = _RULES_BY_ID.get(rule_id)
        if rule is not None and rule.manual:
            hits[rule_id] = "manual_attestation"

    v = TierVerdict()
    for rule_id, channel in sorted(hits.items()):
        rule = _RULES_BY_ID[rule_id]
        if rule.tier == 1:
            v.tier1_hits.append(rule_id)
        elif rule.tier == 2:
            v.tier2_hits.append(rule_id)
            if channel not in v.tier2_channels:
                v.tier2_channels.append(channel)
        else:
            v.tier3_hits.append(rule_id)
    v.tier2_corroborated = len(v.tier2_channels) >= 2

    lines = ["OMEGA-BEACON tier evaluation"]
    if v.tier1_hits:
        lines.append(f"Tier 1 FLAG ({len(v.tier1_hits)} rule(s)): "
                     + ", ".join(v.tier1_hits))
    else:
        lines.append("Tier 1: no single-detection flags")
    if v.tier2_corroborated:
        lines.append(f"Tier 2 CORROBORATED across channels "
                     f"{v.tier2_channels}: " + ", ".join(v.tier2_hits))
    elif v.tier2_hits:
        lines.append(f"Tier 2: uncorroborated hit(s) "
                     f"({', '.join(v.tier2_hits)}) -- needs >= 2 independent "
                     "channels")
    else:
        lines.append("Tier 2: no hits")
    if v.tier3_hits:
        lines.append(f"Tier 3 (indicative only, stack={len(v.tier3_hits)}): "
                     + ", ".join(v.tier3_hits))
    else:
        lines.append("Tier 3: no indications")
    lines.append(CALIBRATION_DISCLAIMER)
    v.summary = "\n".join(lines)
    return v
