"""Simplify and resample contours to polygon vertices."""

from __future__ import annotations

import cv2
import numpy as np
from numpy.typing import NDArray


def detect_corners(
    contour: NDArray[np.int32],
    angle_threshold: float = 120.0,
    neighbor_dist: int = 3,
) -> set[int]:
    """Find high-curvature points on a contour where the outline bends sharply.

    Computes the angle between vectors to neighbours *neighbor_dist* pixels
    away on each side.  Points whose angle is below *angle_threshold* (degrees)
    are corners.  Non-maximum suppression keeps only the sharpest point in
    each local cluster.

    Args:
        contour: Input contour from cv2.findContours (shape N×1×2).
        angle_threshold: Maximum interior angle (degrees) to qualify as a
            corner.  180 = straight line; lower = sharper.
        neighbor_dist: Number of contour pixels in each direction used to
            compute the curvature vectors.

    Returns:
        Set of contour-point indices that are corners.
    """
    pts = contour.reshape(-1, 2).astype(float)
    n = len(pts)
    if n < 2 * neighbor_dist + 1:
        return set()

    threshold_rad = angle_threshold * np.pi / 180.0
    angles = np.full(n, np.pi)  # default to 180° (straight)

    for i in range(n):
        p_prev = pts[(i - neighbor_dist) % n]
        p_curr = pts[i]
        p_next = pts[(i + neighbor_dist) % n]

        v1 = p_prev - p_curr
        v2 = p_next - p_curr

        len1 = float(np.hypot(v1[0], v1[1]))
        len2 = float(np.hypot(v2[0], v2[1]))

        if len1 == 0 or len2 == 0:
            continue

        cos_angle = float(np.dot(v1, v2) / (len1 * len2))
        cos_angle = max(-1.0, min(1.0, cos_angle))
        angles[i] = float(np.arccos(cos_angle))

    # Candidates below threshold, sorted sharpest-first
    candidates = sorted(
        (i for i in range(n) if angles[i] < threshold_rad),
        key=lambda i: angles[i],
    )

    # NMS: suppress neighbours within *neighbor_dist* along the contour
    suppressed: set[int] = set()
    corners: set[int] = set()
    for i in candidates:
        if i in suppressed:
            continue
        corners.add(i)
        for d in range(1, neighbor_dist + 1):
            suppressed.add((i + d) % n)
            suppressed.add((i - d) % n)

    return corners


def simplify_contour(
    contour: NDArray[np.int32], epsilon_pct: float = 0.01
) -> NDArray[np.int32]:
    """Simplify a contour using the Douglas-Peucker algorithm.

    Args:
        contour: Input contour from cv2.findContours.
        epsilon_pct: Approximation accuracy as percentage of arc length.

    Returns:
        Simplified contour.
    """
    epsilon = epsilon_pct * cv2.arcLength(contour, closed=True)
    return cv2.approxPolyDP(contour, epsilon, closed=True)


def resample_contour(
    contour: NDArray[np.int32],
    max_edge_len: float = 2.0,
    corner_indices: set[int] | None = None,
) -> list[tuple[float, float]]:
    """Resample a contour at regular arc-length intervals.

    Walks along the contour boundary and places a vertex whenever the
    accumulated distance exceeds *max_edge_len*.  Points listed in
    *corner_indices* are always emitted regardless of spacing.

    Args:
        contour: Input contour from cv2.findContours (shape N×1×2).
        max_edge_len: Maximum pixel distance between consecutive vertices.
        corner_indices: Contour-point indices that must appear as vertices
            (e.g. from :func:`detect_corners`).

    Returns:
        List of (x, y) float tuples along the contour.
    """
    pts = contour.reshape(-1, 2).astype(float)
    n = len(pts)
    if n == 0:
        return []

    corners = corner_indices or set()
    result: list[tuple[float, float]] = [(pts[0][0], pts[0][1])]
    accum = 0.0

    for i in range(1, n + 1):
        idx = i % n
        prev = pts[i - 1]
        curr = pts[idx]
        seg_len = float(np.hypot(curr[0] - prev[0], curr[1] - prev[1]))

        if seg_len == 0:
            continue

        # Force-emit corner points
        if idx in corners and idx != 0:
            accum = 0.0
            result.append((float(curr[0]), float(curr[1])))
            continue

        remaining = seg_len
        start = prev.copy()

        while remaining > 0:
            needed = max_edge_len - accum
            if remaining >= needed:
                # Place a vertex at the needed distance along this segment
                t = needed / seg_len if seg_len > 0 else 0
                px = start[0] + (curr[0] - prev[0]) * (1 - remaining / seg_len + t)
                py = start[1] + (curr[1] - prev[1]) * (1 - remaining / seg_len + t)
                result.append((px, py))
                remaining -= needed
                accum = 0.0
                start = np.array([px, py])
            else:
                accum += remaining
                remaining = 0

    return result


def contour_to_vertices(contour: NDArray[np.int32]) -> list[tuple[float, float]]:
    """Convert an OpenCV contour to a flat list of (x, y) vertex tuples.

    Args:
        contour: Contour array of shape (N, 1, 2).

    Returns:
        List of (x, y) float tuples.
    """
    return [(float(pt[0][0]), float(pt[0][1])) for pt in contour]
