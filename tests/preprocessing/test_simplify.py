"""Regression tests for Douglas–Peucker simplification + corner detection."""

from __future__ import annotations

import numpy as np

from constellation.preprocessing.simplify import (
    detect_corners,
    simplify_contour,
)


def _rect_contour(w: int, h: int) -> np.ndarray:
    """Return a dense closed-contour rectangle as cv2 shape N×1×2 int32."""
    pts: list[tuple[int, int]] = []
    for x in range(w):
        pts.append((x, 0))
    for y in range(1, h):
        pts.append((w - 1, y))
    for x in range(w - 2, -1, -1):
        pts.append((x, h - 1))
    for y in range(h - 2, 0, -1):
        pts.append((0, y))
    return np.array([[p] for p in pts], dtype=np.int32)


def test_simplify_rectangle_returns_four_corners() -> None:
    contour = _rect_contour(20, 30)
    simplified = simplify_contour(contour, epsilon_pct=0.02)
    assert len(simplified) == 4


def test_simplify_circle_returns_polygon() -> None:
    angles = np.linspace(0, 2 * np.pi, 100, endpoint=False)
    xs = (np.cos(angles) * 10 + 20).astype(np.int32)
    ys = (np.sin(angles) * 10 + 20).astype(np.int32)
    contour = np.stack([xs, ys], axis=1).reshape(-1, 1, 2)
    simplified = simplify_contour(contour, epsilon_pct=0.02)
    # A circle at eps 0.02 of arc length DP-approximates with a few corners
    assert 6 <= len(simplified) <= 24


def test_detect_corners_on_L_shape() -> None:
    # Traverse the L outline densely so neighbor_dist has room.
    pts = [(0, 0), (20, 0), (20, 10), (10, 10), (10, 20), (0, 20)]
    dense: list[tuple[int, int]] = []
    for i, p in enumerate(pts):
        q = pts[(i + 1) % len(pts)]
        dx = 1 if q[0] > p[0] else (-1 if q[0] < p[0] else 0)
        dy = 1 if q[1] > p[1] else (-1 if q[1] < p[1] else 0)
        x, y = p
        while (x, y) != q:
            dense.append((x, y))
            x += dx
            y += dy
    contour = np.array([[d] for d in dense], dtype=np.int32)
    corners = detect_corners(contour, angle_threshold=120.0, neighbor_dist=3)
    # The L has 6 real corners; non-max suppression should surface at least
    # the 4 most-prominent 90°/270° bends.
    assert len(corners) >= 4
