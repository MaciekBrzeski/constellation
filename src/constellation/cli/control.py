"""CLI entry point for the native desktop control panel.

The brain — local-mic ``BeatDetector`` + UDP ``BeatBroadcaster`` — runs in a
**child process** (:class:`~constellation.control.brain.BrainServer`), driven
over localhost UDP. PortAudio's audio threads crash intermittently when sharing
a process with Tk/X11 on the L4T/Tegra box, so the Tkinter panel keeps only the
GUI and the (socket-only) worker ``ControlChannel``; all audio lives in the
child. The headless ``constellation-master`` runs the same brain in-process.
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

    from constellation.control.brain import BrainClient
    from constellation.control.master import ControlChannel

    net_cfg = config.network
    host = args.host if args.host is not None else net_cfg.broadcast_host
    port = args.port if args.port is not None else net_cfg.port

    # Audio runs out-of-process; the panel only talks UDP to it.
    brain = BrainClient(config, args.config, host=host, port=port)
    brain.start()
    control = ControlChannel()
    control.start()

    print(f"[control] brain child spawned, broadcasting beats to {host}:{port}")
    print("[control] control plane listening for heartbeats on port 49501")

    try:
        run_gui(brain, control, config)
    finally:
        control.stop()
        brain.stop()
        print("[control] stopped")


if __name__ == "__main__":
    main()
