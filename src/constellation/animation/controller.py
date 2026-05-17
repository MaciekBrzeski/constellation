"""Animation controller for cycling through animal constellation frames."""

from __future__ import annotations

from pathlib import Path

from constellation.config import AnimationConfig
from constellation.geometry.vertices import interpolate_vertices
from constellation.preprocessing.export import AnimalData, load_animal


class AnimationController:
    """Manages animal data, frame cycling, and action switching.

    Args:
        config: Animation configuration.
        assets_dir: Path to the assets directory containing constellation JSONs.
    """

    def __init__(self, config: AnimationConfig, assets_dir: Path) -> None:
        self._config = config
        self._assets_dir = assets_dir
        self._animals: dict[str, AnimalData] = {}
        self._animal_index = 0
        self._action = config.default_action
        self._frame_index = 0
        # Beat index at which the current action started — see advance_to_beat.
        # None = re-anchor on the next beat (set after any animal/action switch).
        self._beat_anchor: int | None = None
        self._load_animals()

    def _load_animals(self) -> None:
        """Load all available animal JSON files from the constellations directory."""
        constellations_dir = self._assets_dir / "constellations"
        if not constellations_dir.exists():
            return
        # Load configured animals first (preserves order)
        for name in self._config.animals:
            path = constellations_dir / f"{name}.json"
            if path.exists():
                self._animals[name] = load_animal(path)
        # Also discover any other JSON files not in the config list
        for path in sorted(constellations_dir.glob("*.json")):
            name = path.stem
            if name not in self._animals:
                self._animals[name] = load_animal(path)

    @property
    def animal_names(self) -> list[str]:
        return list(self._animals.keys())

    @property
    def current_animal_name(self) -> str | None:
        names = self.animal_names
        if not names:
            return None
        return names[self._animal_index % len(names)]

    def next_animal(self) -> None:
        """Cycle to the next animal."""
        if self._animals:
            self._animal_index = (self._animal_index + 1) % len(self._animals)
            self._frame_index = 0
            self._beat_anchor = None

    def set_animal(self, name: str) -> bool:
        """Switch to a named animal. Returns True if found and switched."""
        names = self.animal_names
        if name not in names:
            return False
        self._animal_index = names.index(name)
        self._frame_index = 0
        self._beat_anchor = None
        return True

    def available_actions(self, animal_name: str | None = None) -> list[str]:
        """List action names defined for the given animal (or current)."""
        name = animal_name or self.current_animal_name
        if name is None or name not in self._animals:
            return []
        return [a.action for a in self._animals[name].animations]

    def set_action(self, action: str) -> None:
        """Switch to a named action (e.g. idle, walk)."""
        self._action = action
        self._frame_index = 0
        self._beat_anchor = None

    def current_vertices(self) -> list[tuple[float, float]]:
        """Get the vertices for the current frame.

        Returns:
            List of (x, y) vertex tuples, or empty list if no data.
        """
        name = self.current_animal_name
        if name is None or name not in self._animals:
            return []

        animal = self._animals[name]
        # Find the matching action
        anim = None
        for a in animal.animations:
            if a.action == self._action:
                anim = a
                break
        if anim is None and animal.animations:
            anim = animal.animations[0]
        if anim is None or not anim.frames:
            return []

        frame = anim.frames[self._frame_index % len(anim.frames)]
        return [(v[0], v[1]) for v in frame.vertices]

    def current_vertices_tween(
        self, t: float
    ) -> list[tuple[float, float]]:
        """Interpolate between the current frame and the next by ``t`` in [0, 1].

        Returns the current frame's vertices unchanged when ``t <= 0``. When
        frames have different vertex counts the shorter list is padded by
        repeating its last vertex (see ``interpolate_vertices``), so extra
        vertices appear to fade in from the tail — acceptable for smooth
        sprite animations where counts change by a few verts per frame.
        """
        name = self.current_animal_name
        if name is None or name not in self._animals:
            return []
        animal = self._animals[name]
        anim = None
        for a in animal.animations:
            if a.action == self._action:
                anim = a
                break
        if anim is None and animal.animations:
            anim = animal.animations[0]
        if anim is None or not anim.frames:
            return []

        n = len(anim.frames)
        cur = anim.frames[self._frame_index % n]
        cur_verts = [(v[0], v[1]) for v in cur.vertices]
        if t <= 0.0 or n < 2:
            return cur_verts
        nxt = anim.frames[(self._frame_index + 1) % n]
        nxt_verts = [(v[0], v[1]) for v in nxt.vertices]
        return interpolate_vertices(cur_verts, nxt_verts, min(1.0, t))

    def current_edges(self) -> list[list[int]]:
        """Get the stored contour edges for the current frame.

        Returns:
            List of [i, j] index pairs, or empty list if no data.
        """
        name = self.current_animal_name
        if name is None or name not in self._animals:
            return []

        animal = self._animals[name]
        anim = None
        for a in animal.animations:
            if a.action == self._action:
                anim = a
                break
        if anim is None and animal.animations:
            anim = animal.animations[0]
        if anim is None or not anim.frames:
            return []

        frame = anim.frames[self._frame_index % len(anim.frames)]
        return frame.edges

    def current_stars(self) -> list[int]:
        """Get star-marker indices (into current vertices) for the current frame.

        Returns an empty list if the frame has no stars field; callers should
        then fall back to treating every vertex as a star.
        """
        name = self.current_animal_name
        if name is None or name not in self._animals:
            return []

        animal = self._animals[name]
        anim = None
        for a in animal.animations:
            if a.action == self._action:
                anim = a
                break
        if anim is None and animal.animations:
            anim = animal.animations[0]
        if anim is None or not anim.frames:
            return []

        frame = anim.frames[self._frame_index % len(anim.frames)]
        return list(frame.stars)

    def advance_frame(self) -> None:
        """Advance to the next animation frame (relative, time-driven path)."""
        self._frame_index += 1

    def advance_to_beat(self, beat_count: int) -> None:
        """Set the frame from an authoritative network beat counter.

        Frame index = ``beat_count`` counted from the beat at which the
        current action started on *this* worker (the anchor). Two workers
        running different actions still advance one frame per beat together,
        each relative to its own action's frame 0 — keeping them frame-locked
        while showing distinct content.

        The anchor is cleared on any animal/action switch (so the new action
        starts at frame 0) and re-set here on the next call. ``beat_count < 0``
        (no beats seen yet) holds the controller at frame 0.
        """
        if beat_count < 0:
            self._frame_index = 0
            return
        if self._beat_anchor is None:
            self._beat_anchor = beat_count
        self._frame_index = beat_count - self._beat_anchor
