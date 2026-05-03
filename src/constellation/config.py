"""Configuration loading and dataclasses."""

from __future__ import annotations

try:
    import tomllib
except ImportError:  # Python < 3.11 fallback
    import tomli as tomllib  # type: ignore[no-redef]
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class DisplayConfig:
    width: int = 800
    height: int = 600
    fps: int = 60
    bg_color: tuple[int, int, int] = (0, 0, 0)
    title: str = "Constellation"


@dataclass(frozen=True)
class PreprocessConfig:
    epsilon_pct: float = 0.01
    contour_max_edge: float = 2.0
    min_contour_area: int = 50
    binarize_threshold: int = 128
    frame_width: int = 32
    frame_height: int = 32
    dither_grid: int = 8
    corner_angle_threshold: float = 120.0
    corner_neighbor_distance: int = 3


@dataclass(frozen=True)
class RenderConfig:
    d_max: float = 0.55
    d_min: float = 0.05
    line_max_width: int = 1
    star_radius_min: int = 1
    star_radius_max: int = 4
    star_color: tuple[int, int, int] = (255, 255, 255)
    line_color: tuple[int, int, int] = (255, 255, 255)
    line_alpha: float = 0.25
    bg_star_count: int = 20
    twinkle_probability: float = 0.08
    twinkle_min_alpha: float = 0.3


@dataclass(frozen=True)
class AnimationConfig:
    target_fps: int = 12
    idle_fps: int = 4
    transition_frames: int = 15
    idle_timeout_sec: float = 30.0
    animals: tuple[str, ...] = (
        "turkey", "rooster", "chick", "bull", "calf", "sheep", "lamb", "piglet",
    )
    default_action: str = "idle"
    action_rotate_min_sec: float = 8.0
    action_rotate_max_sec: float = 20.0
    tempo_rotate_factor: float = 1.0
    strong_beat_threshold: float = 4.0
    strong_hold_sec: float = 2.5


@dataclass(frozen=True)
class AudioConfig:
    sensitivity: float = 1.5
    cooldown_ms: float = 200.0
    history_size: int = 43
    sample_rate: int = 22050
    block_size: int = 1024
    silence_timeout_sec: float = 5.0
    noise_gate: float = 0.01
    energy_window_size: int = 200
    low_intensity_ratio: float = 2.0
    high_intensity_ratio: float = 6.0
    tempo_history_size: int = 8
    min_noise_floor: float = 0.002
    use_aubio: bool = True


@dataclass(frozen=True)
class AppConfig:
    mode: str = "windowed"
    assets_dir: str = "assets"
    display: DisplayConfig = field(default_factory=DisplayConfig)
    preprocess: PreprocessConfig = field(default_factory=PreprocessConfig)
    render: RenderConfig = field(default_factory=RenderConfig)
    animation: AnimationConfig = field(default_factory=AnimationConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)


def _parse_color(val: list[int]) -> tuple[int, int, int]:
    return (val[0], val[1], val[2])


def load_config(path: Path | None = None) -> AppConfig:
    if path is None:
        path = Path("config/default.toml")

    if not path.exists():
        return AppConfig()

    with open(path, "rb") as f:
        raw = tomllib.load(f)

    display_raw = raw.get("display", {})
    if "bg_color" in display_raw:
        display_raw["bg_color"] = _parse_color(display_raw["bg_color"])
    display = DisplayConfig(**display_raw)

    preprocess = PreprocessConfig(**raw.get("preprocess", {}))

    render_raw = raw.get("render", {})
    if "star_color" in render_raw:
        render_raw["star_color"] = _parse_color(render_raw["star_color"])
    if "line_color" in render_raw:
        render_raw["line_color"] = _parse_color(render_raw["line_color"])
    render = RenderConfig(**render_raw)

    anim_raw = raw.get("animation", {})
    if "animals" in anim_raw:
        anim_raw["animals"] = tuple(anim_raw["animals"])
    animation = AnimationConfig(**anim_raw)

    audio = AudioConfig(**raw.get("audio", {}))

    return AppConfig(
        mode=raw.get("mode", "windowed"),
        assets_dir=raw.get("assets_dir", "assets"),
        display=display,
        preprocess=preprocess,
        render=render,
        animation=animation,
        audio=audio,
    )
