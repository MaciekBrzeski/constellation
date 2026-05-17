"""CLI entry point for running the constellation display."""

from __future__ import annotations

import argparse
import socket
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, cast

import pygame

if TYPE_CHECKING:
    from constellation.audio.network import BeatReceiver

from constellation.animation.controller import AnimationController
from constellation.animation.timing import FrameClock
from constellation.config import load_config
from constellation.geometry.edges import Edge
from constellation.geometry.stars import generate_background_stars, update_twinkle
from constellation.geometry.vertices import scale_vertices
from constellation.rendering.display import cleanup, create_display
from constellation.rendering.drawing import draw_frame

# Number keys mapped to indices 0-9
_KEY_TO_INDEX = {
    pygame.K_1: 0,
    pygame.K_2: 1,
    pygame.K_3: 2,
    pygame.K_4: 3,
    pygame.K_5: 4,
    pygame.K_6: 5,
    pygame.K_7: 6,
    pygame.K_8: 7,
    pygame.K_9: 8,
    pygame.K_0: 9,
}


def _get_available_actions(controller: AnimationController) -> list[str]:
    """Return list of action names for the current animal."""
    name = controller.current_animal_name
    if name is None:
        return []
    animal = controller._animals.get(name)
    if animal is None:
        return []
    return [a.action for a in animal.animations]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="constellation-run",
        description="Run the constellation animation display.",
    )
    parser.add_argument("--config", "-c", type=Path, default=None, help="Config TOML path")
    parser.add_argument(
        "--mode",
        choices=["windowed", "fullscreen"],
        default=None,
        help="Display mode (overrides config)",
    )
    parser.add_argument("--animal", "-a", default=None, help="Start with this animal")
    parser.add_argument(
        "--assets", type=Path, default=None, help="Assets directory (overrides config)"
    )
    audio_group = parser.add_mutually_exclusive_group()
    audio_group.add_argument(
        "--audio",
        action="store_true",
        default=False,
        help="Enable beat-driven animation from mic",
    )
    audio_group.add_argument(
        "--remote-beat",
        default=None,
        metavar="HOST:PORT",
        help=(
            "Listen for beat events on UDP HOST:PORT instead of using a "
            "local mic. Use 0.0.0.0 to bind all interfaces."
        ),
    )
    parser.add_argument(
        "--sensitivity",
        type=float,
        default=None,
        help="Beat detection sensitivity (overrides config, default 1.5)",
    )
    parser.add_argument(
        "--worker-id",
        default=None,
        help=(
            "Identifier this worker reports in heartbeats (defaults to hostname). "
            "Only used when --remote-beat is set."
        ),
    )
    parser.add_argument(
        "--master-host",
        default="255.255.255.255",
        help=(
            "Where to send heartbeats. Default: 255.255.255.255 (broadcast). "
            "Set to a specific IP to bypass broadcast on routed networks."
        ),
    )
    parser.add_argument(
        "--debug-sync",
        action="store_true",
        default=False,
        help="Log beat counter / frame index / clock offset (worker mode).",
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)

    # Set up optional beat detector (local mic) or remote receiver (network)
    beat_detector = None
    audio_behavior = None
    if args.audio:
        try:
            from constellation.audio.beat import BeatDetector
        except RuntimeError as exc:
            print(f"Audio error: {exc}", file=sys.stderr)
            sys.exit(1)

        audio_cfg = config.audio
        sensitivity = args.sensitivity if args.sensitivity is not None else audio_cfg.sensitivity
        beat_detector = BeatDetector(
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
        beat_detector.start()
        print(f"[audio] {beat_detector.backend_name} onset backend active")
    elif args.remote_beat is not None:
        from constellation.audio.network import BeatReceiver, DEFAULT_PORT

        host, _, port_s = args.remote_beat.partition(":")
        port = int(port_s) if port_s else DEFAULT_PORT
        bind_host = host if host else config.network.bind_host
        beat_detector = BeatReceiver(
            port=port,
            bind_host=bind_host,
            render_lead_ms=config.network.render_lead_ms,
            offset_window_size=config.network.offset_window_size,
        )
        beat_detector.start()
        print(f"[audio] remote beat receiver active on {bind_host}:{port}")

    # Authoritative beat-counter sync applies only to remote (worker) mode.
    # predictive_sync=False reverts to the legacy advance-on-packet path.
    remote_sync = args.remote_beat is not None and config.network.predictive_sync

    assets_dir = Path(args.assets) if args.assets else Path(config.assets_dir)
    controller = AnimationController(config.animation, assets_dir)
    if args.animal and args.animal in controller.animal_names:
        idx = controller.animal_names.index(args.animal)
        controller._animal_index = idx

    # Create audio-reactive behavior after controller is set up
    if beat_detector is not None:
        from constellation.animation.behavior import AudioReactiveBehavior

        audio_behavior = AudioReactiveBehavior(
            beat_detector=beat_detector,
            controller=controller,
            action_rotate_min_sec=config.animation.action_rotate_min_sec,
            action_rotate_max_sec=config.animation.action_rotate_max_sec,
            tempo_rotate_factor=config.animation.tempo_rotate_factor,
            strong_beat_threshold=config.animation.strong_beat_threshold,
            strong_hold_sec=config.animation.strong_hold_sec,
        )

    # Worker control plane — heartbeat to master + receive remote commands.
    worker_control = None
    if args.remote_beat is not None:
        from constellation.control.worker import WorkerControl

        worker_id = args.worker_id or socket.gethostname()
        worker_control = WorkerControl(
            worker_id=worker_id,
            controller=controller,
            beat_source=beat_detector,
            master_host=args.master_host,
        )
        worker_control.start()
        print(f"[control] worker_id={worker_id} heartbeat → {args.master_host}:49501")

    surface = create_display(config)
    display_size = surface.get_size()
    clock = FrameClock(config.display.fps)
    bg_stars = generate_background_stars(display_size, config.render.bg_star_count)

    # Sprite animation sub-clocks. ``sprite_tick_interval`` is the normal rate;
    # ``idle_tick_interval`` applies only in audio mode when the detector says
    # silence, giving the idle animation a slower, statelier pace.
    sprite_tick_interval = 1000.0 / config.animation.target_fps
    idle_tick_interval = 1000.0 / config.animation.idle_fps
    sprite_accumulator = 0.0

    # Beat pulse: 0.0–1.0, spikes on beat and decays each frame
    beat_pulse = 0.0
    # Decay rate: fraction lost per second (reaches ~0 in ~150 ms)
    beat_decay_rate = 8.0

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_q, pygame.K_ESCAPE):
                    running = False
                elif event.key == pygame.K_SPACE:
                    controller.next_animal()
                elif event.key in _KEY_TO_INDEX:
                    actions = _get_available_actions(controller)
                    idx = _KEY_TO_INDEX[event.key]
                    if idx < len(actions):
                        controller.set_action(actions[idx])

        # Apply any pending remote commands (set_animal / set_action) from
        # the master Pi before behavior runs, so a manual override takes
        # effect on this frame instead of being clobbered by the next tier
        # rotation.
        if worker_control is not None:
            worker_control.apply_pending()

        # Audio-reactive behavior update
        if audio_behavior is not None:
            audio_behavior.update()

        # Advance sprite animation
        dt = clock.tick()
        if remote_sync:
            # Worker mode: the frame index is the brain's authoritative beat
            # counter, pre-rolled so the flip lands on the beat. Drift- and
            # packet-loss-proof — a missed packet self-heals on the next one.
            # remote_sync ⇒ beat_detector is a BeatReceiver (see setup above).
            receiver = cast("BeatReceiver", beat_detector)
            frame_idx = receiver.consume_frame_index()
            if frame_idx is not None:
                controller.advance_to_beat(frame_idx)
                beat_pulse = 1.0
                if args.debug_sync:
                    print(
                        f"[sync] beat_count={receiver.beat_count} "
                        f"frame={controller._frame_index} "
                        f"offset_ms={receiver.clock_offset_ms:.1f}",
                        flush=True,
                    )
            elif time.monotonic() - receiver.last_beat_time > 2.0:
                # No packets from the brain — free-run the idle animation so
                # the deer isn't frozen. While the brain is up but the room
                # is quiet, hold on the scheduler index instead, keeping the
                # two workers phase-locked.
                sprite_accumulator += dt
                while sprite_accumulator >= idle_tick_interval:
                    sprite_accumulator -= idle_tick_interval
                    controller.advance_frame()
        elif beat_detector is not None:
            if beat_detector.beat_event.is_set():
                # Beat-driven: advance frame on each detected beat
                beat_detector.beat_event.clear()
                controller.advance_frame()
                beat_pulse = 1.0
            elif audio_behavior is not None and not audio_behavior.active:
                # Silent: fall back to time-based frame advancement at the
                # slower idle rate so the resting animation reads as calm.
                sprite_accumulator += dt
                while sprite_accumulator >= idle_tick_interval:
                    sprite_accumulator -= idle_tick_interval
                    controller.advance_frame()
        else:
            # No audio — time-based mode (original behavior)
            sprite_accumulator += dt
            while sprite_accumulator >= sprite_tick_interval:
                sprite_accumulator -= sprite_tick_interval
                controller.advance_frame()

        # Decay beat pulse
        if beat_pulse > 0.0:
            beat_pulse = max(0.0, beat_pulse - beat_decay_rate * (dt / 1000.0))

        # Get current vertices and scale to display. Tween only in time-based
        # mode — beat mode stays snappy so hits still feel punchy.
        if beat_detector is None:
            tween_t = min(
                1.0,
                max(0.0, sprite_accumulator / sprite_tick_interval),
            )
            raw_verts = controller.current_vertices_tween(tween_t)
        else:
            raw_verts = controller.current_vertices()
        if raw_verts:
            name = controller.current_animal_name
            animal_data = controller._animals.get(name, None) if name else None
            source_size = animal_data.frame_size if animal_data else (32, 32)
            verts = scale_vertices(raw_verts, source_size, display_size)
        else:
            verts = []

        # Use stored contour edges if available, else fall back to distance-based
        stored = controller.current_edges()
        edges = []
        for pair in stored:
            i, j = pair[0], pair[1]
            if i < len(verts) and j < len(verts):
                dx = verts[i][0] - verts[j][0]
                dy = verts[i][1] - verts[j][1]
                dist = (dx * dx + dy * dy) ** 0.5
                edges.append(Edge(i=i, j=j, distance=dist, width=config.render.line_max_width))

        star_indices = controller.current_stars()

        update_twinkle(
            bg_stars,
            probability=config.render.twinkle_probability,
            min_alpha=config.render.twinkle_min_alpha,
        )
        draw_frame(
            surface,
            verts,
            edges,
            bg_stars,
            config.render,
            beat_pulse=beat_pulse,
            star_indices=star_indices,
        )

    if worker_control is not None:
        worker_control.stop()
    if beat_detector is not None:
        beat_detector.stop()
    cleanup()
    sys.exit(0)
