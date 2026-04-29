"""Load and slice pixel-art spritesheets into individual frames."""

from __future__ import annotations

from pathlib import Path

import numpy as np
from numpy.typing import NDArray
from PIL import Image


def load_spritesheet(path: Path) -> Image.Image:
    """Load a spritesheet image from disk."""
    return Image.open(path).convert("RGBA")


def slice_frames(
    sheet: Image.Image, frame_w: int, frame_h: int, row: int = 0
) -> list[Image.Image]:
    """Slice a spritesheet row into individual frames.

    Args:
        sheet: The full spritesheet image.
        frame_w: Width of each frame in pixels.
        frame_h: Height of each frame in pixels.
        row: Which row to slice (0-indexed).

    Returns:
        List of cropped frame images.
    """
    cols = sheet.width // frame_w
    frames: list[Image.Image] = []
    y = row * frame_h
    for col in range(cols):
        x = col * frame_w
        frame = sheet.crop((x, y, x + frame_w, y + frame_h))
        # Skip fully transparent frames
        if frame.getextrema()[3][1] > 0:  # type: ignore[index]
            frames.append(frame)
    return frames


def frames_to_arrays(frames: list[Image.Image]) -> list[NDArray[np.uint8]]:
    """Convert PIL frames to grayscale numpy arrays using the alpha channel.

    For RGBA images, the alpha channel is used as the grayscale representation
    since we care about sprite silhouette, not color. For non-alpha images,
    standard grayscale conversion is used.
    """
    arrays: list[NDArray[np.uint8]] = []
    for frame in frames:
        if frame.mode == "RGBA":
            # Use alpha channel — sprite presence matters, not color
            arrays.append(np.array(frame.split()[3], dtype=np.uint8))
        else:
            arrays.append(np.array(frame.convert("L"), dtype=np.uint8))
    return arrays
