"""Baseline + replay diagnostics for the BeatDetector.

Record a brief mic sample, or replay a WAV, and emit per-block stats + a
summary. Iterate on detector params without re-recording by passing ``--wav``.

Usage:
    # record + analyse with current config
    python scripts/audio_diag.py --duration 15

    # replay a saved WAV with different params
    python scripts/audio_diag.py --wav tmp/sample.wav --sensitivity 1.2

    # dump per-block rows as CSV for plotting
    python scripts/audio_diag.py --wav tmp/sample.wav --csv tmp/stats.csv
"""

from __future__ import annotations

import argparse
import time
import wave
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from constellation.audio.beat import BeatDetector
from constellation.config import AudioConfig


@dataclass
class Event:
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


def record(duration_s: float, sample_rate: int, out: Path) -> np.ndarray:
    import sounddevice as sd

    total = int(sample_rate * duration_s)
    print(f"[rec] {duration_s}s at {sample_rate} Hz → {out}")
    for i in (3, 2, 1):
        print(f"  {i}...")
        time.sleep(1)
    print("  GO")
    data = sd.rec(total, samplerate=sample_rate, channels=1, dtype="float32")
    sd.wait()
    mono = data.flatten().astype(np.float32)

    out.parent.mkdir(parents=True, exist_ok=True)
    clipped = np.clip(mono * 32767.0, -32768.0, 32767.0).astype(np.int16)
    with wave.open(str(out), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(clipped.tobytes())
    print(f"[rec] saved {len(mono)} samples")
    return mono


def load_wav(path: Path) -> tuple[np.ndarray, int]:
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
) -> list[Event]:
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
    events: list[Event] = []
    block_size = cfg.block_size
    n_blocks = len(audio) // block_size
    for i in range(n_blocks):
        block = audio[i * block_size : (i + 1) * block_size]
        rms = float(np.sqrt(np.mean(block**2)))
        det.beat_event.clear()
        det._process_energy(rms)
        fired = det.beat_event.is_set()
        history = det._energy_history
        rolling = sum(history) / len(history) if history else 0.0
        smoothed = det.smoothed_energy
        floor = det.noise_floor
        # Tier intensity uses the fast EMA (tier_energy); diag surfaces it so
        # the table lines up with the tier column.
        intensity = det.tier_intensity
        events.append(
            Event(
                t_ms=i * block_size / sample_rate * 1000.0,
                rms=rms,
                rolling_mean=rolling,
                smoothed=smoothed,
                noise_floor=floor,
                intensity=intensity,
                tier=det.energy_tier,
                fired=fired,
                strength=det.last_beat_strength if fired else 0.0,
                bpm=det.bpm,
            )
        )
    return events


def summarise(events: list[Event], duration_s: float, cfg: AudioConfig) -> None:
    import statistics as st

    rms = [e.rms for e in events]
    fired = [e for e in events if e.fired]
    tiers = [e.tier for e in events if e.rms >= cfg.noise_gate]
    tier_count = {t: tiers.count(t) for t in ("low", "medium", "high")}
    bpms = [e.bpm for e in events if e.bpm > 0]

    p = lambda xs, q: float(np.percentile(xs, q)) if xs else 0.0  # noqa: E731

    print()
    print("=== RMS distribution ===")
    print(f"  min   {min(rms):.4f}")
    print(f"  p05   {p(rms, 5):.4f}")
    print(f"  p50   {p(rms, 50):.4f}")
    print(f"  p90   {p(rms, 90):.4f}")
    print(f"  p99   {p(rms, 99):.4f}")
    print(f"  max   {max(rms):.4f}")
    print(f"  noise_gate={cfg.noise_gate:.4f}  sensitivity={cfg.sensitivity}")

    print()
    print("=== Beats ===")
    print(f"  fired:      {len(fired)} over {duration_s:.1f}s  "
          f"({len(fired) / duration_s:.2f} /s)")
    if len(fired) >= 2:
        gaps = [
            fired[i + 1].t_ms - fired[i].t_ms for i in range(len(fired) - 1)
        ]
        print(f"  gap ms:     mean {st.mean(gaps):.0f}  "
              f"median {st.median(gaps):.0f}  "
              f"stdev {st.pstdev(gaps):.0f}  "
              f"min/max {min(gaps):.0f}/{max(gaps):.0f}")
    print(f"  BPM final:  {bpms[-1]:.1f}" if bpms else "  BPM: never converged")
    if len(bpms) >= 10:
        print(f"  BPM stdev:  {st.pstdev(bpms[-min(50, len(bpms)):]):.1f} "
              f"over last {min(50, len(bpms))} beats")

    if fired:
        strengths = [e.strength for e in fired]
        print(f"  strength:   min {min(strengths):.2f}  "
              f"p50 {p(strengths, 50):.2f}  "
              f"p90 {p(strengths, 90):.2f}  "
              f"max {max(strengths):.2f}")
        for thr in (2.0, 2.5, 3.0, 4.0):
            n_strong = sum(1 for s in strengths if s >= thr)
            print(f"    threshold {thr:.1f} → {n_strong}/{len(fired)} strong "
                  f"({100.0 * n_strong / len(fired):.0f}%)")

    print()
    print("=== Energy tiers ===")
    total_audible = sum(tier_count.values()) or 1
    for t in ("low", "medium", "high"):
        pct = 100.0 * tier_count[t] / total_audible
        print(f"  {t:<7} {tier_count[t]:5d} blocks  ({pct:5.1f}%)")

    print()
    print("=== Suggestions ===")
    # Noise-floor hint: 5th percentile of RMS often reflects room tone.
    suggested_gate = max(0.001, p(rms, 10) * 1.5)
    if suggested_gate < cfg.noise_gate * 0.5 or suggested_gate > cfg.noise_gate * 2:
        print(f"  noise_gate  current {cfg.noise_gate:.4f}  "
              f"→ try ~{suggested_gate:.4f} (1.5 × p10 RMS)")
    # Sensitivity hint: if almost everything fires or nothing, suggest shift.
    beat_rate = len(fired) / max(duration_s, 1e-6)
    if beat_rate < 0.5:
        print(f"  sensitivity  {cfg.sensitivity}  → try {cfg.sensitivity * 0.8:.2f} "
              f"(few beats fired: {beat_rate:.2f}/s)")
    elif beat_rate > 4.0:
        print(f"  sensitivity  {cfg.sensitivity}  → try {cfg.sensitivity * 1.25:.2f} "
              f"(too many beats: {beat_rate:.2f}/s)")
    if not any(e.bpm > 0 for e in events):
        print("  BPM never estimated — need ≥2 beats during sample")


def write_csv(events: list[Event], path: Path) -> None:
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
    print(f"[csv] {path}")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", type=Path, default=None,
                    help="replay WAV instead of recording")
    ap.add_argument("--duration", type=float, default=15.0,
                    help="record duration (seconds)")
    ap.add_argument("--out", type=Path, default=Path("tmp/sample.wav"),
                    help="record output path")
    ap.add_argument("--csv", type=Path, default=None,
                    help="dump per-block stats to CSV")
    # Allow overriding detector params for quick iteration.
    ap.add_argument("--sensitivity", type=float, default=None)
    ap.add_argument("--cooldown-ms", type=float, default=None)
    ap.add_argument("--noise-gate", type=float, default=None)
    ap.add_argument("--history-size", type=int, default=None)
    args = ap.parse_args(argv)

    if args.wav is None:
        audio = record(args.duration, sample_rate=22050, out=args.out)
        sr = 22050
    else:
        audio, sr = load_wav(args.wav)
        print(f"[wav] {args.wav} — {len(audio)} samples @ {sr} Hz "
              f"({len(audio) / sr:.1f} s)")

    cfg = AudioConfig()
    if args.sensitivity is not None: cfg = _replace(cfg, sensitivity=args.sensitivity)
    if args.cooldown_ms is not None: cfg = _replace(cfg, cooldown_ms=args.cooldown_ms)
    if args.noise_gate is not None: cfg = _replace(cfg, noise_gate=args.noise_gate)
    if args.history_size is not None: cfg = _replace(cfg, history_size=args.history_size)

    events = analyse(audio, sr, cfg)
    summarise(events, duration_s=len(audio) / sr, cfg=cfg)
    if args.csv:
        write_csv(events, args.csv)


def _replace(cfg: AudioConfig, **kw) -> AudioConfig:
    # AudioConfig is frozen — rebuild with overrides.
    from dataclasses import asdict
    d = asdict(cfg)
    d.update(kw)
    return AudioConfig(**d)


if __name__ == "__main__":
    main()
