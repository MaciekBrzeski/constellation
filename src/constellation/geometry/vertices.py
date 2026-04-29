"""Vertex scaling and interpolation for constellation geometry."""

from __future__ import annotations


def scale_vertices(
    verts: list[tuple[float, float]],
    source_size: tuple[int, int],
    target_size: tuple[int, int],
    padding: int = 50,
) -> list[tuple[float, float]]:
    """Scale vertices from source coordinate space to target display size.

    Args:
        verts: List of (x, y) vertices in source coordinates.
        source_size: (width, height) of the source frame.
        target_size: (width, height) of the target display.
        padding: Pixel padding around the edges.

    Returns:
        Scaled vertices in target coordinates.
    """
    sw, sh = source_size
    tw, th = target_size
    usable_w = tw - 2 * padding
    usable_h = th - 2 * padding
    scale = min(usable_w / sw, usable_h / sh)
    offset_x = (tw - sw * scale) / 2
    offset_y = (th - sh * scale) / 2
    return [(x * scale + offset_x, y * scale + offset_y) for x, y in verts]


def interpolate_vertices(
    from_verts: list[tuple[float, float]],
    to_verts: list[tuple[float, float]],
    t: float,
) -> list[tuple[float, float]]:
    """Linearly interpolate between two vertex lists.

    If the lists differ in length, extra vertices in the longer list are faded
    in/out from the nearest vertex in the shorter list.

    Args:
        from_verts: Starting vertices.
        to_verts: Ending vertices.
        t: Interpolation factor (0.0 = from, 1.0 = to).

    Returns:
        Interpolated vertex list.
    """
    max_len = max(len(from_verts), len(to_verts))
    if max_len == 0:
        return []

    # Pad shorter list by repeating last vertex
    def _pad(verts: list[tuple[float, float]], n: int) -> list[tuple[float, float]]:
        if not verts:
            return [(0.0, 0.0)] * n
        while len(verts) < n:
            verts = verts + [verts[-1]]
        return verts

    a = _pad(list(from_verts), max_len)
    b = _pad(list(to_verts), max_len)

    return [
        (ax + (bx - ax) * t, ay + (by - ay) * t)
        for (ax, ay), (bx, by) in zip(a, b)
    ]
