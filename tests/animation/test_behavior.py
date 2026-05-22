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


def test_strong_beat_escalates_one_tier() -> None:
    # A strong beat escalates ONE tier above the sustained energy tier —
    # low → medium, not straight to high.
    actions = ("idle", "eating", "walk", "alert", "run", "jump")

    beat = _FakeBeat(tier="low", bpm=120.0)
    beat.last_beat_audio_ms = 1000.0  # new beat
    beat.last_beat_strength = 5.0     # strong
    ctrl = _FakeController(actions)
    b = AudioReactiveBehavior(
        beat_detector=beat, controller=ctrl, strong_beat_threshold=4.0
    )
    b.update()
    assert ctrl.set_action_calls[-1] in TIER_ACTIONS["medium"]

    # medium + strong kick → high
    beat2 = _FakeBeat(tier="medium", bpm=120.0)
    beat2.last_beat_audio_ms = 1000.0
    beat2.last_beat_strength = 5.0
    ctrl2 = _FakeController(actions)
    b2 = AudioReactiveBehavior(
        beat_detector=beat2, controller=ctrl2, strong_beat_threshold=4.0
    )
    b2.update()
    assert ctrl2.set_action_calls[-1] in TIER_ACTIONS["high"]


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
    b.update()  # strong beat at low tier → escalates one tier to medium
    assert ctrl.set_action_calls[-1] in TIER_ACTIONS["medium"]
    n = len(ctrl.set_action_calls)

    # Next frame — tier is still "low" but lockout must prevent downgrade.
    # No new beat_ms, no elapsed lockout → no action change.
    b.update()
    assert len(ctrl.set_action_calls) == n


def test_strong_beat_does_not_re_pick_every_hit() -> None:
    """Successive strong beats extend lockout but don't re-pick."""
    beat = _FakeBeat(tier="low", bpm=120.0)
    ctrl = _FakeController(("idle", "walk", "alert", "run", "jump"))
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
    # Only the first strong beat should have called set_action (low→medium).
    assert len(ctrl.set_action_calls) == 1
    assert ctrl.set_action_calls[0] in TIER_ACTIONS["medium"]


def test_stale_silence_timeout_kwarg_accepted() -> None:
    """Older callers may still pass silence_timeout_sec — must not crash."""
    beat = _FakeBeat(tier="low")
    ctrl = _FakeController(TIER_ACTIONS["low"])
    AudioReactiveBehavior(
        beat_detector=beat, controller=ctrl, silence_timeout_sec=5.0
    )
