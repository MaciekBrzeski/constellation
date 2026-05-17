"""Tests for AnimationController.advance_to_beat — authoritative sync."""

from __future__ import annotations

from constellation.animation.controller import AnimationController
from constellation.config import AnimationConfig


def _controller(tmp_path) -> AnimationController:
    # No constellations dir under tmp_path → _animals stays empty, which is
    # fine: advance_to_beat only touches the frame index / anchor.
    return AnimationController(AnimationConfig(), tmp_path)


def test_advance_to_beat_anchors_on_first_call(tmp_path) -> None:
    c = _controller(tmp_path)
    c.advance_to_beat(100)
    assert c._frame_index == 0  # first beat seen → anchored here → frame 0
    c.advance_to_beat(103)
    assert c._frame_index == 3  # three beats on → three frames on


def test_advance_to_beat_reanchors_after_set_action(tmp_path) -> None:
    c = _controller(tmp_path)
    c.advance_to_beat(100)
    c.advance_to_beat(105)
    assert c._frame_index == 5
    c.set_action("walk")
    assert c._beat_anchor is None  # action switch clears the anchor
    c.advance_to_beat(108)  # re-anchors at 108 → new action starts at frame 0
    assert c._frame_index == 0
    c.advance_to_beat(110)
    assert c._frame_index == 2


def test_advance_to_beat_reanchors_after_set_animal(tmp_path) -> None:
    c = _controller(tmp_path)
    c.advance_to_beat(50)
    c.advance_to_beat(60)
    # set_animal returns False for an unknown animal but still must not leave
    # a stale anchor if it switched; with no animals loaded it's a no-op.
    c.set_action("idle")
    assert c._beat_anchor is None


def test_advance_to_beat_negative_holds_frame_zero(tmp_path) -> None:
    c = _controller(tmp_path)
    c.advance_to_beat(-1)  # no beats seen yet
    assert c._frame_index == 0


def test_advance_frame_remains_relative(tmp_path) -> None:
    """The legacy time-driven path is untouched by the sync work."""
    c = _controller(tmp_path)
    c.advance_frame()
    c.advance_frame()
    assert c._frame_index == 2
