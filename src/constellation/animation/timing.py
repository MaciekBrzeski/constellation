"""Frame timing and clock management."""

from __future__ import annotations

import pygame


class FrameClock:
    """Manages frame timing using pygame's Clock.

    Args:
        target_fps: Target frames per second for sprite animation.
    """

    def __init__(self, target_fps: int = 12) -> None:
        self._clock = pygame.time.Clock()
        self._target_fps = target_fps
        self._frame_count = 0

    @property
    def frame_count(self) -> int:
        return self._frame_count

    def tick(self) -> float:
        """Advance one tick at the target FPS.

        Returns:
            Time elapsed in milliseconds since last tick.
        """
        self._frame_count += 1
        return self._clock.tick(self._target_fps)

    def reset(self) -> None:
        """Reset the frame counter."""
        self._frame_count = 0
