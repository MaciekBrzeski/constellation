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


def test_ext_beat_override_fires_under_noise_gate() -> None:
    """ext_beat=True should fire even when RMS is below the gate."""
    det = _det(noise_gate=0.5)  # gate set so RMS path can't fire
    for _ in range(10):
        det._process_energy(0.02)
    det._process_energy(0.02, ext_beat=True, ext_bpm=128.0)
    assert det.beat_event.is_set()
    assert det.bpm == 128.0


def test_ext_bpm_overrides_interval_estimate() -> None:
    """A supplied backend BPM wins over the interval-derived estimate."""
    det = _det()
    for _ in range(10):
        det._process_energy(0.02)
    det._process_energy(0.5, ext_beat=True, ext_bpm=140.0)
    det._process_energy(0.5, ext_beat=True, ext_bpm=141.0)
    assert det.bpm == 141.0


def test_beat_count_increments_per_beat() -> None:
    """_beat_count is monotonic — one step per fired beat, -1 before any."""
    det = _det(cooldown_ms=0.0)
    assert det.beat_count == -1
    for _ in range(10):
        det._process_energy(0.02)
    det._process_energy(0.5)
    assert det.beat_count == 0
    det.beat_event.clear()
    det._process_energy(0.5)
    assert det.beat_count == 1


def test_epoch_bumps_on_tempo_relock() -> None:
    """A large BPM jump is a re-lock and bumps the sync epoch."""
    det = _det(cooldown_ms=0.0)
    for _ in range(10):
        det._process_energy(0.02)
    start_epoch = det.epoch
    det._process_energy(0.5, ext_beat=True, ext_bpm=120.0)
    assert det.epoch == start_epoch  # first BPM, no prior to compare
    det._process_energy(0.5, ext_beat=True, ext_bpm=121.0)
    assert det.epoch == start_epoch  # small change, no re-lock
    det._process_energy(0.5, ext_beat=True, ext_bpm=180.0)
    assert det.epoch == start_epoch + 1  # >25% jump → re-lock


def test_predicted_next_beat_follows_bpm() -> None:
    """predicted_next_beat_ms ≈ last beat + one bpm period."""
    det = _det(cooldown_ms=0.0)
    for _ in range(10):
        det._process_energy(0.02)
    det._process_energy(0.5, ext_beat=True, ext_bpm=120.0)
    expected = det.last_beat_monotonic_ms + 60000.0 / 120.0
    assert abs(det.predicted_next_beat_ms - expected) < 1e-6


def test_predicted_next_beat_zero_before_first_beat() -> None:
    det = _det()
    assert det.predicted_next_beat_ms == 0.0


def test_backend_resolves_to_rms_when_aubio_disabled() -> None:
    """backend='auto' + use_aubio=False resolves to the rms path."""
    det = _det(backend="auto", use_aubio=False)
    det._build_backend()
    assert det.backend_name == "rms"
    assert det._onset_backend is None
    assert det._aubio_tempo is None


def test_backend_multiband_builds_onset_backend() -> None:
    """backend='multiband' constructs the spectral-flux detector."""
    from constellation.audio.onset import MultibandSpectralFluxDetector

    det = _det(backend="multiband")
    det._build_backend()
    assert det.backend_name == "multiband"
    assert isinstance(det._onset_backend, MultibandSpectralFluxDetector)


def test_backend_explicit_aubio_falls_back_to_rms_without_lib() -> None:
    """An explicit backend='aubio' degrades to rms when aubio isn't installed."""
    det = _det(backend="aubio")
    det._build_backend()
    # aubio is an optional system package; on a host without it this is "rms".
    assert det.backend_name in ("aubio", "rms")


def test_device_param_is_stored() -> None:
    """The device kwarg is retained for the capture stream; defaults to None."""
    assert _det(device=3).device == 3
    assert _det().device is None


def test_intensity_ratio_setters() -> None:
    """Public ratio setters proxy the private tier-classification thresholds."""
    det = _det()
    det.low_intensity_ratio = 3.0
    det.high_intensity_ratio = 9.0
    assert det.low_intensity_ratio == 3.0
    assert det.high_intensity_ratio == 9.0
    assert det._low_intensity_ratio == 3.0
    assert det._high_intensity_ratio == 9.0


def test_list_input_devices_never_raises() -> None:
    """list_input_devices() returns a list even when PortAudio is unavailable."""
    from constellation.audio.beat import list_input_devices

    result = list_input_devices()
    assert isinstance(result, list)
    assert all(isinstance(idx, int) and isinstance(name, str)
               for idx, name in result)
