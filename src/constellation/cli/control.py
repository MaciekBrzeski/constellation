"""CLI entry point for the native desktop control panel.

Runs the brain — local-mic ``BeatDetector`` + UDP ``BeatBroadcaster`` +
worker ``ControlChannel`` — behind a native Tkinter window
(``constellation.desktop.app``). The audio + networking layers are identical
to ``constellation-master``; only the front end differs (native window
instead of the FastAPI web dashboard).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from constellation.config import load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="constellation-control",
        description="Native desktop control panel for the constellation brain.",
    )
    parser.add_argument(
        "--config", "-c", type=Path, default=None, help="Config TOML path"
    )
    parser.add_argument(
        "--host", default=None, help="Beat broadcast host (overrides config)"
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="Beat broadcast UDP port (overrides config)",
    )
    return parser


def _resolve_device(name: str | None) -> int | None:
    """Resolve a saved device *name* to a PortAudio index.

    Indices are unstable across reboot / re-plug, so config stores the name.
    Returns ``None`` (system default) when the device isn't present.
    """
    if not name:
        return None
    from constellation.audio.beat import list_input_devices

    for idx, dev_name in list_input_devices():
        if dev_name == name:
            return idx
    return None


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)

    try:
        from constellation.desktop.app import run as run_gui
    except ImportError as exc:
        print(
            f"The desktop control panel needs Tkinter ({exc}).\n"
            "Install it with: sudo apt install python3-tk",
            file=sys.stderr,
        )
        sys.exit(1)

    from constellation.audio.beat import BeatDetector
    from constellation.audio.network import BeatBroadcaster
    from constellation.control.master import ControlChannel

    audio_cfg = config.audio
    net_cfg = config.network
    host = args.host if args.host is not None else net_cfg.broadcast_host
    port = args.port if args.port is not None else net_cfg.port

    detector = BeatDetector(
        sensitivity=audio_cfg.sensitivity,
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
        device=_resolve_device(audio_cfg.device),
    )

    audio_ok = True
    try:
        detector.start()
    except RuntimeError as exc:
        # No mic / no libportaudio2 — still launch the GUI so the user can
        # recover from the Microphone tab once the device is available.
        print(f"[control] audio unavailable: {exc}", file=sys.stderr)
        audio_ok = False

    broadcaster = BeatBroadcaster(detector, host=host, port=port)
    broadcaster.start()
    control = ControlChannel()
    control.start()

    print(f"[control] broadcasting beats to {host}:{port}")
    print("[control] control plane listening for heartbeats on port 49501")

    try:
        run_gui(detector, broadcaster, control, config, audio_ok)
    finally:
        broadcaster.stop()
        control.stop()
        detector.stop()  # idempotent — safe whether or not a stream is open
        print("[control] stopped")


if __name__ == "__main__":
    main()
