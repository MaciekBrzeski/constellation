"""CLI entry point for the audio-brain Pi.

Runs ``BeatDetector`` against the local microphone and broadcasts the
state (tier, BPM, beat events) over UDP for one or more worker Pis to
render in lockstep. No display, no animation controller — pure audio.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from pathlib import Path

from constellation.config import load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="constellation-master",
        description="Run the audio brain that broadcasts beat events to workers.",
    )
    parser.add_argument(
        "--config", "-c", type=Path, default=None, help="Config TOML path"
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Broadcast host (overrides config). Defaults to 255.255.255.255.",
    )
    parser.add_argument(
        "--port", type=int, default=None, help="UDP port (overrides config)"
    )
    parser.add_argument(
        "--sensitivity",
        type=float,
        default=None,
        help="Beat detection sensitivity (overrides config)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)

    try:
        from constellation.audio.beat import BeatDetector
        from constellation.audio.network import BeatBroadcaster
    except RuntimeError as exc:
        print(f"Audio error: {exc}", file=sys.stderr)
        sys.exit(1)

    audio_cfg = config.audio
    net_cfg = config.network
    sensitivity = (
        args.sensitivity if args.sensitivity is not None else audio_cfg.sensitivity
    )
    host = args.host if args.host is not None else net_cfg.broadcast_host
    port = args.port if args.port is not None else net_cfg.port

    detector = BeatDetector(
        sensitivity=sensitivity,
        cooldown_ms=audio_cfg.cooldown_ms,
        history_size=audio_cfg.history_size,
        sample_rate=audio_cfg.sample_rate,
        block_size=audio_cfg.block_size,
        noise_gate=audio_cfg.noise_gate,
        energy_window_size=audio_cfg.energy_window_size,
        low_intensity_ratio=audio_cfg.low_intensity_ratio,
        high_intensity_ratio=audio_cfg.high_intensity_ratio,
        tempo_history_size=audio_cfg.tempo_history_size,
        min_noise_floor=audio_cfg.min_noise_floor,
        use_aubio=audio_cfg.use_aubio,
    )
    broadcaster = BeatBroadcaster(detector, host=host, port=port)
    broadcaster.start()

    if detector.aubio_active:
        print("[master] aubio tempo tracker active (phase-locked)")
    else:
        print("[master] aubio unavailable, using RMS onset detection")
    print(f"[master] broadcasting beats to {host}:{port}")

    stop_signal = {"stop": False}

    def _on_signal(signum, _frame) -> None:
        stop_signal["stop"] = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    last_log = 0.0
    last_logged_beat_ms = -1.0
    try:
        while not stop_signal["stop"]:
            time.sleep(0.1)
            now = time.monotonic()
            beat_ms = detector.last_beat_audio_ms
            if beat_ms > last_logged_beat_ms:
                last_logged_beat_ms = beat_ms
                print(
                    f"[master] beat tier={detector.energy_tier} "
                    f"bpm={detector.bpm:.1f} strength={detector.last_beat_strength:.2f}",
                    flush=True,
                )
            elif now - last_log > 5.0:
                last_log = now
                print(
                    f"[master] tier={detector.energy_tier} bpm={detector.bpm:.1f}",
                    flush=True,
                )
    finally:
        broadcaster.stop()
        print("[master] stopped", flush=True)


if __name__ == "__main__":
    main()
