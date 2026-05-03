"""Tests for the hardware-free core of BeatDetector._process_energy."""

from __future__ import annotations

from constellation.audio.beat import BeatDetector


def _det(**kw) -> BeatDetector:
    defaults = dict(
        sensitivity=1.5,
        cooldown_ms=0.0,
        history_size=10,
        sample_rate=22050,
        block_size=1024,
        noise_gate=0.01,
        energy_window_size=50,
        low_intensity_ratio=2.0,
        high_intensity_ratio=6.0,
        tempo_history_size=4,
        min_noise_floor=0.002,
    )
    defaults.update(kw)
    return BeatDetector(**defaults)


def test_quiet_history_no_beat() -> None:
    det = _det()
    for _ in range(15):
        det._process_energy(0.02)
    assert not det.beat_event.is_set()


def test_energy_spike_fires_beat_event() -> None:
    det = _det()
    for _ in range(10):
        det._process_energy(0.02)
    det._process_energy(0.5)  # 25× baseline → well past sensitivity
    assert det.beat_event.is_set()


def test_noise_gate_drops_too_quiet_input() -> None:
    det = _det(noise_gate=0.05)
    for _ in range(20):
        det._process_energy(0.01)
    assert not det.beat_event.is_set()


def test_tier_low_on_silence() -> None:
    det = _det()
    # Fill the window with near-silence; intensity ≈ 1 → low.
    for _ in range(60):
        det._process_energy(0.003)
    assert det.energy_tier == "low"


def test_tier_high_on_loud_sustained() -> None:
    det = _det(low_intensity_ratio=2.0, high_intensity_ratio=6.0)
    # Seed a low noise floor, then sustained loud above threshold.
    for _ in range(30):
        det._process_energy(0.002)  # below noise_gate but still feeds window
    # Noise floor locks near 0.002 (p10 of window). Now play sustained loud.
    for _ in range(30):
        det._process_energy(0.05)
    # smoothed_energy drifts up toward 0.025; noise_floor stays at p10 ≈ 0.002
    # → intensity ≈ 12 → high.
    assert det.energy_tier == "high"


def test_tier_medium_between_thresholds() -> None:
    det = _det(low_intensity_ratio=2.0, high_intensity_ratio=6.0)
    for _ in range(30):
        det._process_energy(0.002)
    # Moderate above-floor energy → intensity between 2 and 6.
    for _ in range(30):
        det._process_energy(0.008)
    assert det.energy_tier == "medium"


def test_adaptive_noise_floor_tracks_p10() -> None:
    det = _det()
    # Mix of quiet + louder samples — p10 should land near the lower bound.
    for _ in range(30):
        det._process_energy(0.003)
    for _ in range(20):
        det._process_energy(0.05)
    assert 0.002 <= det.noise_floor <= 0.01


def test_noise_floor_min_clamp() -> None:
    det = _det(min_noise_floor=0.01)
    for _ in range(60):
        det._process_energy(0.0001)
    # Even with near-zero input, floor stays at the configured minimum.
    assert det.noise_floor >= 0.01


def test_bpm_uses_audio_clock_not_wall_clock() -> None:
    """Offline replay must produce sane BPM (~ block rate × spike spacing)."""
    det = _det(
        sample_rate=22050, block_size=1024, sensitivity=1.5, cooldown_ms=100.0
    )
    # Prime history with quiet baseline.
    for _ in range(10):
        det._process_energy(0.02)
    # Block duration ≈ 46.4 ms. Fire a beat every 10 blocks ≈ 464 ms → ~129 BPM.
    for _ in range(30):
        for _ in range(9):
            det._process_energy(0.02)
        det._process_energy(1.0)
    assert 100.0 < det.bpm < 180.0
    assert det.bpm < 1e6  # regression: the old wall-clock BPM went to infinity


def test_cooldown_enforced_on_audio_clock() -> None:
    det = _det(cooldown_ms=200.0)
    for _ in range(10):
        det._process_energy(0.02)
    # Three spikes back-to-back — block gap is ~46 ms, cooldown 200 ms so only
    # the first should fire; next two blocked until audio-time >= 200 ms.
    det._process_energy(1.0)
    assert det.beat_event.is_set()
    det.beat_event.clear()
    det._process_energy(1.0)
    assert not det.beat_event.is_set()
    det.beat_event.clear()
    det._process_energy(1.0)
    assert not det.beat_event.is_set()


def test_aubio_beat_override_fires_under_noise_gate() -> None:
    """aubio_beat=True should fire even when RMS is below the gate."""
    det = _det(noise_gate=0.5)  # gate set so RMS path can't fire
    for _ in range(10):
        det._process_energy(0.02)
    det._process_energy(0.02, aubio_beat=True, aubio_bpm=128.0)
    assert det.beat_event.is_set()
    assert det.bpm == 128.0


def test_aubio_bpm_overrides_interval_estimate() -> None:
    """Supplied aubio BPM wins over the interval-derived estimate."""
    det = _det()
    for _ in range(10):
        det._process_energy(0.02)
    det._process_energy(0.5, aubio_beat=True, aubio_bpm=140.0)
    det._process_energy(0.5, aubio_beat=True, aubio_bpm=141.0)
    assert det.bpm == 141.0
