"""Beacon lattice synthesis, detection, and duplex reply channel.

The "lattice" is a 13-node acoustic structure derived from a prior signal
autopsy:

* 13 nodes, each exactly 1.000 s long.
* Carrier of node k is 70 * p_k Hz, where p_k is the k-th prime
  (2, 3, 5, ..., 41), giving 140 Hz ... 2870 Hz.
* A global sin^2(pi * t) amplitude envelope spans the 13 s, producing 13
  lobes with zeros at integer seconds and peaks at half-integer seconds.
* A reference per-node amplitude vector was recovered from the autopsy.

Everything in this module is ordinary DSP. Detection results are statistical
scores only; a high confidence value means the input matches the lattice
template, nothing more.
"""

from __future__ import annotations

import math
import wave
from dataclasses import dataclass, field
from typing import Optional, Sequence

import numpy as np

try:  # optional dependency, guarded per project policy
    from scipy.io import wavfile as _scipy_wavfile
    from scipy import stats as _scipy_stats
except Exception:  # pragma: no cover - exercised only without scipy
    _scipy_wavfile = None
    _scipy_stats = None

# --------------------------------------------------------------------------
# Lattice constants (exact, from the autopsy specification)
# --------------------------------------------------------------------------

PRIMES_13 = (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41)
LATTICE_FREQS = tuple(70 * p for p in PRIMES_13)  # Hz
N_NODES = 13
NODE_SECONDS = 1.0
DEFAULT_SR = 44100
REFERENCE_AMPLITUDES = (
    0.73, 0.66, 0.57, 0.48, 0.31, 0.22, 0.05,
    0.02, 0.12, 0.15, 0.13, 0.007, 0.09,
)

# Ternary reply channel: three well-separated amplitude levels.
REPLY_LEVELS = (0.10, 0.45, 0.80)


# --------------------------------------------------------------------------
# Small numeric helpers (scipy-free fallbacks live here)
# --------------------------------------------------------------------------

def goertzel_power(samples: np.ndarray, sr: int, freq: float) -> float:
    """Single-bin Goertzel power of ``samples`` at ``freq`` Hz.

    Returns |X(f)|^2 normalized so that a pure tone of amplitude A and length
    N (integer number of cycles) yields roughly (A*N/2)^2.
    """
    n = int(samples.shape[0])
    if n == 0:
        return 0.0
    x = np.asarray(samples, dtype=np.float64)
    # fractional-cycle safe: use direct DFT at the exact frequency
    t = np.arange(n, dtype=np.float64) / float(sr)
    c = np.exp(-2j * np.pi * freq * t)
    return float(abs(np.dot(x, c)) ** 2)


def tone_amplitude(samples: np.ndarray, sr: int, freq: float) -> float:
    """Least-squares amplitude of a sinusoid of known frequency."""
    x = np.asarray(samples, dtype=np.float64)
    n = x.shape[0]
    if n == 0:
        return 0.0
    t = np.arange(n, dtype=np.float64) / float(sr)
    basis = np.column_stack([np.sin(2 * np.pi * freq * t),
                             np.cos(2 * np.pi * freq * t)])
    coef, *_ = np.linalg.lstsq(basis, x, rcond=None)
    return float(math.hypot(coef[0], coef[1]))


def pearsonr(a: Sequence[float], b: Sequence[float]) -> tuple[float, float]:
    """Pearson r and two-sided p-value (scipy if present, else t-statistic
    with a normal approximation adequate for n=13 screening)."""
    x = np.asarray(a, dtype=np.float64)
    y = np.asarray(b, dtype=np.float64)
    if x.size != y.size or x.size < 3:
        return 0.0, 1.0
    if _scipy_stats is not None:
        r, p = _scipy_stats.pearsonr(x, y)
        return float(r), float(p)
    x = x - x.mean()
    y = y - y.mean()
    denom = math.sqrt(float((x ** 2).sum() * (y ** 2).sum()))
    if denom == 0.0:
        return 0.0, 1.0
    r = float((x * y).sum() / denom)
    r = max(-1.0, min(1.0, r))
    # t statistic with n-2 dof; normal approx for the p-value (documented
    # approximation, only used when scipy is absent).
    n = x.size
    if abs(r) >= 1.0:
        return r, 0.0
    t = r * math.sqrt((n - 2) / (1.0 - r * r))
    p = math.erfc(abs(t) / math.sqrt(2.0))
    return r, p


def envelope_sin2(t: np.ndarray) -> np.ndarray:
    """The global sin^2(pi t) envelope."""
    return np.sin(np.pi * t) ** 2


# --------------------------------------------------------------------------
# Synthesis
# --------------------------------------------------------------------------

def synthesize_lattice(amplitude_vector: Optional[Sequence[float]] = None,
                       sr: int = DEFAULT_SR) -> np.ndarray:
    """Synthesize the 13 s beacon lattice.

    Parameters
    ----------
    amplitude_vector:
        Per-node peak amplitudes (13 values). Defaults to the autopsy
        reference vector.
    sr:
        Sample rate in Hz.

    Returns
    -------
    np.ndarray
        float32 mono samples, length exactly ``13 * sr``.
    """
    amps = np.asarray(
        REFERENCE_AMPLITUDES if amplitude_vector is None else amplitude_vector,
        dtype=np.float64,
    )
    if amps.size != N_NODES:
        raise ValueError("amplitude_vector must have exactly 13 entries")
    total = int(round(N_NODES * NODE_SECONDS * sr))
    t = np.arange(total, dtype=np.float64) / float(sr)
    env = envelope_sin2(t)
    out = np.zeros(total, dtype=np.float64)
    node_len = int(round(NODE_SECONDS * sr))
    for k in range(N_NODES):
        seg = slice(k * node_len, (k + 1) * node_len)
        out[seg] = amps[k] * np.sin(2.0 * np.pi * LATTICE_FREQS[k] * t[seg])
    out *= env
    peak = np.max(np.abs(out))
    if peak > 1.0:  # keep the waveform within full scale
        out /= peak
    return out.astype(np.float32)


# --------------------------------------------------------------------------
# WAV I/O
# --------------------------------------------------------------------------

def save_wav(path: str, samples: np.ndarray, sr: int = DEFAULT_SR) -> None:
    """Write mono float/int samples to a 16-bit PCM WAV file."""
    x = np.asarray(samples)
    if x.dtype != np.int16:
        x = np.clip(x.astype(np.float64), -1.0, 1.0)
        x = (x * 32767.0).astype(np.int16)
    if _scipy_wavfile is not None:
        _scipy_wavfile.write(path, int(sr), x)
        return
    with wave.open(path, "wb") as wf:  # stdlib fallback
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sr))
        wf.writeframes(x.tobytes())


def load_wav(path: str) -> tuple[np.ndarray, int]:
    """Read a WAV file; returns (float64 mono in [-1, 1], sample rate)."""
    if _scipy_wavfile is not None:
        sr, data = _scipy_wavfile.read(path)
    else:
        with wave.open(path, "rb") as wf:
            sr = wf.getframerate()
            raw = wf.readframes(wf.getnframes())
            data = np.frombuffer(raw, dtype=np.int16)
    data = np.asarray(data)
    if data.ndim > 1:  # downmix to mono
        data = data.mean(axis=1)
    if data.dtype == np.int16:
        data = data.astype(np.float64) / 32768.0
    elif data.dtype == np.int32:
        data = data.astype(np.float64) / 2147483648.0
    else:
        data = data.astype(np.float64)
    return data, int(sr)


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

@dataclass
class DetectionResult:
    """Structured, deliberately non-sensational detection report.

    ``confidence`` is a template-match score in [0, 1]: how closely the input
    resembles the 13-node lattice. It is NOT evidence about the origin of the
    signal.
    """
    sample_rate: int
    duration_s: float
    node_freqs_hz: list[float]
    node_powers: list[float]
    node_amplitudes: list[float]
    amplitude_correlation_r: float
    amplitude_correlation_p: float
    envelope_correlation: float
    snr_db: float
    confidence: float
    notes: str = ""
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "sample_rate": self.sample_rate,
            "duration_s": self.duration_s,
            "node_freqs_hz": self.node_freqs_hz,
            "node_powers": self.node_powers,
            "node_amplitudes": self.node_amplitudes,
            "amplitude_correlation_r": self.amplitude_correlation_r,
            "amplitude_correlation_p": self.amplitude_correlation_p,
            "envelope_correlation": self.envelope_correlation,
            "snr_db": self.snr_db,
            "confidence": self.confidence,
            "notes": self.notes,
        }


def _node_windows(samples: np.ndarray, sr: int) -> list[np.ndarray]:
    """Split into 1 s windows aligned to envelope peaks.

    The envelope peaks at half-integer seconds, so window k is
    [k*sr, (k+1)*sr): it brackets the peak at k + 0.5 s.
    """
    node_len = int(round(NODE_SECONDS * sr))
    windows = []
    for k in range(N_NODES):
        seg = samples[k * node_len:(k + 1) * node_len]
        if seg.shape[0] < node_len:
            seg = np.pad(seg, (0, node_len - seg.shape[0]))
        windows.append(seg.astype(np.float64))
    return windows


def detect_lattice(samples: np.ndarray, sr: int = DEFAULT_SR,
                   amplitude_vector: Optional[Sequence[float]] = None
                   ) -> DetectionResult:
    """Score how well ``samples`` match the 13-node lattice template.

    Returns a :class:`DetectionResult` whose ``confidence`` in [0, 1] combines:

    * per-node Goertzel power at the 13 lattice carriers (presence of nodes),
    * Pearson correlation of the recovered per-node amplitude vector against
      the reference vector,
    * correlation of the short-time energy envelope against sin^2(pi t).

    A high score only asserts template similarity.
    """
    x = np.asarray(samples, dtype=np.float64)
    if x.ndim > 1:
        x = x.mean(axis=1)
    duration = x.shape[0] / float(sr)
    ref = np.asarray(
        REFERENCE_AMPLITUDES if amplitude_vector is None else amplitude_vector,
        dtype=np.float64)

    windows = _node_windows(x, sr)

    powers, amps = [], []
    for k, w in enumerate(windows):
        p = goertzel_power(w, sr, LATTICE_FREQS[k])
        powers.append(p)
        # amplitude of the carrier inside the sin^2(pi t) lobe: mean of the
        # envelope over one node is 1/2, so the raw LS amplitude is halved.
        amps.append(2.0 * tone_amplitude(w, sr, LATTICE_FREQS[k]))
    amps_arr = np.asarray(amps)

    # --- amplitude-vector correlation -------------------------------------
    r_amp, p_amp = pearsonr(amps_arr, ref)

    # --- envelope correlation ---------------------------------------------
    # short-time energy at ~50 ms resolution vs the expected envelope, which
    # is sin^4(pi t) (energy ~ amplitude^2) scaled by the recovered per-node
    # amplitude squared. Comparing against the bare sin^4 shape would unfairly
    # penalize a correct lattice whose nodes have unequal amplitudes.
    hop = max(1, int(0.050 * sr))
    nframes = max(1, x.shape[0] // hop)
    energy = np.array([
        float(np.mean(x[i * hop:(i + 1) * hop] ** 2)) for i in range(nframes)
    ])
    te = (np.arange(nframes) + 0.5) * hop / float(sr)
    node_idx = np.clip((te / NODE_SECONDS).astype(int), 0, N_NODES - 1)
    node_gain = np.clip(amps_arr, 0.0, None) ** 2
    env_ref = envelope_sin2(te) ** 2 * node_gain[node_idx]
    if energy.std() > 0 and env_ref.std() > 0:
        r_env = float(np.corrcoef(energy, env_ref)[0, 1])
    else:
        r_env = 0.0

    # --- SNR: fitted lattice power vs residual after removing it ----------
    reconstructed = synthesize_lattice(amplitude_vector=np.clip(amps_arr, 0, None),
                                       sr=sr)[:x.shape[0]].astype(np.float64)
    resid = x - reconstructed
    p_sig = float(np.mean(reconstructed ** 2)) + 1e-15
    p_res = float(np.mean(resid ** 2)) + 1e-15
    snr_db = max(0.0, 10.0 * math.log10(p_sig / p_res))

    # --- node presence: fraction of nodes whose power stands out ----------
    med = float(np.median(powers)) + 1e-30
    presence = float(np.mean([p > 4.0 * med for p in powers])) if med > 0 else 0.0

    # --- composite confidence ---------------------------------------------
    c_amp = max(0.0, min(1.0, (r_amp + 1.0) / 2.0))
    # gate on statistical significance so chance correlations on noise
    # contribute little
    if p_amp >= 0.05:
        c_amp *= 0.25
    c_env = max(0.0, min(1.0, (r_env + 1.0) / 2.0))
    c_snr = max(0.0, min(1.0, snr_db / 12.0))
    confidence = float(0.45 * c_amp + 0.30 * c_env + 0.15 * c_snr + 0.10 * presence)
    confidence = max(0.0, min(1.0, confidence))

    notes = (
        "Template-match score only. A high confidence asserts waveform "
        "similarity to the 13-node lattice, not any particular origin."
    )
    return DetectionResult(
        sample_rate=int(sr),
        duration_s=duration,
        node_freqs_hz=[float(f) for f in LATTICE_FREQS],
        node_powers=[float(p) for p in powers],
        node_amplitudes=[float(a) for a in amps_arr],
        amplitude_correlation_r=float(r_amp),
        amplitude_correlation_p=float(p_amp),
        envelope_correlation=float(r_env),
        snr_db=float(snr_db),
        confidence=confidence,
        notes=notes,
    )


# --------------------------------------------------------------------------
# Duplex reply channel (ternary amplitude keying on the same lattice)
# --------------------------------------------------------------------------

def encode_reply(symbol_seq: Sequence[int], sr: int = DEFAULT_SR) -> np.ndarray:
    """Encode a 13-trit message as a lattice with ternary amplitudes.

    Each symbol (0, 1, or 2) selects one of :data:`REPLY_LEVELS` as the peak
    amplitude of the corresponding node, allowing two ARS instances to
    exchange 13-trit (3^13 ~ 1.6 M states) messages acoustically.
    """
    syms = list(symbol_seq)
    if len(syms) != N_NODES:
        raise ValueError("reply message must contain exactly 13 trits")
    for s in syms:
        if s not in (0, 1, 2):
            raise ValueError("symbols must be 0, 1 or 2")
    amps = [REPLY_LEVELS[s] for s in syms]
    return synthesize_lattice(amplitude_vector=amps, sr=sr)


def decode_reply(samples: np.ndarray, sr: int = DEFAULT_SR) -> list[int]:
    """Invert :func:`encode_reply`.

    Per node, the carrier amplitude is estimated and assigned to the nearest
    of the three ternary levels (maximum-likelihood under equal priors).
    """
    windows = _node_windows(np.asarray(samples, dtype=np.float64), sr)
    out = []
    for k, w in enumerate(windows):
        a = 2.0 * tone_amplitude(w, sr, LATTICE_FREQS[k])
        d = [abs(a - lvl) for lvl in REPLY_LEVELS]
        out.append(int(np.argmin(d)))
    return out


def decode_reply_checked(
    samples: np.ndarray,
    sr: int = DEFAULT_SR,
    min_snr_db: float = 6.0,
) -> tuple[Optional[list[int]], float]:
    """Like :func:`decode_reply`, but with a noise-floor refusal gate.

    The noise floor is estimated from the envelope nulls: the sin^2(pi t)
    envelope is exactly zero at every integer-second boundary, so 100 ms
    slices centered on the boundaries sample whatever else is present
    (room noise, interference). If the loudest node window is less than
    ``min_snr_db`` above that floor, decoding is refused — classifying
    silence would fabricate trits.

    Returns ``(trits_or_None, snr_db)``.
    """
    x = np.asarray(samples, dtype=np.float64)
    windows = _node_windows(x, sr)
    amps = [2.0 * tone_amplitude(w, sr, LATTICE_FREQS[k])
            for k, w in enumerate(windows)]
    edge = max(1, int(0.05 * sr))
    floor_segs = []
    for b in range(1, len(windows)):
        c = b * sr
        seg = x[max(0, c - edge): c + edge]
        if len(seg):
            floor_segs.append(float(np.sqrt(np.mean(seg ** 2))))
    noise = float(np.median(floor_segs)) if floor_segs else 0.0
    sig = max(amps) if amps else 0.0
    snr_db = 20.0 * math.log10(max(sig, 1e-12) / max(noise, 1e-12))
    if snr_db < min_snr_db:
        return None, snr_db
    out = [int(np.argmin([abs(a - lvl) for lvl in REPLY_LEVELS])) for a in amps]
    return out, snr_db
