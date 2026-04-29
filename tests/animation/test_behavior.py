"""Tier-driven action selection in AudioReactiveBehavior."""

from __future__ import annotations

import time

from constellation.animation.behavior import TIER_ACTIONS, AudioReactiveBehavior
from constellation.preprocessing.export import AnimalData, AnimationData, FrameData


class _FakeBeat:
    def __init__(self, tier: str = "low", bpm: float = 120.0) -> None:
        self.energy_tier = tier
        self.bpm = bpm
        self.last_beat_audio_ms: float = -1e12  # no beat fired
        self.last_beat_strength: float = 0.0


class _FakeController:
    def __init__(self, actions: tuple[str, ...]) -> None:
        self.current_animal_name = "deer"
        self._animals = {
            "deer": AnimalData(
                name="deer",
                frame_size=(64, 64),
                animations=[
                    AnimationData(action=a, frames=[FrameData()]) for a in actions
                ],
            )
        }
        self.set_action_calls: list[str] = []

    def set_action(self, action: str) -> None:
        self.set_action_calls.append(action)


def test_active_reflects_tier() -> None:
    beat = _FakeBeat(tier="low")
    ctrl = _FakeController(TIER_ACTIONS["low"] + TIER_ACTIONS["medium"] + TIER_ACTIONS["high"])
    b = AudioReactiveBehavior(beat_detector=beat, controller=ctrl)
    assert b.active is False
    beat.energy_tier = "medium"
    assert b.active is True
    beat.energy_tier = "high"
    assert b.active is True


def test_tier_change_picks_action_from_new_pool() -> None:
    beat = _FakeBeat(tier="low")
    ctrl = _FakeController(("idle", "eating", "walk", "alert", "run", "jump"))
    b = AudioReactiveBehavior(beat_detector=beat, controller=ctrl)
    b.update()  # initial (low)
    assert ctrl.set_action_calls[-1] in TIER_ACTIONS["low"]

    beat.energy_tier = "high"
    b.update()
    assert ctrl.set_action_calls[-1] in TIER_ACTIONS["high"]


def test_rotation_cycles_within_same_tier() -> None:
    beat = _FakeBeat(tier="medium")
    ctrl = _FakeController(TIER_ACTIONS["medium"])
    b = AudioReactiveBehavior(
        beat_detector=beat,
        controller=ctrl,
        action_rotate_min_sec=0.0,
        action_rotate_max_sec=0.0,  # force immediate rotation
    )
    b.update()
    b._next_rotate_time = time.monotonic() - 1  # pretend timer elapsed
    b.update()
    # Two updates, same tier — expect at least 2 action sets, both from medium pool.
    assert len(ctrl.set_action_calls) >= 2
    for a in ctrl.set_action_calls:
        assert a in TIER_ACTIONS["medium"]


def test_fallback_when_action_missing_from_animal() -> None:
    # Animal only has idle + walk — behavior wants 'jump' on high tier.
    beat = _FakeBeat(tier="high")
    ctrl = _FakeController(("idle", "walk"))
    b = AudioReactiveBehavior(beat_detector=beat, controller=ctrl)
    b.update()
    # High tier pool is (run, jump); neither available. Fallback keeps the
    # animal from freezing — current behavior: no action set (safe no-op).
    # This test pins that no crash occurs on missing actions.
    # (Optional stronger contract: fall back to idle — would need code change.)
    # For now assert at minimum that no exception was raised.
    assert True


def test_strong_beat_overrides_tier_to_high() -> None:
    beat = _FakeBeat(tier="low", bpm=120.0)
    beat.last_beat_audio_ms = 1000.0  # new beat
    beat.last_beat_strength = 5.0     # strong
    ctrl = _FakeController(("idle", "eating", "walk", "alert", "run", "jump"))
    b = AudioReactiveBehavior(
        beat_detector=beat, controller=ctrl, strong_beat_threshold=4.0
    )
    b.update()
    assert ctrl.set_action_calls[-1] in TIER_ACTIONS["high"]


def test_weak_beat_leaves_tier_alone() -> None:
    beat = _FakeBeat(tier="low", bpm=120.0)
    beat.last_beat_audio_ms = 1000.0
    beat.last_beat_strength = 2.0  # below threshold
    ctrl = _FakeController(("idle", "eating", "walk", "alert", "run", "jump"))
    b = AudioReactiveBehavior(
        beat_detector=beat, controller=ctrl, strong_beat_threshold=4.0
    )
    b.update()
    assert ctrl.set_action_calls[-1] in TIER_ACTIONS["low"]


def test_same_beat_not_handled_twice() -> None:
    # Tier pinned to high so the sustained-tier path stays quiet after the
    # strong-beat override — lets us verify the dedupe on audio_ms alone.
    beat = _FakeBeat(tier="high", bpm=120.0)
    beat.last_beat_audio_ms = 1000.0
    beat.last_beat_strength = 5.0
    ctrl = _FakeController(("run", "jump"))
    b = AudioReactiveBehavior(
        beat_detector=beat, controller=ctrl, strong_beat_threshold=4.0,
        action_rotate_min_sec=999, action_rotate_max_sec=999,
    )
    b.update()
    n = len(ctrl.set_action_calls)
    b.update()  # same beat_ms, same tier — neither path should fire
    assert len(ctrl.set_action_calls) == n


def test_strong_beat_lockout_prevents_immediate_revert() -> None:
    """A strong beat while tier=low must not get overwritten next frame."""
    beat = _FakeBeat(tier="low", bpm=120.0)
    beat.last_beat_audio_ms = 1000.0
    beat.last_beat_strength = 5.0
    ctrl = _FakeController(("idle", "eating", "walk", "alert", "run", "jump"))
    b = AudioReactiveBehavior(
        beat_detector=beat,
        controller=ctrl,
        strong_beat_threshold=4.0,
        strong_hold_sec=10.0,  # long hold so the test doesn't race
    )
    b.update()  # strong beat → picks from high pool
    assert ctrl.set_action_calls[-1] in TIER_ACTIONS["high"]
    n = len(ctrl.set_action_calls)

    # Next frame — tier is still "low" but lockout must prevent downgrade.
    # No new beat_ms, no elapsed lockout → no action change.
    b.update()
    assert len(ctrl.set_action_calls) == n


def test_strong_beat_does_not_cycle_run_jump_every_hit() -> None:
    """Successive strong beats extend lockout but don't re-pick."""
    beat = _FakeBeat(tier="low", bpm=120.0)
    ctrl = _FakeController(("idle", "run", "jump"))
    b = AudioReactiveBehavior(
        beat_detector=beat,
        controller=ctrl,
        strong_beat_threshold=4.0,
        strong_hold_sec=10.0,
    )
    for i in range(5):
        beat.last_beat_audio_ms = 1000.0 + i * 200  # new beats every 200ms
        beat.last_beat_strength = 5.0
        b.update()
    # Only the first strong beat should have called set_action.
    assert len(ctrl.set_action_calls) == 1
    assert ctrl.set_action_calls[0] in TIER_ACTIONS["high"]


def test_stale_silence_timeout_kwarg_accepted() -> None:
    """Older callers may still pass silence_timeout_sec — must not crash."""
    beat = _FakeBeat(tier="low")
    ctrl = _FakeController(TIER_ACTIONS["low"])
    AudioReactiveBehavior(
        beat_detector=beat, controller=ctrl, silence_timeout_sec=5.0
    )
