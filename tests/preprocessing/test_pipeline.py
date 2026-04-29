"""End-to-end preprocess pipeline on a synthetic frame."""

from __future__ import annotations

import numpy as np

from constellation.cli.preprocess import _process_frames
from constellation.config import PreprocessConfig


def _square_frame(size: int, margin: int) -> np.ndarray:
    arr = np.zeros((size, size), dtype=np.uint8)
    arr[margin : size - margin, margin : size - margin] = 255
    return arr


def test_pipeline_square_produces_four_stars_and_closed_ring() -> None:
    arr = _square_frame(32, 8)  # 16×16 filled square centred in 32×32
    pp = PreprocessConfig()
    frames = _process_frames([arr], pp)

    assert len(frames) == 1
    f = frames[0]

    # Dense raw contour → many vertices, edges form a closed ring.
    assert len(f.vertices) >= 4
    assert len(f.edges) == len(f.vertices)
    assert f.edges[-1][1] == 0  # closed ring wraps back to index 0

    # Square simplifies to 4 DP corners → 4 star markers.
    assert len(f.stars) == 4
    # Star indices must point inside vertex array.
    for s in f.stars:
        assert 0 <= s < len(f.vertices)


def test_pipeline_empty_frame_yields_empty_data() -> None:
    arr = np.zeros((32, 32), dtype=np.uint8)
    frames = _process_frames([arr], PreprocessConfig())
    assert len(frames) == 1
    f = frames[0]
    assert f.vertices == []
    assert f.edges == []
    assert f.stars == []
