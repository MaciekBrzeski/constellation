"""Configuration loading and dataclasses."""

from __future__ import annotations

try:
    import tomllib
except ImportError:  # Python < 3.11 fallback
    import tomli as tomllib  # type: ignore[no-redef]
import os
from dataclasses import dataclass, field, fields
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
    # Input device name for the capture stream. ``None`` = system default.
    # Stores the device *name* (not index) — indices shift across re-plug.
    device: str | None = None


@dataclass(frozen=True)
class NetworkConfig:
    mode: str = "standalone"  # "standalone" | "master" | "worker"
    broadcast_host: str = "255.255.255.255"
    bind_host: str = "0.0.0.0"
    port: int = 49500


@dataclass(frozen=True)
class AppConfig:
    mode: str = "windowed"
    assets_dir: str = "assets"
    display: DisplayConfig = field(default_factory=DisplayConfig)
    preprocess: PreprocessConfig = field(default_factory=PreprocessConfig)
    render: RenderConfig = field(default_factory=RenderConfig)
    animation: AnimationConfig = field(default_factory=AnimationConfig)
    audio: AudioConfig = field(default_factory=AudioConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)


def _parse_color(val: list[int]) -> tuple[int, int, int]:
    return (val[0], val[1], val[2])


def user_config_path() -> Path:
    """Path to the per-user config overlay (``~/.config/constellation``).

    Machine-specific tuning (chosen microphone, calibrated detector values)
    is saved here so it survives restarts without churning the
    version-controlled ``config/default.toml``.
    """
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "constellation" / "config.toml"


# Overlay sections deep-merged on top of the base config when present.
_OVERLAY_SECTIONS = ("audio", "network")


def load_config(path: Path | None = None) -> AppConfig:
    if path is None:
        path = Path("config/default.toml")

    raw: dict = {}
    if path.exists():
        with open(path, "rb") as f:
            raw = tomllib.load(f)

    # Merge the per-user overlay (if any) on top of the base config —
    # per-section shallow update, so overlay keys win but unset keys fall
    # back to the base / dataclass defaults.
    overlay_path = user_config_path()
    if overlay_path.exists():
        with open(overlay_path, "rb") as f:
            overlay = tomllib.load(f)
        for section in _OVERLAY_SECTIONS:
            if isinstance(overlay.get(section), dict):
                merged = dict(raw.get(section, {}))
                merged.update(overlay[section])
                raw[section] = merged

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
    network = NetworkConfig(**raw.get("network", {}))

    return AppConfig(
        mode=raw.get("mode", "windowed"),
        assets_dir=raw.get("assets_dir", "assets"),
        display=display,
        preprocess=preprocess,
        render=render,
        animation=animation,
        audio=audio,
        network=network,
    )


def _toml_value(value: object) -> str:
    """Serialise a scalar to a TOML literal. bool/int/float/str only."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    raise TypeError(f"unsupported TOML value: {value!r}")


def _dump_toml(sections: dict[str, dict]) -> str:
    """Render ``{section: {key: scalar}}`` as a flat TOML document."""
    out: list[str] = []
    for name, table in sections.items():
        if not isinstance(table, dict):
            continue
        out.append(f"[{name}]")
        for key, value in table.items():
            out.append(f"{key} = {_toml_value(value)}")
        out.append("")
    return "\n".join(out)


def save_audio_overrides(audio: AudioConfig, path: Path | None = None) -> None:
    """Persist the ``[audio]`` section to the per-user config overlay.

    Reads any existing overlay first so other sections (e.g. ``[network]``)
    are preserved, then atomically rewrites the file. A ``None`` device is
    omitted (TOML has no null literal).
    """
    if path is None:
        path = user_config_path()

    sections: dict[str, dict] = {}
    if path.exists():
        with open(path, "rb") as f:
            sections = tomllib.load(f)

    audio_table: dict[str, object] = {}
    for fld in fields(audio):
        value = getattr(audio, fld.name)
        if value is None:  # e.g. device unset — TOML can't represent null
            continue
        audio_table[fld.name] = value
    sections["audio"] = audio_table

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(_dump_toml(sections))
    os.replace(tmp, path)
