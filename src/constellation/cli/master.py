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
    parser.add_argument(
        "--gui",
        action="store_true",
        default=False,
        help="Serve the browser control dashboard (requires the [gui] extra)",
    )
    parser.add_argument(
        "--web-host",
        default="0.0.0.0",
        help="Bind host for the web UI (default: all interfaces)",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=8000,
        help="Port for the web UI (default: 8000)",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)

    try:
        from constellation.audio.beat import BeatDetector
        from constellation.audio.network import BeatBroadcaster
        from constellation.control.master import ControlChannel
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
        backend=audio_cfg.backend,
        fft_size=audio_cfg.fft_size,
        flux_history_size=audio_cfg.flux_history_size,
        bass_sensitivity=audio_cfg.bass_sensitivity,
        mid_sensitivity=audio_cfg.mid_sensitivity,
        high_sensitivity=audio_cfg.high_sensitivity,
        bass_max_hz=audio_cfg.bass_max_hz,
        high_min_hz=audio_cfg.high_min_hz,
        tempo_min_bpm=audio_cfg.tempo_min_bpm,
        tempo_max_bpm=audio_cfg.tempo_max_bpm,
    )
    broadcaster = BeatBroadcaster(detector, host=host, port=port)
    broadcaster.start()
    control = ControlChannel()
    control.start()

    print(f"[master] {detector.backend_name} onset backend active")
    print(f"[master] broadcasting beats to {host}:{port}")
    print("[master] control plane listening for heartbeats on port 49501")

    stop_signal = {"stop": False}

    def _on_signal(signum, _frame) -> None:
        stop_signal["stop"] = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    if args.gui:
        try:
            import uvicorn

            from constellation.web.app import create_app
        except ImportError:
            print(
                "Web UI requires the [gui] extra. Install with: "
                "pip install 'constellation[gui]'",
                file=sys.stderr,
            )
            broadcaster.stop()
            control.stop()
            sys.exit(1)
        app = create_app(detector, control)
        print(
            f"[master] web UI on http://{args.web_host}:{args.web_port}/  (Ctrl+C to stop)",
            flush=True,
        )
        try:
            uvicorn.run(
                app, host=args.web_host, port=args.web_port, log_level="warning"
            )
        finally:
            broadcaster.stop()
            control.stop()
            print("[master] stopped", flush=True)
        return

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
        control.stop()
        print("[master] stopped", flush=True)


if __name__ == "__main__":
    main()
