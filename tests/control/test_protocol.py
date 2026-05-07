"""Wire-format tests for the control plane."""

from __future__ import annotations

import json

from constellation.control.protocol import (
    CommandMessage,
    HeartbeatMessage,
    decode,
)


def test_heartbeat_round_trip() -> None:
    msg = HeartbeatMessage(
        worker_id="deer-pi",
        animal="deer_male",
        action="walk",
        available_animals=["deer_male", "wolf"],
        available_actions_by_animal={"deer_male": ["idle", "walk", "run"]},
        tier="medium",
        bpm=120.0,
        aubio_active=True,
    )
    decoded = decode(msg.encode())
    assert decoded is not None
    assert decoded["type"] == "heartbeat"
    assert decoded["worker_id"] == "deer-pi"
    assert decoded["animal"] == "deer_male"
    assert decoded["available_actions_by_animal"]["deer_male"] == ["idle", "walk", "run"]


def test_command_round_trip() -> None:
    msg = CommandMessage(name="set_animal", value="wolf")
    decoded = decode(msg.encode())
    assert decoded == {"type": "cmd", "name": "set_animal", "value": "wolf"}


def test_decode_rejects_garbage() -> None:
    assert decode(b"not json") is None
    assert decode(b"\xff\xfe\xfd") is None
    assert decode(json.dumps([1, 2, 3]).encode()) is None
