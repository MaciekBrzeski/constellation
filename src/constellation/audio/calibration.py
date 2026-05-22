"""Beat-detector calibration: record/replay audio and analyse the detector.

Records a brief microphone sample (or replays a WAV) through ``BeatDetector``
offline, collects per-block stats, and derives a :class:`CalibrationReport`
plus suggested ``sensitivity`` / ``noise_gate`` values.

Used both by the ``scripts/audio_diag.py`` CLI and the desktop control panel's
Calibration tab.
"""

from __future__ import annotations

import statistics as st
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from constellation.audio.beat import BeatDetector
from constellation.config import AudioConfig

# Strength thresholds reported in the per-beat histogram.
_STRENGTH_THRESHOLDS = (2.0, 2.5, 3.0, 4.0)

# Baseline (room-tone) calibration margins. The gate must sit above genuine
# background noise so silence never trips it; the adaptive-floor clamp tracks
# the typical room level.
_GATE_FROM_P95 = 1.5
_GATE_FROM_P99 = 1.2
_FLOOR_FROM_P50 = 1.0
_MIN_GATE = 0.001
_MIN_FLOOR = 0.0005


@dataclass
class Event:
    """Per-block diagnostic snapshot from a calibration pass."""

    t_ms: float
    rms: float
    rolling_mean: float
    smoothed: float
    noise_floor: float
    intensity: float
    tier: str
    fired: bool
    strength: float
    bpm: float
    # Per-band spectral flux — populated only on a ``backend="multiband"``
    # pass, ``None`` for the default RMS pass.
    band_flux: dict[str, float] | None = None


@dataclass
class CalibrationReport:
    """Structured summary of a calibration pass — measurement only."""

    duration_s: float
    rms_min: float
    rms_p05: float
    rms_p10: float
    rms_p50: float
    rms_p90: float
    rms_p99: float
    rms_max: float
    beats_fired: int
    beat_rate_hz: float
    gap_mean_ms: float
    gap_median_ms: float
    gap_stdev_ms: float
    gap_min_ms: float
    gap_max_ms: float
    bpm_final: float
    bpm_estimated: bool
    tier_blocks: dict[str, int]
    strength_min: float
    strength_p50: float
    strength_p90: float
    strength_max: float
    events: list[Event] = field(default_factory=list)


@dataclass
class BaselineReport:
    """RMS distribution of a music-free room-tone recording.

    Used to set ``noise_gate`` (the hard silence gate) and ``min_noise_floor``
    (the lower clamp on the detector's adaptive floor) from genuine background
    noise rather than from the quiet passages of a music sample.
    """

    duration_s: float
    blocks: int
    rms_min: float
    rms_p50: float
    rms_p90: float
    rms_p95: float
    rms_p99: float
    rms_max: float


def _pct(xs: list[float], q: float) -> float:
    return float(np.percentile(xs, q)) if xs else 0.0


def measure_baseline(
    audio: np.ndarray, sample_rate: int, cfg: AudioConfig
) -> BaselineReport:
    """Reduce a room-tone (no music) recording to an RMS distribution.

    Splits ``audio`` into ``cfg.block_size`` blocks — the same granularity the
    live detector sees — and summarises their RMS spread.
    """
    block_size = cfg.block_size
    n_blocks = len(audio) // block_size
    rms = [
        float(np.sqrt(np.mean(audio[i * block_size : (i + 1) * block_size] ** 2)))
        for i in range(n_blocks)
    ]
    return BaselineReport(
        duration_s=len(audio) / max(sample_rate, 1),
        blocks=n_blocks,
        rms_min=min(rms) if rms else 0.0,
        rms_p50=_pct(rms, 50),
        rms_p90=_pct(rms, 90),
        rms_p95=_pct(rms, 95),
        rms_p99=_pct(rms, 99),
        rms_max=max(rms) if rms else 0.0,
    )


def suggest_from_baseline(report: BaselineReport) -> dict[str, float]:
    """Recommend ``noise_gate`` and ``min_noise_floor`` from room tone.

    ``noise_gate`` clears even the loudest background blip; ``min_noise_floor``
    keeps the adaptive floor anchored at the typical room level.
    """
    gate = max(
        _MIN_GATE,
        report.rms_p95 * _GATE_FROM_P95,
        report.rms_p99 * _GATE_FROM_P99,
    )
    floor = max(_MIN_FLOOR, report.rms_p50 * _FLOOR_FROM_P50)
    return {"noise_gate": gate, "min_noise_floor": floor}


def format_baseline_summary(
    report: BaselineReport, cfg: AudioConfig, suggestions: dict[str, float]
) -> str:
    """Render the human-readable baseline summary for the GUI text box."""
    lines: list[str] = [
        "",
        f"=== Background noise ({report.duration_s:.1f}s, {report.blocks} blocks) ===",
        f"  min   {report.rms_min:.4f}",
        f"  p50   {report.rms_p50:.4f}",
        f"  p90   {report.rms_p90:.4f}",
        f"  p95   {report.rms_p95:.4f}",
        f"  p99   {report.rms_p99:.4f}",
        f"  max   {report.rms_max:.4f}",
        "",
        "=== Suggestions ===",
        f"  noise_gate       current {cfg.noise_gate:.4f}  "
        f"→ {suggestions['noise_gate']:.4f}",
        f"  min_noise_floor  current {cfg.min_noise_floor:.4f}  "
        f"→ {suggestions['min_noise_floor']:.4f}",
        "",
        "Apply, then run a music pass to tune sensitivity.",
    ]
    return "\n".join(lines)


def record(
    duration_s: float,
    sample_rate: int,
    *,
    device: int | str | None = None,
    out: Path | None = None,
    countdown: bool = True,
) -> np.ndarray:
    """Capture ``duration_s`` of mono audio from ``device`` (or system default).

    Writes a 16-bit WAV to ``out`` when given. ``countdown`` prints a 3-2-1
    lead-in (the CLI uses it; the GUI drives its own and passes ``False``).
    """
    import sounddevice as sd  # type: ignore[import-untyped]

    total = int(sample_rate * duration_s)
    if countdown:
        print(f"[rec] {duration_s}s at {sample_rate} Hz" + (f" → {out}" if out else ""))
        for i in (3, 2, 1):
            print(f"  {i}...")
            time.sleep(1)
        print("  GO")
    data = sd.rec(
        total, samplerate=sample_rate, channels=1, dtype="float32", device=device
    )
    sd.wait()
    mono = data.flatten().astype(np.float32)

    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        clipped = np.clip(mono * 32767.0, -32768.0, 32767.0).astype(np.int16)
        with wave.open(str(out), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(sample_rate)
            w.writeframes(clipped.tobytes())
    if countdown:
        print(f"[rec] saved {len(mono)} samples")
    return mono


def load_wav(path: Path) -> tuple[np.ndarray, int]:
    """Load a 16-bit WAV as float32 mono in [-1, 1] plus its sample rate."""
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        channels = w.getnchannels()
        sampwidth = w.getsampwidth()
        frames = w.readframes(w.getnframes())
    if sampwidth != 2:
        raise SystemExit(f"only 16-bit WAV supported (got {sampwidth*8}-bit)")
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    return audio, sr


def analyse(
    audio: np.ndarray,
    sample_rate: int,
    cfg: AudioConfig,
    backend: str = "rms",
) -> list[Event]:
    """Replay ``audio`` block-by-block through a detector, return per-block events.

    ``backend="rms"`` (default) exercises the wideband RMS onset path.
    ``backend="multiband"`` runs the multiband spectral-flux detector and
    feeds its beats into the energy/tier logic, also recording per-band flux.
    """
    det = BeatDetector(
        sensitivity=cfg.sensitivity,
        cooldown_ms=cfg.cooldown_ms,
        history_size=cfg.history_size,
        sample_rate=sample_rate,
        block_size=cfg.block_size,
        noise_gate=cfg.noise_gate,
        energy_window_size=cfg.energy_window_size,
        low_intensity_ratio=cfg.low_intensity_ratio,
        high_intensity_ratio=cfg.high_intensity_ratio,
        tempo_history_size=cfg.tempo_history_size,
        min_noise_floor=cfg.min_noise_floor,
    )
    mb = None
    if backend == "multiband":
        from constellation.audio.onset import MultibandSpectralFluxDetector

        mb = MultibandSpectralFluxDetector(
            sample_rate=sample_rate,
            block_size=cfg.block_size,
            fft_size=cfg.fft_size,
            flux_history_size=cfg.flux_history_size,
            bass_sensitivity=cfg.bass_sensitivity,
            mid_sensitivity=cfg.mid_sensitivity,
            high_sensitivity=cfg.high_sensitivity,
            bass_max_hz=cfg.bass_max_hz,
            high_min_hz=cfg.high_min_hz,
            tempo_min_bpm=cfg.tempo_min_bpm,
            tempo_max_bpm=cfg.tempo_max_bpm,
            tempo_history_size=cfg.tempo_history_size,
        )
    events: list[Event] = []
    block_size = cfg.block_size
    n_blocks = len(audio) // block_size
    for i in range(n_blocks):
        block = audio[i * block_size : (i + 1) * block_size]
        rms = float(np.sqrt(np.mean(block**2)))
        det.beat_event.clear()
        band_flux: dict[str, float] | None = None
        if mb is not None:
            result = mb.process_block(block)
            band_flux = result.band_flux
            det._process_energy(rms, ext_beat=result.beat, ext_bpm=result.bpm)
        else:
            det._process_energy(rms)
        fired = det.beat_event.is_set()
        history = det._energy_history
        rolling = sum(history) / len(history) if history else 0.0
        events.append(
            Event(
                t_ms=i * block_size / sample_rate * 1000.0,
                rms=rms,
                rolling_mean=rolling,
                smoothed=det.smoothed_energy,
                noise_floor=det.noise_floor,
                # Tier intensity uses the fast EMA (tier_energy) so the
                # intensity column lines up with the tier column.
                intensity=det.tier_intensity,
                tier=det.energy_tier,
                fired=fired,
                strength=det.last_beat_strength if fired else 0.0,
                bpm=det.bpm,
                band_flux=band_flux,
            )
        )
    return events


def build_report(
    events: list[Event], duration_s: float, cfg: AudioConfig
) -> CalibrationReport:
    """Reduce per-block events to a :class:`CalibrationReport`."""
    rms = [e.rms for e in events]
    fired = [e for e in events if e.fired]
    tiers = [e.tier for e in events if e.rms >= cfg.noise_gate]
    tier_count = {t: tiers.count(t) for t in ("low", "medium", "high")}
    bpms = [e.bpm for e in events if e.bpm > 0]

    gaps = [
        fired[i + 1].t_ms - fired[i].t_ms for i in range(len(fired) - 1)
    ]
    strengths = [e.strength for e in fired]

    return CalibrationReport(
        duration_s=duration_s,
        rms_min=min(rms) if rms else 0.0,
        rms_p05=_pct(rms, 5),
        rms_p10=_pct(rms, 10),
        rms_p50=_pct(rms, 50),
        rms_p90=_pct(rms, 90),
        rms_p99=_pct(rms, 99),
        rms_max=max(rms) if rms else 0.0,
        beats_fired=len(fired),
        beat_rate_hz=len(fired) / max(duration_s, 1e-6),
        gap_mean_ms=st.mean(gaps) if gaps else 0.0,
        gap_median_ms=st.median(gaps) if gaps else 0.0,
        gap_stdev_ms=st.pstdev(gaps) if gaps else 0.0,
        gap_min_ms=min(gaps) if gaps else 0.0,
        gap_max_ms=max(gaps) if gaps else 0.0,
        bpm_final=bpms[-1] if bpms else 0.0,
        bpm_estimated=bool(bpms),
        tier_blocks=tier_count,
        strength_min=min(strengths) if strengths else 0.0,
        strength_p50=_pct(strengths, 50),
        strength_p90=_pct(strengths, 90),
        strength_max=max(strengths) if strengths else 0.0,
        events=events,
    )


def suggest(
    report: CalibrationReport,
    cfg: AudioConfig,
    *,
    have_baseline: bool = False,
) -> dict[str, float]:
    """Recommend detector tweaks. Only includes a key when it should change.

    - ``noise_gate``: ~1.5 × the p10 RMS, when meaningfully off the current.
      Skipped when ``have_baseline`` is set — a dedicated room-tone pass
      (:func:`measure_baseline`) owns the gate, and music-pass p10 is the
      quiet part of the song, not true silence.
    - ``sensitivity``: lowered if almost nothing fires, raised if too much does.
    """
    out: dict[str, float] = {}
    if not have_baseline:
        suggested_gate = max(0.001, report.rms_p10 * 1.5)
        if suggested_gate < cfg.noise_gate * 0.5 or suggested_gate > cfg.noise_gate * 2:
            out["noise_gate"] = suggested_gate
    if report.beat_rate_hz < 0.5:
        out["sensitivity"] = cfg.sensitivity * 0.8
    elif report.beat_rate_hz > 4.0:
        out["sensitivity"] = cfg.sensitivity * 1.25
    return out


def format_summary(
    report: CalibrationReport,
    cfg: AudioConfig,
    suggestions: dict[str, float],
) -> str:
    """Render the human-readable multi-line summary (CLI + GUI text box)."""
    lines: list[str] = []
    lines.append("")
    lines.append("=== RMS distribution ===")
    lines.append(f"  min   {report.rms_min:.4f}")
    lines.append(f"  p05   {report.rms_p05:.4f}")
    lines.append(f"  p50   {report.rms_p50:.4f}")
    lines.append(f"  p90   {report.rms_p90:.4f}")
    lines.append(f"  p99   {report.rms_p99:.4f}")
    lines.append(f"  max   {report.rms_max:.4f}")
    lines.append(
        f"  noise_gate={cfg.noise_gate:.4f}  sensitivity={cfg.sensitivity}"
    )

    lines.append("")
    lines.append("=== Beats ===")
    lines.append(
        f"  fired:      {report.beats_fired} over {report.duration_s:.1f}s  "
        f"({report.beat_rate_hz:.2f} /s)"
    )
    if report.beats_fired >= 2:
        lines.append(
            f"  gap ms:     mean {report.gap_mean_ms:.0f}  "
            f"median {report.gap_median_ms:.0f}  "
            f"stdev {report.gap_stdev_ms:.0f}  "
            f"min/max {report.gap_min_ms:.0f}/{report.gap_max_ms:.0f}"
        )
    if report.bpm_estimated:
        lines.append(f"  BPM final:  {report.bpm_final:.1f}")
    else:
        lines.append("  BPM: never converged")
    bpms = [e.bpm for e in report.events if e.bpm > 0]
    if len(bpms) >= 10:
        window = min(50, len(bpms))
        lines.append(
            f"  BPM stdev:  {st.pstdev(bpms[-window:]):.1f} over last {window} beats"
        )

    if report.beats_fired:
        lines.append(
            f"  strength:   min {report.strength_min:.2f}  "
            f"p50 {report.strength_p50:.2f}  "
            f"p90 {report.strength_p90:.2f}  "
            f"max {report.strength_max:.2f}"
        )
        strengths = [e.strength for e in report.events if e.fired]
        for thr in _STRENGTH_THRESHOLDS:
            n_strong = sum(1 for s in strengths if s >= thr)
            lines.append(
                f"    threshold {thr:.1f} → {n_strong}/{report.beats_fired} strong "
                f"({100.0 * n_strong / report.beats_fired:.0f}%)"
            )

    lines.append("")
    lines.append("=== Energy tiers ===")
    total_audible = sum(report.tier_blocks.values()) or 1
    for t in ("low", "medium", "high"):
        pct = 100.0 * report.tier_blocks[t] / total_audible
        lines.append(f"  {t:<7} {report.tier_blocks[t]:5d} blocks  ({pct:5.1f}%)")

    lines.append("")
    lines.append("=== Suggestions ===")
    if "noise_gate" in suggestions:
        lines.append(
            f"  noise_gate  current {cfg.noise_gate:.4f}  "
            f"→ try ~{suggestions['noise_gate']:.4f} (1.5 × p10 RMS)"
        )
    if "sensitivity" in suggestions:
        if report.beat_rate_hz < 0.5:
            lines.append(
                f"  sensitivity  {cfg.sensitivity}  → try "
                f"{suggestions['sensitivity']:.2f} "
                f"(few beats fired: {report.beat_rate_hz:.2f}/s)"
            )
        else:
            lines.append(
                f"  sensitivity  {cfg.sensitivity}  → try "
                f"{suggestions['sensitivity']:.2f} "
                f"(too many beats: {report.beat_rate_hz:.2f}/s)"
            )
    if not report.bpm_estimated:
        lines.append("  BPM never estimated — need ≥2 beats during sample")

    return "\n".join(lines)


def write_csv(events: list[Event], path: Path) -> None:
    """Dump per-block events as CSV for offline plotting."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        f.write(
            "t_ms,rms,rolling,smoothed,noise_floor,intensity,"
            "tier,fired,strength,bpm\n"
        )
        for e in events:
            f.write(
                f"{e.t_ms:.1f},{e.rms:.5f},{e.rolling_mean:.5f},"
                f"{e.smoothed:.5f},{e.noise_floor:.5f},{e.intensity:.2f},"
                f"{e.tier},{int(e.fired)},{e.strength:.2f},{e.bpm:.2f}\n"
            )
