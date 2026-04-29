"""Blue noise sampling for interior detail points."""

from __future__ import annotations

import math

import numpy as np
from numpy.typing import NDArray


def dither_interior_points(
    frame: NDArray[np.uint8],
    grid_size: int = 8,
    contour_verts: list[tuple[float, float]] | None = None,
    min_dist: float = 2.0,
) -> list[tuple[float, float]]:
    """Extract interior detail points via Poisson disk (blue noise) sampling.

    Selects filled pixels with a minimum spacing constraint, producing an
    even, non-clumpy distribution. Deterministic (seeded RNG).

    Args:
        frame: 2D grayscale image (uint8).
        grid_size: Controls point density — minimum spacing between points
            is ``min(width, height) / grid_size``.
        contour_verts: Existing contour vertices to avoid duplicating.
        min_dist: Minimum distance from contour vertices (in frame pixels).

    Returns:
        List of (x, y) interior points in frame coordinates.
    """
    h, w = frame.shape
    min_spacing = min(w, h) / grid_size

    # Candidate positions: centers of filled pixels
    ys, xs = np.where(frame > 128)
    if len(xs) == 0:
        return []

    candidates = list(zip(xs.astype(float) + 0.5, ys.astype(float) + 0.5))

    # Deterministic shuffle for reproducible output
    rng = np.random.default_rng(seed=42)
    order = rng.permutation(len(candidates))

    selected: list[tuple[float, float]] = []
    for idx in order:
        x, y = candidates[idx]

        # Skip points too close to contour vertices
        if contour_verts and any(
            math.hypot(x - cx, y - cy) < min_dist for cx, cy in contour_verts
        ):
            continue

        # Skip points too close to already selected points (blue noise constraint)
        if any(math.hypot(x - sx, y - sy) < min_spacing for sx, sy in selected):
            continue

        selected.append((x, y))

    return selected
