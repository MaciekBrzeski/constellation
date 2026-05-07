"""Worker-side control: heartbeat sender + command listener.

The main loop polls ``apply_pending(controller)`` once per frame. Commands
arrive on a background thread and land in a thread-safe queue; the main
loop drains them on the same thread that touches the controller, so we
don't introduce data races on ``_animal_index``, ``_action``,
``_frame_index``.
"""

from __future__ import annotations

import queue
import socket
import threading
import time
from typing import TYPE_CHECKING

from constellation.control.protocol import (
    DEFAULT_COMMAND_PORT,
    DEFAULT_HEARTBEAT_PORT,
    HEARTBEAT_INTERVAL_S,
    HeartbeatMessage,
    decode,
)

if TYPE_CHECKING:
    from constellation.animation.controller import AnimationController


class WorkerControl:
    def __init__(
        self,
        worker_id: str,
        controller: "AnimationController",
        beat_source: object,
        master_host: str = "255.255.255.255",
        heartbeat_port: int = DEFAULT_HEARTBEAT_PORT,
        command_port: int = DEFAULT_COMMAND_PORT,
        bind_host: str = "0.0.0.0",
        heartbeat_interval_s: float = HEARTBEAT_INTERVAL_S,
    ) -> None:
        self._worker_id = worker_id
        self._controller = controller
        self._beat_source = beat_source
        self._master_addr = (master_host, heartbeat_port)
        self._bind_host = bind_host
        self._command_port = command_port
        self._heartbeat_interval = heartbeat_interval_s

        self._send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._send_sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._cmd_sock: socket.socket | None = None

        self._stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None
        self._cmd_thread: threading.Thread | None = None
        self._pending: queue.Queue[tuple[str, object]] = queue.Queue()

    # ---------------------------------------------------------------- public
    def start(self) -> None:
        self._cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._cmd_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._cmd_sock.bind((self._bind_host, self._command_port))
        self._cmd_sock.settimeout(0.5)
        self._stop.clear()

        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop, name="WorkerHeartbeat", daemon=True
        )
        self._heartbeat_thread.start()

        self._cmd_thread = threading.Thread(
            target=self._cmd_loop, name="WorkerCmdListener", daemon=True
        )
        self._cmd_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._cmd_sock is not None:
            try:
                self._cmd_sock.close()
            except OSError:
                pass
            self._cmd_sock = None
        for t in (self._heartbeat_thread, self._cmd_thread):
            if t is not None:
                t.join(timeout=1.0)
        self._heartbeat_thread = None
        self._cmd_thread = None
        self._send_sock.close()

    def apply_pending(self) -> None:
        """Drain queued commands on the main thread (controller-safe)."""
        while True:
            try:
                name, value = self._pending.get_nowait()
            except queue.Empty:
                return
            if name == "set_animal" and isinstance(value, str):
                self._controller.set_animal(value)
            elif name == "set_action" and isinstance(value, str):
                self._controller.set_action(value)

    # --------------------------------------------------------------- internal
    def _build_heartbeat(self) -> bytes:
        animal = self._controller.current_animal_name or ""
        action = getattr(self._controller, "_action", "") or ""
        avail_animals = list(self._controller.animal_names)
        avail_actions = {
            name: self._controller.available_actions(name) for name in avail_animals
        }
        msg = HeartbeatMessage(
            worker_id=self._worker_id,
            animal=animal,
            action=action,
            available_animals=avail_animals,
            available_actions_by_animal=avail_actions,
            tier=getattr(self._beat_source, "energy_tier", "low"),
            bpm=float(getattr(self._beat_source, "bpm", 0.0)),
            aubio_active=bool(getattr(self._beat_source, "aubio_active", False)),
        )
        return msg.encode()

    def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._send_sock.sendto(self._build_heartbeat(), self._master_addr)
            except OSError:
                pass
            # Sleep in short steps so stop() reacts quickly.
            t_end = time.monotonic() + self._heartbeat_interval
            while time.monotonic() < t_end and not self._stop.is_set():
                time.sleep(0.05)

    def _cmd_loop(self) -> None:
        assert self._cmd_sock is not None
        while not self._stop.is_set():
            try:
                data, _ = self._cmd_sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            msg = decode(data)
            if msg is None or msg.get("type") != "cmd":
                continue
            name = msg.get("name")
            value = msg.get("value")
            if isinstance(name, str):
                self._pending.put((name, value))
