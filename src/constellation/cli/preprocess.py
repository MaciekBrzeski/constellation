"""CLI entry point for preprocessing spritesheets into constellation data."""

from __future__ import annotations

import argparse
from pathlib import Path

from constellation.config import load_config
from constellation.preprocessing.binarize import binarize, extract_contours
from constellation.preprocessing.export import (
    AnimalData,
    AnimationData,
    FrameData,
    export_animal,
)
from constellation.preprocessing.simplify import simplify_contour
from constellation.preprocessing.spritesheet import (
    frames_to_arrays,
    load_spritesheet,
    slice_frames,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="constellation-preprocess",
        description="Preprocess pixel-art spritesheets into constellation vertex data.",
    )
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument("--input", "-i", type=Path, help="Path to spritesheet PNG")
    input_group.add_argument(
        "--input-dir", "-d", type=Path, help="Directory of per-action PNGs (e.g. Male_Idle.png)"
    )
    parser.add_argument("--animal", "-a", required=True, help="Animal name (e.g. deer_male)")
    parser.add_argument(
        "--actions",
        nargs="+",
        default=["idle"],
        help="Action names corresponding to spritesheet rows",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        default=Path("assets/constellations"),
        help="Output directory for JSON",
    )
    parser.add_argument("--config", "-c", type=Path, default=None, help="Config TOML path")
    parser.add_argument(
        "--frame-size",
        nargs=2,
        type=int,
        default=None,
        metavar=("W", "H"),
        help="Frame width and height (overrides config)",
    )
    return parser


def _process_frames(arrays, pp) -> list[FrameData]:
    """Convert grayscale arrays into FrameData list.

    Vertices form a dense polyline following the raw pixel contour so that
    drawn edges hug the silhouette. Stars mark only the Douglas-Peucker
    corner subset (direction-changing points).
    """
    frame_data_list: list[FrameData] = []
    for arr in arrays:
        binary = binarize(arr, threshold=pp.binarize_threshold)
        contours = extract_contours(binary, min_area=pp.min_contour_area)
        all_verts: list[list[float]] = []
        all_edges: list[list[int]] = []
        all_stars: list[int] = []
        for contour in contours:
            pts = [(float(p[0][0]), float(p[0][1])) for p in contour]
            if len(pts) < 2:
                continue
            simplified = simplify_contour(contour, epsilon_pct=pp.epsilon_pct)
            corner_set = {(int(p[0][0]), int(p[0][1])) for p in simplified}
            base = len(all_verts)
            for k, (x, y) in enumerate(pts):
                all_edges.append([base + k, base + (k + 1) % len(pts)])
                if (int(x), int(y)) in corner_set:
                    all_stars.append(base + k)
            all_verts.extend([list(v) for v in pts])
        frame_data_list.append(
            FrameData(vertices=all_verts, edges=all_edges, stars=all_stars)
        )
    return frame_data_list


def _process_input_dir(
    input_dir: Path,
    frame_w: int,
    frame_h: int,
    pp=None,
) -> list[AnimationData]:
    """Process a directory of per-action PNGs into AnimationData list.

    Filenames like ``Male_Alert.png`` are split on ``_`` and the last segment
    (lowercased) becomes the action name.
    """
    if pp is None:
        pp = load_config().preprocess

    animations: list[AnimationData] = []
    for png_path in sorted(input_dir.glob("*.png")):
        # Extract action name: "Male_Alert.png" -> "alert"
        stem = png_path.stem  # "Male_Alert"
        action_name = stem.rsplit("_", 1)[-1].lower()

        sheet = load_spritesheet(png_path)
        frames = slice_frames(sheet, frame_w, frame_h, row=0)
        if not frames:
            continue
        arrays = frames_to_arrays(frames)
        frame_data_list = _process_frames(arrays, pp)
        animations.append(AnimationData(action=action_name, frames=frame_data_list))

    return animations


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    config = load_config(args.config)
    pp = config.preprocess

    frame_w = args.frame_size[0] if args.frame_size else pp.frame_width
    frame_h = args.frame_size[1] if args.frame_size else pp.frame_height

    if args.input_dir:
        animations = _process_input_dir(args.input_dir, frame_w, frame_h, pp)
    else:
        sheet = load_spritesheet(args.input)
        animations: list[AnimationData] = []
        for row, action_name in enumerate(args.actions):
            frames = slice_frames(sheet, frame_w, frame_h, row=row)
            arrays = frames_to_arrays(frames)
            frame_data_list = _process_frames(arrays, pp)
            animations.append(AnimationData(action=action_name, frames=frame_data_list))

    animal = AnimalData(
        name=args.animal,
        frame_size=(frame_w, frame_h),
        animations=animations,
    )
    path = export_animal(animal, args.output)
    print(f"Exported {args.animal} → {path}")
