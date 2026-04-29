"""Pygame display initialization and cleanup."""

from __future__ import annotations

import pygame

from constellation.config import AppConfig


def create_display(config: AppConfig) -> pygame.Surface:
    """Initialize pygame and create a display surface.

    Args:
        config: Application configuration.

    Returns:
        The main pygame display surface.
    """
    pygame.init()
    if config.mode == "fullscreen":
        size = (0, 0)  # (0, 0) = current desktop resolution
        flags = pygame.FULLSCREEN
    else:
        size = (config.display.width, config.display.height)
        flags = 0
    surface = pygame.display.set_mode(size, flags)
    pygame.display.set_caption(config.display.title)
    return surface


def cleanup() -> None:
    """Shut down pygame."""
    pygame.quit()
