"""Onset-detection backends for :class:`~constellation.audio.beat.BeatDetector`.

Pure-numpy, hardware-free, fully unit-testable. The detector owns energy
tiering / noise floor / beat strength; an onset backend only answers the
narrower question "did a beat start in this block, and what is the tempo?".

``MultibandSpectralFluxDetector`` splits each block into bass / mid / high
bands, measures half-wave-rectified spectral flux per band against a rolling
adaptive threshold, and locks tempo to the bass (kick) band. It is far more
robust across genres than wideband RMS and runs comfortably on the Switch's
Tegra (one ``rfft`` per ~46 ms block).
"""

from __future__ import annotations

import collections
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

import numpy as np


@dataclass
class OnsetResult:
    """One block's worth of onset-backend output."""

    beat: bool = False
    bpm: float = 0.0
    band_flux: dict[str, float] = field(default_factory=dict)


@runtime_checkable
class OnsetBackend(Protocol):
    """Minimal interface a pluggable onset backend must satisfy."""

    def process_block(self, block: np.ndarray) -> OnsetResult:
        """Consume one audio block, return whether a beat started."""
        ...

    def reset(self) -> None:
        """Clear all rolling state (history, tempo estimate, buffers)."""
        ...


_BANDS = ("bass", "mid", "high")


class MultibandSpectralFluxDetector:
    """Multiband spectral-flux onset detector with kick-locked tempo.

    Per block: window the most recent ``fft_size`` samples, take an rfft,
    and sum the magnitude spectrum into bass / mid / high bands. Spectral
    flux per band is the half-wave-rectified increase over the previous
    block's per-bin magnitude. A band fires an onset when its flux exceeds
    an adaptive threshold ``median(recent flux) + sensitivity * std(recent
    flux)`` past a short per-band cooldown. The ``median + k·std`` form is
    robust to high-variance input — a plain ``median × k`` threshold lets
    noisy mid/high bands fire spuriously between real onsets.

    ``beat`` fires on a bass onset (the primary kick), or on a simultaneous
    mid+high onset (handles kick-less sections). Tempo is estimated from the
    inter-onset intervals of the bass band, octave-folded into
    ``[tempo_min_bpm, tempo_max_bpm]``.

    Onset detection and tempo are suppressed while block RMS is below
    ``min_rms`` so room noise can't manufacture beats during silence.
    """

    def __init__(
        self,
        sample_rate: int = 22050,
        block_size: int = 1024,
        fft_size: int = 1024,
        flux_history_size: int = 43,
        bass_sensitivity: float = 3.0,
        mid_sensitivity: float = 4.0,
        high_sensitivity: float = 4.0,
        bass_max_hz: float = 250.0,
        high_min_hz: float = 4000.0,
        tempo_min_bpm: float = 50.0,
        tempo_max_bpm: float = 200.0,
        cooldown_ms: float = 110.0,
        min_rms: float = 0.003,
        tempo_history_size: int = 8,
    ) -> None:
        self.sample_rate = sample_rate
        self.block_size = block_size
        self.fft_size = fft_size
        self._min_rms = min_rms
        self._cooldown_ms = cooldown_ms
        self._tempo_min_bpm = tempo_min_bpm
        self._tempo_max_bpm = tempo_max_bpm

        self._sensitivity = {
            "bass": bass_sensitivity,
            "mid": mid_sensitivity,
            "high": high_sensitivity,
        }

        # Per-band rfft bin masks. rfftfreq gives the centre frequency of each
        # bin; a bin belongs to exactly one band.
        freqs = np.fft.rfftfreq(fft_size, d=1.0 / sample_rate)
        bass_mask = freqs < bass_max_hz
        high_mask = freqs >= high_min_hz
        mid_mask = ~bass_mask & ~high_mask
        self._band_masks = {"bass": bass_mask, "mid": mid_mask, "high": high_mask}

        self._window = np.hanning(fft_size).astype(np.float32)
        self._ring = np.zeros(max(fft_size, block_size), dtype=np.float32)
        self._block_ms = block_size / sample_rate * 1000.0

        self._flux_history: dict[str, collections.deque[float]] = {
            b: collections.deque(maxlen=flux_history_size) for b in _BANDS
        }
        self._prev_flux: dict[str, float] = {b: 0.0 for b in _BANDS}
        self._prev_mag: np.ndarray | None = None
        self._last_onset_ms: dict[str, float] = {b: -1e12 for b in _BANDS}
        self._bass_onset_times: collections.deque[float] = collections.deque(
            maxlen=tempo_history_size + 1
        )
        self._intervals: collections.deque[float] = collections.deque(
            maxlen=tempo_history_size
        )
        self._audio_ms = 0.0
        self._bpm = 0.0

    # ------------------------------------------------------------------ public
    @property
    def bpm(self) -> float:
        """Kick-locked tempo estimate (0.0 before it converges)."""
        return self._bpm

    def reset(self) -> None:
        self._ring[:] = 0.0
        self._prev_mag = None
        for b in _BANDS:
            self._flux_history[b].clear()
            self._prev_flux[b] = 0.0
            self._last_onset_ms[b] = -1e12
        self._bass_onset_times.clear()
        self._intervals.clear()
        self._audio_ms = 0.0
        self._bpm = 0.0

    def process_block(self, block: np.ndarray) -> OnsetResult:
        """Detect band onsets in one audio block."""
        self._audio_ms += self._block_ms
        block = np.asarray(block, dtype=np.float32)
        rms = float(np.sqrt(np.mean(block ** 2))) if block.size else 0.0

        # Slide the new block into the FFT ring buffer.
        n = min(block.size, self._ring.size)
        if n:
            self._ring = np.roll(self._ring, -n)
            self._ring[-n:] = block[-n:]

        windowed = self._ring[-self.fft_size :] * self._window
        mag = np.abs(np.fft.rfft(windowed))

        band_flux: dict[str, float] = {b: 0.0 for b in _BANDS}
        if self._prev_mag is not None:
            diff = mag - self._prev_mag
            diff[diff < 0.0] = 0.0  # half-wave rectify — onsets only
            for b in _BANDS:
                band_flux[b] = float(diff[self._band_masks[b]].sum())
        self._prev_mag = mag

        # Append flux to the rolling history regardless of the noise gate so
        # the adaptive threshold keeps tracking through quiet passages.
        onsets: dict[str, bool] = {b: False for b in _BANDS}
        audible = rms >= self._min_rms
        for b in _BANDS:
            hist = self._flux_history[b]
            flux = band_flux[b]
            # Need a few samples before the median/std threshold is meaningful.
            if len(hist) >= 8:
                arr = np.fromiter(hist, dtype=np.float64)
                threshold = float(np.median(arr)) + self._sensitivity[b] * float(
                    np.std(arr)
                )
                cooled = (
                    self._audio_ms - self._last_onset_ms[b]
                ) >= self._cooldown_ms
                # Rising edge (flux > previous block) rejects the noise-tail
                # exceedances that aren't actually a transient attack.
                rising = flux > self._prev_flux[b]
                if (
                    audible
                    and flux > threshold
                    and flux > 0.0
                    and rising
                    and cooled
                ):
                    onsets[b] = True
                    self._last_onset_ms[b] = self._audio_ms
            hist.append(flux)
            self._prev_flux[b] = flux

        if onsets["bass"]:
            self._register_bass_onset()

        beat = onsets["bass"] or (onsets["mid"] and onsets["high"])
        return OnsetResult(beat=beat, bpm=self._bpm, band_flux=band_flux)

    # ---------------------------------------------------------------- internal
    def _register_bass_onset(self) -> None:
        """Fold a fresh bass inter-onset interval into the tempo estimate."""
        self._bass_onset_times.append(self._audio_ms)
        if len(self._bass_onset_times) < 2:
            return
        interval = self._bass_onset_times[-1] - self._bass_onset_times[-2]
        interval = self._octave_fold(interval)
        if interval <= 0.0:
            return
        self._intervals.append(interval)
        if len(self._intervals) < 3:
            return
        intervals = list(self._intervals)
        mean = sum(intervals) / len(intervals)
        if mean <= 0.0:
            return
        # Only trust the estimate when the recent intervals agree — a noisy
        # spread means we're catching syncopation, not the pulse.
        stdev = float(np.std(intervals))
        if stdev / mean < 0.25:
            self._bpm = 60000.0 / float(np.median(intervals))

    def _octave_fold(self, interval_ms: float) -> float:
        """Pull an interval into the [tempo_min, tempo_max] BPM band.

        Half-/double-time detections (a hit landing on the off-beat, or a
        skipped kick) are folded so the tempo stays in a musical range.
        """
        if interval_ms <= 0.0:
            return interval_ms
        min_interval = 60000.0 / self._tempo_max_bpm
        max_interval = 60000.0 / self._tempo_min_bpm
        # Guard against pathological inputs with a bounded loop.
        for _ in range(8):
            if interval_ms < min_interval:
                interval_ms *= 2.0
            elif interval_ms > max_interval:
                interval_ms /= 2.0
            else:
                break
        return interval_ms
