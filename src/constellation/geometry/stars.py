"""Background star generation and twinkle animation."""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class BackgroundStar:
    """A decorative background star."""

    x: float
    y: float
    radius: float = 1.0
    alpha: float = 1.0


def generate_background_stars(
    display_size: tuple[int, int],
    count: int = 20,
    margin: int = 10,
) -> list[BackgroundStar]:
    """Generate random background stars across the display area.

    Args:
        display_size: (width, height) of the display.
        count: Number of background stars to generate.
        margin: Pixel margin from display edges.

    Returns:
        List of BackgroundStar objects at random positions.
    """
    w, h = display_size
    stars: list[BackgroundStar] = []
    for _ in range(count):
        x = random.uniform(margin, w - margin)
        y = random.uniform(margin, h - margin)
        # Most stars are tiny; a few are bright — exponential distribution
        radius = random.expovariate(2.0) + 0.3
        radius = min(radius, 3.0)
        alpha = random.uniform(0.4, 1.0)
        stars.append(BackgroundStar(x=x, y=y, radius=radius, alpha=alpha))
    return stars


def update_twinkle(
    stars: list[BackgroundStar],
    probability: float = 0.08,
    min_alpha: float = 0.3,
) -> None:
    """Randomly update star alpha values to create a twinkle effect.

    Args:
        stars: List of background stars to update in place.
        probability: Chance each star changes alpha per frame.
        min_alpha: Minimum alpha value.
    """
    for star in stars:
        if random.random() < probability:
            star.alpha = random.uniform(min_alpha, 1.0)
