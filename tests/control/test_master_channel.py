"""ControlChannel listens for heartbeats and routes commands."""

from __future__ import annotations

import socket
import time

from constellation.control.master import ControlChannel
from constellation.control.protocol import HeartbeatMessage


def _send_heartbeat(port: int, **kwargs: object) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    msg = HeartbeatMessage(
        worker_id=str(kwargs.get("worker_id", "w1")),
        animal=str(kwargs.get("animal", "deer_male")),
        action=str(kwargs.get("action", "idle")),
        available_animals=list(kwargs.get("available_animals", ["deer_male"])),
        available_actions_by_animal=dict(
            kwargs.get("available_actions_by_animal", {"deer_male": ["idle"]})
        ),
        tier=str(kwargs.get("tier", "low")),
        bpm=float(kwargs.get("bpm", 0.0)),
    )
    sock.sendto(msg.encode(), ("127.0.0.1", port))
    sock.close()


def test_channel_records_heartbeat() -> None:
    ch = ControlChannel(heartbeat_port=49531, command_port=49532, bind_host="127.0.0.1")
    ch.start()
    try:
        _send_heartbeat(49531, worker_id="alpha", animal="wolf", tier="high", bpm=140.0)
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and not ch.workers():
            time.sleep(0.01)
        workers = ch.workers()
        assert len(workers) == 1
        assert workers[0].worker_id == "alpha"
        assert workers[0].animal == "wolf"
        assert workers[0].tier == "high"
        assert workers[0].bpm == 140.0
        assert workers[0].is_online(time.monotonic())
    finally:
        ch.stop()


def test_send_command_to_known_worker() -> None:
    # Bind a listener on the worker's command port to receive whatever the
    # channel sends.
    cmd_listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cmd_listener.bind(("127.0.0.1", 49542))
    cmd_listener.settimeout(1.0)

    ch = ControlChannel(heartbeat_port=49541, command_port=49542, bind_host="127.0.0.1")
    ch.start()
    try:
        _send_heartbeat(49541, worker_id="beta")
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and not ch.workers():
            time.sleep(0.01)

        ok = ch.send_command("beta", "set_animal", "wolf")
        assert ok

        data, _ = cmd_listener.recvfrom(4096)
        import json
        msg = json.loads(data.decode())
        assert msg == {"type": "cmd", "name": "set_animal", "value": "wolf"}
    finally:
        ch.stop()
        cmd_listener.close()


def test_send_command_unknown_worker_returns_false() -> None:
    ch = ControlChannel(heartbeat_port=49551, command_port=49552, bind_host="127.0.0.1")
    ch.start()
    try:
        assert ch.send_command("nobody", "set_animal", "wolf") is False
    finally:
        ch.stop()
