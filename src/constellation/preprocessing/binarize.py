"""Binarize frames and extract contours."""

from __future__ import annotations

import cv2
import numpy as np
from numpy.typing import NDArray


def binarize(frame: NDArray[np.uint8], threshold: int = 128) -> NDArray[np.uint8]:
    """Convert a grayscale frame to a binary image.

    Args:
        frame: Grayscale image as numpy array.
        threshold: Pixel intensity threshold for binarization.

    Returns:
        Binary image (0 or 255 values).
    """
    _, binary = cv2.threshold(frame, threshold, 255, cv2.THRESH_BINARY)
    return binary


def extract_contours(
    binary: NDArray[np.uint8], min_area: int = 50
) -> list[NDArray[np.int32]]:
    """Extract external contours from a binary image.

    Args:
        binary: Binary image (output of binarize).
        min_area: Minimum contour area to keep.

    Returns:
        List of contour arrays, filtered by minimum area.
    """
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    return [c for c in contours if cv2.contourArea(c) >= min_area]
