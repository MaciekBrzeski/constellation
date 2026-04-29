"""Data structures and JSON I/O for preprocessed constellation data."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class FrameData:
    """Vertices, edges, and star-marker indices for a single animation frame.

    ``vertices`` forms a dense polyline following the silhouette (used for edge
    drawing). ``stars`` is a subset of vertex indices marking the direction-
    changing points where a visible star is rendered. If ``stars`` is empty,
    renderers should fall back to treating every vertex as a star.
    """

    vertices: list[list[float]] = field(default_factory=list)
    edges: list[list[int]] = field(default_factory=list)
    stars: list[int] = field(default_factory=list)


@dataclass
class AnimationData:
    """Frames for a single action (e.g. idle, walk)."""

    action: str = ""
    frames: list[FrameData] = field(default_factory=list)


@dataclass
class AnimalData:
    """All animation data for one animal."""

    name: str = ""
    frame_size: tuple[int, int] = (32, 32)
    animations: list[AnimationData] = field(default_factory=list)


def export_animal(animal: AnimalData, output_dir: Path) -> Path:
    """Export animal data to a JSON file.

    Args:
        animal: The animal data to export.
        output_dir: Directory to write the JSON file.

    Returns:
        Path to the written JSON file.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{animal.name}.json"
    data = asdict(animal)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    return path


def load_animal(path: Path) -> AnimalData:
    """Load animal data from a JSON file.

    Args:
        path: Path to the JSON file.

    Returns:
        Deserialized AnimalData.
    """
    with open(path) as f:
        raw = json.load(f)
    animations = [
        AnimationData(
            action=anim["action"],
            frames=[
                FrameData(
                    vertices=frame["vertices"],
                    edges=frame.get("edges", []),
                    stars=frame.get("stars", []),
                )
                for frame in anim["frames"]
            ],
        )
        for anim in raw.get("animations", [])
    ]
    return AnimalData(
        name=raw["name"],
        frame_size=tuple(raw["frame_size"]),
        animations=animations,
    )
