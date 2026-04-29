"""Edge computation for constellation line rendering."""

from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass
class Edge:
    """A line segment between two vertices with a computed width."""

    i: int
    j: int
    distance: float
    width: float


def _bbox_diagonal(verts: list[tuple[float, float]]) -> float:
    """Compute the diagonal length of the vertex bounding box."""
    xs = [v[0] for v in verts]
    ys = [v[1] for v in verts]
    return math.hypot(max(xs) - min(xs), max(ys) - min(ys))


def compute_edges(
    verts: list[tuple[float, float]],
    d_max: float = 0.55,
    d_min: float = 0.05,
    line_max_width: int = 2,
    adaptive: bool = True,
) -> list[Edge]:
    """Compute edges between vertices within distance thresholds.

    Connects pairs of vertices that are between d_min and d_max apart.
    Line width is inversely proportional to distance.

    Args:
        verts: List of (x, y) vertices.
        d_max: Maximum distance threshold. Fraction of bounding box diagonal
            when adaptive=True, raw pixels when adaptive=False.
        d_min: Minimum distance threshold. Same units as d_max.
        line_max_width: Maximum line width in pixels.
        adaptive: When True, interpret d_max/d_min as fractions of the
            vertex bounding box diagonal.

    Returns:
        List of Edge objects.
    """
    if len(verts) < 2:
        return []

    if adaptive:
        diagonal = _bbox_diagonal(verts)
        abs_d_max = d_max * diagonal
        abs_d_min = d_min * diagonal
    else:
        abs_d_max = d_max
        abs_d_min = d_min

    edges: list[Edge] = []
    n = len(verts)
    for i in range(n):
        for j in range(i + 1, n):
            dx = verts[i][0] - verts[j][0]
            dy = verts[i][1] - verts[j][1]
            dist = math.hypot(dx, dy)
            if abs_d_min <= dist <= abs_d_max:
                width = line_max_width * (1.0 - (dist - abs_d_min) / (abs_d_max - abs_d_min))
                width = max(1.0, width)
                edges.append(Edge(i=i, j=j, distance=dist, width=width))
    return edges
