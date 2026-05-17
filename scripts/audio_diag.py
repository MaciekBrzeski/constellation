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

This is a thin CLI wrapper — the reusable logic lives in
``constellation.audio.calibration``.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from pathlib import Path

from constellation.audio.calibration import (
    analyse,
    build_report,
    format_summary,
    load_wav,
    record,
    suggest,
    write_csv,
)
from constellation.config import AudioConfig


def _replace(cfg: AudioConfig, **kw) -> AudioConfig:
    # AudioConfig is frozen — rebuild with overrides.
    d = asdict(cfg)
    d.update(kw)
    return AudioConfig(**d)


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
    ap.add_argument("--backend", choices=["rms", "multiband"], default="rms",
                    help="onset backend to replay through (default: rms)")
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

    events = analyse(audio, sr, cfg, backend=args.backend)
    report = build_report(events, duration_s=len(audio) / sr, cfg=cfg)
    print(format_summary(report, cfg, suggest(report, cfg)))
    if args.csv:
        write_csv(events, args.csv)
        print(f"[csv] {args.csv}")


if __name__ == "__main__":
    main()
