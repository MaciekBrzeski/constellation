"""Tests for the hardware-free calibration analysis."""

from __future__ import annotations

import numpy as np

from constellation.audio.calibration import (
    CalibrationReport,
    analyse,
    build_report,
    format_summary,
    suggest,
)
from constellation.config import AudioConfig

_SR = 22050
_BLOCK = 1024


def _burst_audio(n_blocks: int = 220, period: int = 21) -> np.ndarray:
    """Quiet signal with a loud burst every ``period`` blocks."""
    audio = np.full(n_blocks * _BLOCK, 0.02, dtype=np.float32)
    for b in range(period, n_blocks, period):
        audio[b * _BLOCK : (b + 1) * _BLOCK] = 0.6
    return audio


def _report(**kw) -> CalibrationReport:
    base = dict(
        duration_s=10.0,
        rms_min=0.01, rms_p05=0.012, rms_p10=0.02,
        rms_p50=0.05, rms_p90=0.2, rms_p99=0.4, rms_max=0.5,
        beats_fired=10, beat_rate_hz=1.0,
        gap_mean_ms=1000.0, gap_median_ms=1000.0, gap_stdev_ms=10.0,
        gap_min_ms=950.0, gap_max_ms=1050.0,
        bpm_final=60.0, bpm_estimated=True,
        tier_blocks={"low": 10, "medium": 20, "high": 5},
        strength_min=2.0, strength_p50=3.0, strength_p90=5.0, strength_max=7.0,
        events=[],
    )
    base.update(kw)
    return CalibrationReport(**base)


def test_analyse_detects_periodic_bursts() -> None:
    events = analyse(_burst_audio(), _SR, AudioConfig())
    assert len(events) == 220
    fired = [e for e in events if e.fired]
    assert len(fired) >= 5  # roughly one per burst


def test_analyse_rms_backend_leaves_band_flux_none() -> None:
    """The default RMS pass records no per-band flux (regression guard)."""
    events = analyse(_burst_audio(), _SR, AudioConfig())
    assert all(e.band_flux is None for e in events)


def test_analyse_multiband_backend_records_band_flux() -> None:
    """The multiband pass runs end-to-end and populates per-band flux."""
    cfg = AudioConfig()
    events = analyse(_burst_audio(), _SR, cfg, backend="multiband")
    assert len(events) == 220
    assert all(e.band_flux is not None for e in events)
    assert set(events[-1].band_flux) == {"bass", "mid", "high"}


def test_build_report_summarises_events() -> None:
    cfg = AudioConfig()
    audio = _burst_audio()
    events = analyse(audio, _SR, cfg)
    report = build_report(events, duration_s=len(audio) / _SR, cfg=cfg)
    assert report.beats_fired > 0
    assert report.beat_rate_hz > 0
    assert report.rms_max >= report.rms_p50 >= report.rms_min


def test_suggest_lowers_sensitivity_when_few_beats() -> None:
    cfg = AudioConfig()
    out = suggest(_report(beat_rate_hz=0.2), cfg)
    assert out["sensitivity"] == cfg.sensitivity * 0.8


def test_suggest_raises_sensitivity_when_too_many_beats() -> None:
    cfg = AudioConfig()
    out = suggest(_report(beat_rate_hz=6.0), cfg)
    assert out["sensitivity"] == cfg.sensitivity * 1.25


def test_suggest_keeps_sensitivity_in_normal_range() -> None:
    out = suggest(_report(beat_rate_hz=2.0), AudioConfig())
    assert "sensitivity" not in out


def test_suggest_noise_gate_from_p10() -> None:
    cfg = AudioConfig()
    out = suggest(_report(rms_p10=0.05), cfg)
    assert out["noise_gate"] == max(0.001, 0.05 * 1.5)


def test_format_summary_is_readable() -> None:
    cfg = AudioConfig()
    report = _report()
    text = format_summary(report, cfg, suggest(report, cfg))
    assert "RMS distribution" in text
    assert "Energy tiers" in text
    assert text.count("\n") > 5
