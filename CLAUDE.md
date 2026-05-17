# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`constellation` — Python package (v0.1.0, requires Python ≥3.11) that turns pixel-art animal spritesheets into kinetic, optionally beat-reactive constellation animations rendered via pygame. Designed to deploy on a Raspberry Pi Zero 2 W driving an HDMI projector; the preprocessing toolchain runs on the dev host, the Pi ships only precompiled JSON + pure-Python runtime.

Four CLI entry points (console scripts when pip-installed, see `pyproject.toml`):

- `constellation-preprocess` → `constellation.cli.preprocess:main` — converts PNG spritesheets into JSON vertex/edge data.
- `constellation-run` → `constellation.cli.run:main` — runs the live pygame display.
- `constellation-master` → `constellation.cli.master:main` — runs the audio brain (mic → beat detection → UDP broadcast), optionally with a FastAPI web dashboard (`--gui`).
- `constellation-control` → `constellation.cli.control:main` — same brain behind a native Tkinter window instead of the web dashboard.

## Repo notes

The working copy is self-contained: `pyproject.toml`, `README.md`, `config/default.toml`, `src/constellation/config.py`, and all `tests/*.py` are present. (Earlier backups of this tree were missing those — no longer the case.)

- `git` works (`origin` → `https://github.com/MaciekBrzeski/constellation.git`). HTTPS push needs `gh auth setup-git` first; GitHub rejects password auth.
- The `.venv/` was editable-installed from a different host path (`/home/grayskull/Dokumenty/constellation`) — paths baked into the install metadata may not match this checkout. Re-run `pip install -e '.[audio,dev]'` if imports misbehave.
- `assets/constellations/` (precompiled JSON) and `assets/spritesheets/` (source PNGs) may be empty in a fresh clone.

## Commands

The venv is at `.venv/`, package installed editable. Activate it (`source .venv/bin/activate.fish` — shell is fish) before running anything. Install extras: `pip install -e '.[audio,dev]'` (add `gui` for the web dashboard).

- Run display: `constellation-run [--config PATH] [--mode windowed|fullscreen] [--animal NAME] [--assets DIR] [--audio] [--sensitivity FLOAT] [--remote-beat HOST:PORT] [--master-host HOST]`
- Run brain: `constellation-master [--host HOST] [--gui] [--web-host HOST] [--web-port PORT]`
- Run desktop panel: `constellation-control [--config PATH] [--host HOST] [--port PORT]` (or `./scripts/start-control.sh`)
- Preprocess single sheet: `constellation-preprocess -i sheet.png -a deer_male --actions idle walk run -o assets/constellations`
- Preprocess per-action dir (filenames like `Male_Alert.png` → action `alert`): `constellation-preprocess -d sprites/deer_male -a deer_male`
- Tests: `pytest` (31 tests across all subsystems) or `pytest tests/audio/test_beat.py::test_name` for one.
- Lint / type-check: `ruff check src tests` and `mypy src`.
- Audio tuning: `python scripts/audio_diag.py record --seconds 30 --out tmp/s.wav` / `python scripts/audio_diag.py replay tmp/s.wav --sensitivity 1.5`.

Runtime keys in the display window: `Q`/`Esc` quit, `Space` cycles animals, `1`–`0` pick action by index.

## Architecture

Two pipelines share the `AppConfig` TOML (`config/default.toml`, loaded via `constellation.config.load_config`).

### Preprocessing pipeline — `src/constellation/preprocessing/` + `cli/preprocess.py`

PNG spritesheet → grayscale/alpha frames → binary mask → external contours → corner-detected + arc-length resampled polygon → JSON per animal.

1. `spritesheet.load_spritesheet` / `slice_frames` / `frames_to_arrays` — load RGBA PNG, slice a row into per-frame images, convert each to grayscale. **For RGBA inputs the alpha channel is used as grayscale** (silhouette matters, not color).
2. `binarize.binarize` + `extract_contours` — threshold to binary, `cv2.findContours(RETR_EXTERNAL, CHAIN_APPROX_NONE)`, filter by `min_area`.
3. `simplify.detect_corners` — curvature-based corner detection with non-max suppression, then `resample_contour` walks the contour and emits vertices at `max_edge_len` intervals while force-emitting corner indices. Closed loop: edges form a ring `[k, (k+1) % n]` per contour.
4. `export.export_animal` writes `AnimalData(name, frame_size, animations=[AnimationData(action, frames=[FrameData(vertices, edges)])])` to `assets/constellations/<name>.json`. Multiple contours per frame are concatenated with rebased edge indices.

`dither.dither_interior_points` exists but is **not wired into the pipeline** — Poisson-disk interior sampling that would supplement contour vertices; preserve it if refactoring.

### Runtime pipeline — `src/constellation/animation/`, `audio/`, `rendering/` + `cli/run.py`

Main loop in `cli/run.py` ties everything together:

- `AnimationController` (`animation/controller.py`) — loads every JSON in `assets/constellations/` (configured animals first, then alphabetic discovery), owns `_animal_index`, `_action`, `_frame_index`. Exposes `current_vertices()`, `current_edges()`, `advance_frame()`, `next_animal()`, `set_action()`. Supports linear tweening between adjacent frames. Falls back to the first available animation when the requested action is missing.
- `FrameClock` (`animation/timing.py`) — thin pygame `Clock` wrapper returning `dt` ms per `tick()` at `config.display.fps`.
- **Frame advancement has two modes** driven off the same `dt`:
  - No audio → `sprite_accumulator += dt`; emits one frame per `1000 / animation.target_fps` ms.
  - With audio → each `beat_detector.beat_event` fires `controller.advance_frame()` + spikes `beat_pulse` to 1.0; silent periods fall back to the time-based accumulator.
- `BeatDetector` (`audio/beat.py`) — runs `sounddevice.InputStream` on a background thread (input device selectable via the `device` arg/config). Selectable onset backend (`AudioConfig.backend`), built in `_build_backend()` on `start()`:
  - **multiband** (shipped default) — `audio/onset.py` `MultibandSpectralFluxDetector`: numpy STFT, bass/mid/high spectral flux against a `median + k·std` threshold with a rising-edge filter, kick-locked tempo. No system deps.
  - **aubio** — `aubio.tempo` autocorrelation tracker; used when selected and `import aubio` succeeds.
  - **rms** — beat when `rms > rolling_mean * sensitivity` past cooldown.
  - **auto** — aubio (if `use_aubio` + installed) → multiband → rms.
  All backends feed `_process_energy(rms, ext_beat=, ext_bpm=)` (renamed from `aubio_beat`/`aubio_bpm`); tier/noise-floor/strength logic is backend-independent. `BeatDetector` also exposes `beat_count`/`epoch`/`predicted_next_beat_ms` for network sync. Tracks an adaptive noise floor (p10 of recent block RMS) and classifies energy into `low`/`medium`/`high` tiers via `low_intensity_ratio`/`high_intensity_ratio`. Requires the `[audio]` extra; raises `RuntimeError` with an install hint if `sounddevice` or `libportaudio2` is missing. `list_input_devices()` enumerates capture devices without raising.
- `AudioReactiveBehavior` (`animation/behavior.py`) — sits between detector and controller, **not a subclass**. On each `update()` picks actions by tier:
  - `low` → `idle`, `eating`
  - `medium` → `walk`, `alert`
  - `high` → `run`, `jump`
  Rotates action every `action_rotate_min_sec`–`action_rotate_max_sec`, scaled by `(120 / bpm) ** tempo_rotate_factor` clamped to `[0.25, 4.0]`. A strong-beat lockout (`strong_beat_threshold`, `strong_hold_sec`) holds the high tier across a burst. Only sets an action the current animal actually defines.
- Rendering — `rendering/display.create_display` inits pygame; `rendering/drawing.draw_frame` clears to black, draws twinkling background stars (`geometry/stars.py`), then semi-transparent edges, then vertex star glyphs. Glyphs are cached per `(radius, sparkle, color)` and blitted with `set_alpha`. Per-vertex radii cached + reseeded only when vertex count changes via `_ensure_star_radii`, seeded with `Random(42)`. `beat_pulse` boosts edge alpha and star radii up to 2× and forces sparkle spikes.
- `geometry/vertices.scale_vertices` maps source sprite coords to display coords with letterboxing + padding. `geometry/edges.compute_edges` (distance-thresholded pairwise edges) is a **fallback** — normally `controller.current_edges()` returns stored contour rings.

### Multi-projector control plane — `audio/network.py`, `control/`, `web/`, `desktop/`

Scales beyond one projector. Three roles (`NetworkConfig.mode`): `standalone` (mic + display, default), `master`/`brain` (mic only), `worker` (display only, network-driven).

- `audio/network.py` — `BeatBroadcaster` sends state packets over UDP at 50 Hz (default broadcast `255.255.255.255:49500`); each packet carries an authoritative `beat_count`, `brain_ms` clock, predicted next-beat time and `epoch`. `BeatReceiver` (bound `0.0.0.0:49500`) is a drop-in `BeatDetector` replacement.
- `audio/sync.py` — `BeatScheduler` (owned by `BeatReceiver`): turns broadcast packets into an authoritative local frame index. Rolling-min brain→local clock offset, snaps to `beat_count` (packet-loss self-heals), and predictively pre-rolls the frame flip onto the beat. `controller.advance_to_beat(beat_count)` applies it via a per-action anchor so two workers stay frame-locked while running different actions. Worker frame advance lives in `run.py` under the `remote_sync` flag (`NetworkConfig.predictive_sync`, `--debug-sync` to log it).
- `control/` — UDP control plane separate from the beat stream: `protocol.py` (message encoding), `master.py` `ControlChannel` (tracks workers, sends animal/action commands, listens for 1 Hz heartbeats on port 49501), `worker.py` (worker-side control + heartbeat).
- `web/` — `app.py` FastAPI dashboard + `static/` single-page UI (WebSocket, 10 Hz updates); served by `constellation-master --gui`. Needs the `[gui]` extra.
- `desktop/app.py` — native Tkinter five-tab control panel (Status, Microphone, Tuning, Calibration, Workers); same brain as the web dashboard, no browser. Needs `python3-tk`.
- `audio/calibration.py` — record/replay audio through `BeatDetector` offline, build a `CalibrationReport`, and `suggest()` tuned `sensitivity`/`noise_gate`. Shared by `scripts/audio_diag.py` and the desktop Calibration tab.

### Config — `src/constellation/config.py` + `config/default.toml`

`load_config(path: Path | None) -> AppConfig`. Frozen dataclasses; TOML keys map by field name. `AppConfig`: `mode`, `assets_dir`, `display`, `preprocess`, `render`, `animation`, `audio`, `network`.

- `DisplayConfig`: `width`, `height`, `fps`, `bg_color`, `title`.
- `PreprocessConfig`: `epsilon_pct`, `contour_max_edge`, `min_contour_area`, `binarize_threshold`, `frame_width`, `frame_height`, `dither_grid`, `corner_angle_threshold`, `corner_neighbor_distance`.
- `RenderConfig`: `d_max`, `d_min`, `line_max_width`, `star_radius_min/max`, `star_color`, `line_color`, `line_alpha`, `bg_star_count`, `twinkle_probability`, `twinkle_min_alpha`.
- `AnimationConfig`: `target_fps`, `idle_fps`, `transition_frames`, `idle_timeout_sec`, `animals`, `default_action`, `action_rotate_min/max_sec`, `tempo_rotate_factor`, `strong_beat_threshold`, `strong_hold_sec`.
- `AudioConfig`: `sensitivity`, `cooldown_ms`, `history_size`, `sample_rate`, `block_size`, `silence_timeout_sec`, `noise_gate`, `energy_window_size`, `low_intensity_ratio`, `high_intensity_ratio`, `tempo_history_size`, `min_noise_floor`, `use_aubio`, `device`, `backend`, `fft_size`, `flux_history_size`, `bass/mid/high_sensitivity`, `bass_max_hz`, `high_min_hz`, `tempo_min/max_bpm`.
- `NetworkConfig`: `mode`, `broadcast_host`, `bind_host`, `port`, `ap_mode`, `ap_ssid`, `ap_interface`, `ap_address`, `render_lead_ms`, `offset_window_size`, `predictive_sync`.

**Per-user overlay**: `load_config` deep-merges `~/.config/constellation/config.toml` (`user_config_path()`) over `config/default.toml` for the `[audio]` and `[network]` sections only — machine-specific tuning (chosen mic, calibrated values) lives there and survives without touching the version-controlled default. `save_audio_overrides()` writes the `[audio]` section back atomically; a `None` `device` is omitted (TOML has no null).

## Gotchas

- `AudioReactiveBehavior._set_action_if_available` and `cli/run.py._get_available_actions` both reach into `AnimationController._animals` directly. Renaming `_animals` must update both.
- `drawing._ensure_star_radii` uses module-level state — radii reseed only when `len(verts)` changes. Switching between animals with the same vertex count keeps the old twinkle pattern.
- `BeatDetector._last_beat_time` is stored in **milliseconds** but `last_beat_time` returns seconds — don't compare the raw field against `time.monotonic()`.
- `preprocessing/simplify.resample_contour` has a subtle reparameterisation inside its `while remaining > 0` loop (`px/py` formulas) — tread carefully when editing.
- `AudioConfig.device` stores the device **name**, not index — PortAudio indices shift across re-plug. `cli/control._resolve_device` / `BeatDetector` resolve name → index at start.
- The desktop panel's Tk `mainloop` and all widget access stay on the main thread; detector/broadcaster/control run their own threads, bridged by a 10 Hz `root.after` poll.
