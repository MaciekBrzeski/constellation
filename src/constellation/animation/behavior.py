"""Audio-reactive behavior: switches animal actions based on beat activity."""

from __future__ import annotations

import random
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from constellation.animation.controller import AnimationController
    from constellation.audio.beat import BeatDetector


TIER_ACTIONS: dict[str, tuple[str, ...]] = {
    "low": ("idle", "eating"),
    "medium": ("walk", "alert"),
    "high": ("run", "jump"),
}


class AudioReactiveBehavior:
    """Drive animal action selection from the detector's energy tier.

    Pure tier-driven: whenever ``BeatDetector.energy_tier`` changes we switch
    to a random action from that tier's pool, and within the same tier we
    rotate to another action every ``action_rotate_min_sec`` to
    ``action_rotate_max_sec`` (scaled by tempo). The tier classifier already
    falls to ``low`` within ~2 s of silence via its fast EMA, so there's no
    separate silence-timeout path any more.

    Tier → actions:
      * low    → idle, eating
      * medium → walk, alert
      * high   → run, jump
    """

    def __init__(
        self,
        beat_detector: BeatDetector,
        controller: AnimationController,
        action_rotate_min_sec: float = 8.0,
        action_rotate_max_sec: float = 20.0,
        tempo_rotate_factor: float = 1.0,
        strong_beat_threshold: float = 4.0,
        strong_hold_sec: float = 2.5,
        **_unused: object,
    ) -> None:
        """
        Args:
            beat_detector: Source of tier + BPM state.
            controller: Target animal.
            action_rotate_min_sec: Minimum seconds before rotating within a tier.
            action_rotate_max_sec: Maximum seconds before rotating within a tier.
            tempo_rotate_factor: Tempo exponent — delay scales by
                ``(reference_bpm / bpm) ** factor``, so faster music rotates
                actions faster.
            strong_beat_threshold: Beat ``rms / noise_floor`` above which a
                transient is treated as a strong kick — briefly overrides the
                sustained tier to pick a run/jump action.
            strong_hold_sec: After a strong beat, hold the high-tier action
                for at least this many seconds before allowing the sustained
                tier to downgrade. Prevents single-frame twitches where the
                deer snaps to run then back to idle. Each new strong beat
                extends the hold.
            **_unused: Accepts stale kwargs (e.g. ``silence_timeout_sec``)
                from older callers without crashing.
        """
        self._beat_detector = beat_detector
        self._controller = controller
        self._rotate_min = action_rotate_min_sec
        self._rotate_max = action_rotate_max_sec
        self._tempo_rotate_factor = tempo_rotate_factor
        self._strong_beat_threshold = strong_beat_threshold
        self._strong_hold_sec = strong_hold_sec
        self._reference_bpm: float = 120.0

        self._current_action: str | None = None
        self._current_tier: str | None = None
        self._next_rotate_time: float = 0.0
        self._last_handled_beat_ms: float = 0.0
        self._strong_until: float = 0.0

    @property
    def active(self) -> bool:
        """True when the detector reports non-low tier (music is playing)."""
        return self._beat_detector.energy_tier != "low"

    def update(self) -> None:
        """Called once per main-loop iteration."""
        now = time.monotonic()
        tier = self._beat_detector.energy_tier

        # Strong-beat override: a kick well above the noise floor pops the
        # deer into run/jump. Dedupe by audio-time so the same beat isn't
        # handled twice across main-loop frames. The first strong beat in a
        # burst picks a high action; subsequent strong beats merely extend
        # the lockout (avoids re-picking every kick → would cycle run↔jump
        # every 500 ms and look spasmodic).
        beat_ms = self._beat_detector.last_beat_audio_ms
        if beat_ms > self._last_handled_beat_ms:
            self._last_handled_beat_ms = beat_ms
            strength = self._beat_detector.last_beat_strength
            if strength >= self._strong_beat_threshold:
                already_locked = now < self._strong_until
                self._strong_until = now + self._strong_hold_sec
                if not already_locked:
                    self._current_tier = "high"
                    self._pick_tier_action("high")
                    self._schedule_rotation()
                return

        # Strong-beat lockout — suppress downward tier changes while active.
        # Upward moves (tier → high) are rare during lockout and harmless.
        if now < self._strong_until and tier != "high":
            if now >= self._next_rotate_time:
                # Rotate within the high pool while held.
                self._pick_tier_action("high")
                self._schedule_rotation()
            return

        if tier != self._current_tier:
            # Tier changed (e.g. song got louder) — switch action immediately.
            self._current_tier = tier
            self._pick_tier_action(tier)
            self._schedule_rotation()
        elif now >= self._next_rotate_time:
            # Same tier but rotation timer fired — pick another action in tier.
            self._pick_tier_action(tier)
            self._schedule_rotation()

    def _pick_tier_action(self, tier: str) -> None:
        """Choose a random action from the given tier, excluding the current one."""
        pool = TIER_ACTIONS.get(tier, TIER_ACTIONS["low"])
        candidates = [a for a in pool if a != self._current_action]
        if not candidates:
            candidates = list(pool)
        self._set_action_if_available(random.choice(candidates))

    def _schedule_rotation(self) -> None:
        delay = random.uniform(self._rotate_min, self._rotate_max)
        bpm = self._beat_detector.bpm
        if bpm > 0:
            scale = (self._reference_bpm / bpm) ** self._tempo_rotate_factor
            scale = max(0.25, min(4.0, scale))
            delay *= scale
        self._next_rotate_time = time.monotonic() + delay

    def _set_action_if_available(self, action: str) -> None:
        """Only set the action if the current animal actually has it.

        Falls back to any available action in the same tier so animals with
        missing actions (e.g. no ``eating`` frames) don't freeze.
        """
        name = self._controller.current_animal_name
        if name is None:
            return
        animal = self._controller._animals.get(name)
        if animal is None:
            return
        available = {a.action for a in animal.animations}
        if action in available:
            self._controller.set_action(action)
            self._current_action = action
            return
        # Fallback: try any other action in the same tier.
        pool = TIER_ACTIONS.get(self._current_tier or "low", ())
        for candidate in pool:
            if candidate in available and candidate != self._current_action:
                self._controller.set_action(candidate)
                self._current_action = candidate
                return
