"""alpha142 -- the ARS "logic compiler".

Correlates observed 13-vectors against the autopsy reference, scores
13-fold rotational symmetry of time series, and merges channel readings and
beacon detections into a single tiered anomaly report.

Everything here is descriptive statistics over pre-registered templates.
A high score means "matches the template", never "establishes a cause".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

import numpy as np

from .beacon import (DetectionResult, REFERENCE_AMPLITUDES, detect_lattice,
                     pearsonr)
from .channels import ChannelReading
from .tiers import TierVerdict, evaluate_tiers


@dataclass
class SymmetryResult:
    """13-fold rotational symmetry score of a time series' autocorrelation."""
    order: int
    score: float            # 0..1, fraction of autocorrelation energy at
                            # lags that are multiples of the 1/13 period bin
    method: str
    notes: str = ""


@dataclass
class CompositeReport:
    """Merged verdict across channels, beacon detection, and tier rules."""
    amplitude_correlation_r: Optional[float] = None
    amplitude_correlation_p: Optional[float] = None
    beacon_confidence: Optional[float] = None
    symmetry: Optional[SymmetryResult] = None
    channel_flags: dict[str, bool] = field(default_factory=dict)
    tier_verdict: Optional[TierVerdict] = None
    summary: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "amplitude_correlation_r": self.amplitude_correlation_r,
            "amplitude_correlation_p": self.amplitude_correlation_p,
            "beacon_confidence": self.beacon_confidence,
            "symmetry": None if self.symmetry is None else {
                "order": self.symmetry.order,
                "score": self.symmetry.score,
                "method": self.symmetry.method,
            },
            "channel_flags": self.channel_flags,
            "tier_verdict": None if self.tier_verdict is None else
            self.tier_verdict.as_dict(),
            "summary": self.summary,
        }


def correlate_amplitude_vector(
        observed_13_vector: Sequence[float],
        reference: Sequence[float] = REFERENCE_AMPLITUDES
) -> tuple[float, float]:
    """Pearson (r, p) of a 13-element observed amplitude vector against the
    autopsy reference vector."""
    obs = list(observed_13_vector)
    if len(obs) != 13:
        raise ValueError("observed vector must have exactly 13 entries")
    return pearsonr(obs, list(reference))


def thirteen_fold_symmetry(timeseries: Sequence[float],
                           max_lag: Optional[int] = None) -> SymmetryResult:
    """Rotational symmetry score at order 13 of the autocorrelation.

    Method (documented honestly): compute the biased autocorrelation of the
    mean-centered series out to ``max_lag`` lags. The score is the fraction
    of total autocorrelation energy (lag >= 1) carried at lags that are
    integer multiples of L/13, where L = max_lag is the analysis horizon --
    i.e. how much of the series' temporal structure repeats with 13-fold
    spacing. White noise scores near 1/13; a waveform built on a 13-beat
    period scores near 1.
    """
    x = np.asarray(timeseries, dtype=np.float64)
    x = x - x.mean()
    n = x.size
    if n < 26:
        return SymmetryResult(order=13, score=0.0,
                              method="order-13 autocorrelation energy fraction",
                              notes="series too short (<26 samples)")
    L = max_lag or min(n - 1, 13 * 64)
    # FFT-based biased autocorrelation: O(n log n), safe for long recordings
    nfft = 1 << (n + L).bit_length()
    spec = np.fft.rfft(x, nfft)
    ac = np.fft.irfft(spec * np.conj(spec), nfft)[:L + 1]
    ac = ac / (ac[0] + 1e-15)
    energy = ac[1:] ** 2
    step = max(1, L // 13)
    on_nodes = energy[step - 1::step]
    total = float(energy.sum()) + 1e-15
    score = float(on_nodes.sum() / total)
    return SymmetryResult(
        order=13,
        score=min(1.0, score),
        method="order-13 autocorrelation energy fraction",
        notes=f"lags 1..{L}, node spacing {step} sample(s)",
    )


def evaluate(event_stream: Sequence[ChannelReading],
             beacon_samples: Optional[np.ndarray] = None,
             sr: int = 44100,
             manual_attestations: Optional[Sequence[str]] = None
             ) -> CompositeReport:
    """Merge channel readings (+ optional beacon audio) into one verdict.

    ``manual_attestations`` are strings naming pre-registered manual events
    (e.g. "prediction_validated"); they are treated as attested facts by the
    tier evaluator, which remains the caller's responsibility to audit.
    """
    report = CompositeReport()
    readings = list(event_stream)
    report.channel_flags = {r.channel: r.anomaly_flag for r in readings}

    detection: Optional[DetectionResult] = None
    if beacon_samples is not None:
        detection = detect_lattice(beacon_samples, sr)
        report.beacon_confidence = detection.confidence
        r, p = correlate_amplitude_vector(detection.node_amplitudes)
        report.amplitude_correlation_r = r
        report.amplitude_correlation_p = p
        report.symmetry = thirteen_fold_symmetry(beacon_samples)

    report.tier_verdict = evaluate_tiers(
        readings,
        detection=detection,
        manual_attestations=manual_attestations or [],
    )
    report.summary = report.tier_verdict.summary
    return report
