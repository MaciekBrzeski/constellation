"""Tests for scale_vertices + interpolate_vertices math."""

from __future__ import annotations

import pytest

from constellation.geometry.vertices import interpolate_vertices, scale_vertices


def test_scale_vertices_centers_within_padding() -> None:
    verts = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    scaled = scale_vertices(
        verts, source_size=(1, 1), target_size=(100, 100), padding=10
    )
    xs = [p[0] for p in scaled]
    ys = [p[1] for p in scaled]
    # Usable area is 80×80 centered at (50, 50).
    assert min(xs) == pytest.approx(10.0)
    assert max(xs) == pytest.approx(90.0)
    assert min(ys) == pytest.approx(10.0)
    assert max(ys) == pytest.approx(90.0)


def test_scale_vertices_preserves_aspect() -> None:
    verts = [(0.0, 0.0), (2.0, 0.0), (2.0, 1.0), (0.0, 1.0)]
    scaled = scale_vertices(
        verts, source_size=(2, 1), target_size=(100, 100), padding=0
    )
    xs = [p[0] for p in scaled]
    ys = [p[1] for p in scaled]
    # 2:1 source in a 100×100 target fills width, letterboxes height.
    assert max(xs) - min(xs) == pytest.approx(100.0)
    assert max(ys) - min(ys) == pytest.approx(50.0)


def test_interpolate_at_t_zero_returns_source() -> None:
    a = [(0.0, 0.0), (1.0, 1.0)]
    b = [(10.0, 10.0), (20.0, 20.0)]
    result = interpolate_vertices(a, b, 0.0)
    assert result == pytest.approx(a)


def test_interpolate_at_t_one_returns_target() -> None:
    a = [(0.0, 0.0), (1.0, 1.0)]
    b = [(10.0, 10.0), (20.0, 20.0)]
    result = interpolate_vertices(a, b, 1.0)
    assert result == pytest.approx(b)


def test_interpolate_midpoint() -> None:
    a = [(0.0, 0.0), (10.0, 0.0)]
    b = [(0.0, 10.0), (10.0, 10.0)]
    result = interpolate_vertices(a, b, 0.5)
    assert result == pytest.approx([(0.0, 5.0), (10.0, 5.0)])


def test_interpolate_pads_shorter_list() -> None:
    a = [(0.0, 0.0)]
    b = [(10.0, 0.0), (20.0, 0.0)]
    result = interpolate_vertices(a, b, 1.0)
    # Output length matches the longer list; both slots end up at b.
    assert len(result) == 2
    assert result == pytest.approx(b)
