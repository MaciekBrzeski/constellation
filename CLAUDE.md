# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

`constellation` — Python package (v0.1.0, requires Python ≥3.11) that turns pixel-art animal spritesheets into kinetic, optionally beat-reactive constellation animations rendered via pygame.

Two CLI entry points (installed as console scripts when the package is pip-installed):

- `constellation-preprocess` → `constellation.cli.preprocess:main` — converts PNG spritesheets into JSON vertex/edge data.
- `constellation-run` → `constellation.cli.run:main` — runs the live pygame display.

## Repo state warning

This working copy is a partial backup and is **not self-contained**:

- No `pyproject.toml`, `README`, `.gitignore`, or `pytest.ini` at the root.
- `.git/` has `objects/` + `refs/` only — no `HEAD` or `config`, so git commands fail.
- `src/constellation/config.py` and `src/constellation/__main__.py` exist **only as `.pyc`** under `__pycache__/` — source `.py` files are missing but imported everywhere (`from constellation.config import load_config, AppConfig, AnimationConfig, RenderConfig`).
- `tests/*/` contain only `.pyc` files (no `.py`). Pytest cache shows they were run with pytest 9.0.2 under Python 3.13.
- `config/` and `.claude/` dirs are empty.
- `assets/constellations/` and `assets/spritesheets/` dirs exist but are empty.

The installed venv at `.venv/` was editable-installed from `/home/grayskull/Dokumenty/constellation` (different host path). Before doing real work, reconstruct `config.py` / `pyproject.toml` / tests, or fetch them from the source repo — do not assume missing files mean they were intentionally removed.

## Commands

The venv is at `.venv/` and has the package installed in editable mode. Activate it (`source .venv/bin/activate.fish` — shell is fish) before running anything.

- Run display: `constellation-run [--config PATH] [--mode windowed|fullscreen] [--animal NAME] [--assets DIR] [--audio] [--sensitivity FLOAT]`
- Preprocess single sheet: `constellation-preprocess -i sheet.png -a deer_male --actions idle walk run -o assets/constellations`
- Preprocess per-action dir (filenames like `Male_Alert.png` → action `alert`): `constellation-preprocess -d sprites/deer_male -a deer_male`
- Tests (pytest is installed in venv): `pytest` or `pytest tests/audio/test_beat.py::test_name` for a single test. Note: test source files are currently missing from this copy.
- Lint / type-check (dev deps `ruff` + `mypy` installed): `ruff check src tests` and `mypy src`.

Runtime keys in the display window: `Q`/`Esc` quit, `Space` cycles animals, `1`–`0` pick action by index.

## Architecture

Two pipelines share the `AppConfig` TOML (loaded via the missing-source `constellation.config.load_config`):

### Preprocessing pipeline — `src/constellation/preprocessing/` + `cli/preprocess.py`

PNG spritesheet → grayscale/alpha frames → binary mask → external contours → corner-detected + arc-length resampled polygon → JSON per animal.

1. `spritesheet.load_spritesheet` / `slice_frames` / `frames_to_arrays` — load RGBA PNG, slice a row into per-frame images, then convert each to grayscale. **For RGBA inputs, the alpha channel is used as grayscale** (sprite silhouette matters, not color).
2. `binarize.binarize` + `extract_contours` — threshold to binary, `cv2.findContours(RETR_EXTERNAL, CHAIN_APPROX_NONE)`, filter by `min_area`.
3. `simplify.detect_corners` — curvature-based corner detection with non-max suppression, then `resample_contour` walks the contour and emits vertices at `max_edge_len` intervals while force-emitting corner indices. Closed loop: edges form a ring `[k, (k+1) % n]` per contour.
4. `export.export_animal` writes `AnimalData(name, frame_size, animations=[AnimationData(action, frames=[FrameData(vertices, edges)])])` to `assets/constellations/<name>.json`. Multiple contours per frame are concatenated with rebased edge indices.

`dither.dither_interior_points` exists but is **not wired into the pipeline** — Poisson-disk interior sampling that would supplement contour vertices; preserve it if refactoring.

### Runtime pipeline — `src/constellation/animation/`, `audio/`, `rendering/` + `cli/run.py`

Main loop in `cli/run.py` ties everything together:

- `AnimationController` (`animation/controller.py`) — loads every JSON in `assets/constellations/` (configured animals first, then alphabetic discovery), owns `_animal_index`, `_action`, `_frame_index`. Exposes `current_vertices()`, `current_edges()`, `advance_frame()`, `next_animal()`, `set_action()`. Falls back to first available animation when the requested action is missing.
- `FrameClock` (`animation/timing.py`) — thin pygame `Clock` wrapper returning `dt` ms per `tick()` at `config.display.fps`.
- **Frame advancement has two modes** driven off the same `dt`:
  - No audio → `sprite_accumulator += dt`; emits one frame per `1000 / animation.target_fps` ms.
  - With audio → each `beat_detector.beat_event` fires `controller.advance_frame()` + spikes `beat_pulse` to 1.0; silent periods fall back to the time-based accumulator.
- `BeatDetector` (`audio/beat.py`) — runs `sounddevice.InputStream` on a background thread. RMS per block → rolling mean → beat when `rms > rolling_mean * sensitivity` past cooldown. Tracks BPM from recent beat intervals and classifies current energy into `low`/`medium`/`high` tiers from the ratio of rolling-mean to longer smoothed-energy window. Requires the `[audio]` extra (`sounddevice`); raises `RuntimeError` with install hint otherwise.
- `AudioReactiveBehavior` (`animation/behavior.py`) — sits between detector and controller, **not a subclass**. On each `update()`, checks silence timeout, then picks actions by tier:
  - `low` → `idle`, `eating`
  - `medium` → `walk`, `alert`
  - `high` → `run`, `jump`
  Rotates action every `action_rotate_min_sec`–`action_rotate_max_sec`, scaled by `(120 / bpm) ** tempo_rotate_factor` clamped to `[0.25, 4.0]`. Only sets an action if the current animal actually defines it.
- Rendering — `rendering/display.create_display` inits pygame; `rendering/drawing.draw_frame` clears to black, draws twinkling background stars (`geometry/stars.py`), then semi-transparent edges, then vertex stars (per-vertex radii cached and reseeded only when vertex count changes via `_ensure_star_radii`, seeded with `Random(42)` for stability across frames). `beat_pulse` boosts edge alpha and star radii up to 2× and forces sparkle spikes.
- `geometry/vertices.scale_vertices` maps source sprite coords to display coords with letterboxing + padding. `geometry/edges.compute_edges` (distance-thresholded pairwise edges, adaptive to bbox diagonal) is a **fallback** — normally `controller.current_edges()` returns stored contour rings and `run.py` only computes distances for line widths.

### Config surface (reconstruct if restoring `config.py`)

`config.load_config(path: Path | None) -> AppConfig` with nested dataclasses:

- `AppConfig`: `mode`, `assets_dir`, `display: DisplayConfig`, `animation: AnimationConfig`, `render: RenderConfig`, `preprocess: PreprocessConfig`, `audio: AudioConfig`.
- `DisplayConfig`: `width`, `height`, `title`, `fps`.
- `AnimationConfig`: `animals: list[str]`, `default_action`, `target_fps`, `action_rotate_min_sec`, `action_rotate_max_sec`, `tempo_rotate_factor`.
- `RenderConfig`: `bg_star_count`, `twinkle_probability`, `twinkle_min_alpha`, `star_color`, `line_color`, `line_alpha`, `star_radius_min`, `star_radius_max`, `line_max_width`.
- `PreprocessConfig`: `frame_width`, `frame_height`, `binarize_threshold`, `min_contour_area`, `corner_angle_threshold`, `corner_neighbor_distance`, `contour_max_edge`.
- `AudioConfig`: `sensitivity`, `cooldown_ms`, `history_size`, `sample_rate`, `block_size`, `noise_gate`, `energy_window_size`, `energy_low_threshold`, `energy_high_threshold`, `tempo_history_size`, `silence_timeout_sec`.

All fields are read by name in the code above — use it as the ground truth when reconstructing.

## Gotchas

- `AudioReactiveBehavior._set_action_if_available` and `cli/run.py._get_available_actions` both reach into `AnimationController._animals` directly. Refactors that rename `_animals` must update both.
- `drawing._ensure_star_radii` uses module-level state — radii reseed only when `len(verts)` changes. Switching between animals with the same vertex count will keep the old pattern.
- `BeatDetector._last_beat_time` is stored in **milliseconds** but `last_beat_time` property returns seconds — don't compare raw `_last_beat_time` against `time.monotonic()`.
- `preprocessing/simplify.resample_contour` has a subtle reparameterisation inside its `while remaining > 0` loop (`px/py` formulas) — tread carefully when editing.
