"""Worker-side heartbeat sender + command listener."""

from __future__ import annotations

import json
import socket
import time

from constellation.control.protocol import CommandMessage
from constellation.control.worker import WorkerControl


class _FakeController:
    """Minimal AnimationController stand-in.

    Only the surface that ``WorkerControl`` reads / writes — animal
    naming + ``set_animal`` / ``set_action``.
    """

    def __init__(self) -> None:
        self.current_animal_name = "deer_male"
        self._action = "idle"
        self.animal_names = ["deer_male", "wolf"]
        self.last_set_animal: str | None = None
        self.last_set_action: str | None = None

    def available_actions(self, animal_name: str | None = None) -> list[str]:
        return ["idle", "walk", "run"]

    def set_animal(self, name: str) -> bool:
        self.last_set_animal = name
        if name in self.animal_names:
            self.current_animal_name = name
            return True
        return False

    def set_action(self, name: str) -> None:
        self.last_set_action = name
        self._action = name


class _FakeBeatSource:
    energy_tier = "medium"
    bpm = 110.0
    aubio_active = True


def test_heartbeat_includes_state_and_animals() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 49561))
    listener.settimeout(2.0)

    wc = WorkerControl(
        worker_id="worker-x",
        controller=_FakeController(),
        beat_source=_FakeBeatSource(),
        master_host="127.0.0.1",
        heartbeat_port=49561,
        command_port=49562,
        bind_host="127.0.0.1",
        heartbeat_interval_s=0.1,
    )
    wc.start()
    try:
        data, _ = listener.recvfrom(4096)
        msg = json.loads(data.decode())
        assert msg["type"] == "heartbeat"
        assert msg["worker_id"] == "worker-x"
        assert msg["animal"] == "deer_male"
        assert msg["available_animals"] == ["deer_male", "wolf"]
        assert msg["available_actions_by_animal"]["deer_male"] == ["idle", "walk", "run"]
        assert msg["tier"] == "medium"
        assert msg["bpm"] == 110.0
        assert msg["aubio_active"] is True
    finally:
        wc.stop()
        listener.close()


def test_command_queued_and_applied_on_apply_pending() -> None:
    controller = _FakeController()
    wc = WorkerControl(
        worker_id="worker-y",
        controller=controller,
        beat_source=_FakeBeatSource(),
        master_host="127.0.0.1",
        heartbeat_port=49571,
        command_port=49572,
        bind_host="127.0.0.1",
        heartbeat_interval_s=10.0,
    )
    wc.start()
    try:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(
            CommandMessage(name="set_animal", value="wolf").encode(),
            ("127.0.0.1", 49572),
        )
        sender.sendto(
            CommandMessage(name="set_action", value="run").encode(),
            ("127.0.0.1", 49572),
        )
        sender.close()

        # Give the listener thread time to enqueue.
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and (
            controller.last_set_animal is None
            or controller.last_set_action is None
        ):
            wc.apply_pending()
            time.sleep(0.02)

        assert controller.last_set_animal == "wolf"
        assert controller.last_set_action == "run"
    finally:
        wc.stop()
