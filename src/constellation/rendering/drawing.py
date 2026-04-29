"""Drawing functions for constellation rendering."""

from __future__ import annotations

import random

import pygame

from constellation.config import RenderConfig
from constellation.geometry.edges import Edge
from constellation.geometry.stars import BackgroundStar

# Per-vertex star radii — computed once, reseeded when vertex count changes.
_cached_star_radii: list[int] = []
_cached_vert_count: int = 0

# Pre-rendered star glyphs keyed on (radius, sparkle, color). The hot path
# (bg stars + constellation markers × every frame) used to hammer set_at per
# pixel; now each unique (r, sparkle, color) is rendered once to a small
# SRCALPHA surface and reused via a single blit.
_glyph_cache: dict[tuple[int, bool, tuple[int, int, int]], pygame.Surface] = {}


def _render_glyph(
    radius: int, sparkle: bool, color: tuple[int, int, int]
) -> pygame.Surface:
    """Render a star glyph to a transparent surface for caching."""
    # primary_len = radius*4 + margin for halo ring + 1 px safety
    size = radius * 10 + 4
    surf = pygame.Surface((size, size), pygame.SRCALPHA)
    surf.fill((0, 0, 0, 0))
    cx = cy = size // 2
    _draw_star(surf, cx, cy, radius, color, alpha=1.0, sparkle=sparkle)
    return surf


def _get_glyph(
    radius: int, sparkle: bool, color: tuple[int, int, int]
) -> pygame.Surface:
    key = (radius, sparkle, color)
    glyph = _glyph_cache.get(key)
    if glyph is None:
        glyph = _render_glyph(radius, sparkle, color)
        _glyph_cache[key] = glyph
    return glyph


def _blit_star(
    surface: pygame.Surface,
    cx: int,
    cy: int,
    radius: int,
    color: tuple[int, int, int],
    alpha: float = 1.0,
    sparkle: bool = True,
) -> None:
    """Blit a cached star glyph centered at (cx, cy)."""
    if radius < 1:
        return
    glyph = _get_glyph(radius, sparkle, color)
    glyph.set_alpha(int(255 * max(0.0, min(1.0, alpha))))
    w, h = glyph.get_size()
    surface.blit(glyph, (cx - w // 2, cy - h // 2))


def _ensure_star_radii(count: int, r_min: int, r_max: int) -> list[int]:
    """Return a stable list of random radii for *count* vertices.

    Biased toward a few bright + many faint stars so the output reads as a
    constellation with magnitude variance: 20% bright, 50% mid, 30% faint.
    """
    global _cached_star_radii, _cached_vert_count
    if count != _cached_vert_count:
        rng = random.Random(42)
        r_mid = (r_min + r_max) // 2
        radii: list[int] = []
        for _ in range(count):
            roll = rng.random()
            if roll < 0.2:
                radii.append(r_max)
            elif roll < 0.7:
                radii.append(r_mid)
            else:
                radii.append(r_min)
        _cached_star_radii = radii
        _cached_vert_count = count
    return _cached_star_radii


def _draw_sparkle(
    surface: pygame.Surface,
    cx: int,
    cy: int,
    radius: int,
    color: tuple[int, int, int],
    alpha: float = 1.0,
) -> None:
    """Draw 8-point diffraction spikes on top of an existing core.

    Four bright cardinal spikes with thickened roots + four dim diagonal
    spikes at half length — mimicking the diffraction pattern of a bright
    point source through a camera iris or eye lashes.
    """
    w, h = surface.get_size()
    primary_len = radius * 4
    diag_len = max(1, radius * 2)
    root_len = max(1, primary_len // 3)

    # Primary cardinal spikes (tapered), with 1-px perpendicular thickening
    # near the root so the star has weight at its center.
    for step in range(1, primary_len + 1):
        t = 1.0 - step / (primary_len + 1)
        bright = alpha * (t ** 1.2) * 0.9
        c = _apply_alpha(color, bright)
        for px, py in (
            (cx, cy - step),
            (cx, cy + step),
            (cx - step, cy),
            (cx + step, cy),
        ):
            if 0 <= px < w and 0 <= py < h:
                surface.set_at((px, py), _blend(surface.get_at((px, py)), c))
        if step <= root_len:
            c2 = _apply_alpha(color, bright * 0.5)
            for px, py in (
                (cx - 1, cy - step), (cx + 1, cy - step),
                (cx - 1, cy + step), (cx + 1, cy + step),
                (cx - step, cy - 1), (cx - step, cy + 1),
                (cx + step, cy - 1), (cx + step, cy + 1),
            ):
                if 0 <= px < w and 0 <= py < h:
                    surface.set_at((px, py), _blend(surface.get_at((px, py)), c2))

    # Diagonal spikes — shorter and dimmer (turns a + into an 8-point star)
    for step in range(1, diag_len + 1):
        t = 1.0 - step / (diag_len + 1)
        bright = alpha * (t ** 1.5) * 0.35
        c = _apply_alpha(color, bright)
        for px, py in (
            (cx - step, cy - step),
            (cx + step, cy - step),
            (cx - step, cy + step),
            (cx + step, cy + step),
        ):
            if 0 <= px < w and 0 <= py < h:
                surface.set_at((px, py), _blend(surface.get_at((px, py)), c))


def _draw_star(
    surface: pygame.Surface,
    cx: int,
    cy: int,
    radius: int,
    color: tuple[int, int, int],
    alpha: float = 1.0,
    sparkle: bool = False,
) -> None:
    """Draw a single star — bright core, optional halo ring, and spikes."""
    # Solid hot core
    core_r = max(1, radius // 2) if sparkle else radius
    pygame.draw.circle(surface, _apply_alpha(color, alpha), (cx, cy), core_r)
    # Soft halo ring for larger stars — 1-px outline at full radius, dim
    if sparkle and radius >= 2:
        pygame.draw.circle(
            surface, _apply_alpha(color, alpha * 0.35), (cx, cy), radius, width=1
        )
    if sparkle:
        _draw_sparkle(surface, cx, cy, radius, color, alpha)


def draw_frame(
    surface: pygame.Surface,
    verts: list[tuple[float, float]],
    edges: list[Edge],
    bg_stars: list[BackgroundStar],
    config: RenderConfig,
    beat_pulse: float = 0.0,
    star_indices: list[int] | None = None,
) -> None:
    """Draw a single constellation frame to the surface.

    Clears the surface to black, draws background stars, constellation edges
    (semi-transparent), and vertex stars (randomised radii with sparkles),
    then flips.

    Args:
        beat_pulse: 0.0–1.0 intensity of current beat pulse. Boosts vertex
            star radii and edge brightness when > 0.
    """
    surface.fill((0, 0, 0))

    # Draw background stars
    for star in bg_stars:
        r = max(1, int(star.radius * star.alpha))
        sparkle = star.radius >= 1.8
        _blit_star(
            surface,
            int(star.x),
            int(star.y),
            r,
            config.star_color,
            alpha=star.alpha,
            sparkle=sparkle,
        )

    # Draw constellation edges (semi-transparent, boosted on beat)
    edge_alpha = config.line_alpha + beat_pulse * (1.0 - config.line_alpha)
    line_color = _apply_alpha(config.line_color, min(1.0, edge_alpha))
    for edge in edges:
        if edge.i < len(verts) and edge.j < len(verts):
            start = (int(verts[edge.i][0]), int(verts[edge.i][1]))
            end = (int(verts[edge.j][0]), int(verts[edge.j][1]))
            pygame.draw.aaline(surface, line_color, start, end)

    # Draw constellation vertex stars (randomised sizes, sparkle on large ones).
    # Beat pulse boosts radius by up to 2x and forces sparkle on all stars.
    # star_indices selects which vertices get a star — fallback: all vertices.
    if star_indices is None or not star_indices:
        star_positions = list(enumerate(verts))
    else:
        star_positions = [(i, verts[i]) for i in star_indices if i < len(verts)]
    radii = _ensure_star_radii(
        len(star_positions), config.star_radius_min, config.star_radius_max
    )
    radius_boost = 1.0 + beat_pulse * 1.0  # 1x to 2x
    for k, (_idx, (x, y)) in enumerate(star_positions):
        r = max(1, int(radii[k] * radius_boost))
        # Constellation markers always get full sparkle treatment.
        _blit_star(surface, int(x), int(y), r, config.star_color, sparkle=True)

    pygame.display.flip()


def _apply_alpha(color: tuple[int, int, int], alpha: float) -> tuple[int, int, int]:
    """Scale an RGB color by an alpha factor."""
    return (
        int(color[0] * alpha),
        int(color[1] * alpha),
        int(color[2] * alpha),
    )


def _blend(
    bg: tuple[int, ...] | pygame.Color, fg: tuple[int, int, int]
) -> tuple[int, int, int]:
    """Additive blend of fg onto bg (clamped to 255)."""
    return (
        min(255, bg[0] + fg[0]),
        min(255, bg[1] + fg[1]),
        min(255, bg[2] + fg[2]),
    )
