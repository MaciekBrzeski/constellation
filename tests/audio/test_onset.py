"""Tests for the multiband spectral-flux onset backend."""

from __future__ import annotations

import numpy as np

from constellation.audio.onset import _BANDS, MultibandSpectralFluxDetector

_SR = 22050
_BLOCK = 1024


def _det(**kw) -> MultibandSpectralFluxDetector:
    defaults = dict(sample_rate=_SR, block_size=_BLOCK, fft_size=_BLOCK)
    defaults.update(kw)
    return MultibandSpectralFluxDetector(**defaults)


def _sine(freq: float, amp: float = 0.5) -> np.ndarray:
    t = np.arange(_BLOCK) / _SR
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _quiet() -> np.ndarray:
    return (0.005 * np.random.randn(_BLOCK)).astype(np.float32)


def test_band_masks_partition_the_spectrum() -> None:
    """Every rfft bin belongs to exactly one band; bass covers the low end."""
    det = _det()
    masks = det._band_masks
    total = sum(int(masks[b].sum()) for b in _BANDS)
    n_bins = _BLOCK // 2 + 1
    assert total == n_bins  # partition — no bin lost or double-counted
    freqs = np.fft.rfftfreq(_BLOCK, d=1.0 / _SR)
    assert masks["bass"][freqs < 250.0].all()
    assert masks["high"][freqs >= 4000.0].all()
    assert not (masks["bass"] & masks["high"]).any()


def test_silence_never_fires() -> None:
    det = _det()
    fired = sum(det.process_block(np.zeros(_BLOCK, np.float32)).beat for _ in range(100))
    assert fired == 0


def test_white_noise_barely_fires() -> None:
    """Sustained white noise has no real onsets — at most a rare false beat."""
    np.random.seed(9)
    det = _det()
    fired = sum(
        det.process_block((0.02 * np.random.randn(_BLOCK)).astype(np.float32)).beat
        for _ in range(300)
    )
    assert fired < 10  # ~1% worst case on pathological pure noise


def test_bass_burst_fires_a_beat() -> None:
    """A loud bass tone after quiet warmup triggers a bass onset → beat."""
    np.random.seed(1)
    det = _det()
    for _ in range(12):  # warm up the flux history (threshold needs >= 8)
        det.process_block(_quiet())
    result = det.process_block(_sine(60.0, amp=0.6))
    assert result.beat
    assert result.band_flux["bass"] > result.band_flux["high"]


def test_kick_locked_tempo_converges() -> None:
    """Periodic bass impulses → a stable BPM near the true tempo."""
    np.random.seed(2)
    det = _det()
    period = 10  # blocks → 10 * 46.4 ms ≈ 464 ms ≈ 129 BPM
    for i in range(200):
        block = _sine(60.0, amp=0.6) if i % period == 0 else _quiet()
        det.process_block(block)
    assert 110.0 < det.bpm < 150.0


def test_octave_fold_pulls_into_musical_range() -> None:
    det = _det(tempo_min_bpm=50.0, tempo_max_bpm=200.0)
    # 200 ms interval = 300 BPM → folded ×2 to 400 ms (150 BPM).
    assert det._octave_fold(200.0) == 400.0
    # 2000 ms interval = 30 BPM → folded ÷2 to 1000 ms (60 BPM).
    assert det._octave_fold(2000.0) == 1000.0
    # Already in range — untouched.
    assert det._octave_fold(500.0) == 500.0


def test_reset_clears_state() -> None:
    np.random.seed(3)
    det = _det()
    for _ in range(30):
        det.process_block(_sine(60.0, amp=0.5))
    det.reset()
    assert det._prev_mag is None
    assert det.bpm == 0.0
    assert all(len(det._flux_history[b]) == 0 for b in _BANDS)
