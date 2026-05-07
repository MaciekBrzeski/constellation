"""Master-side control channel: heartbeat listener + command sender.

Spawns a UDP listener thread on the heartbeat port. Each incoming
heartbeat updates an in-memory worker registry. The web UI calls
``send_command`` to push a unicast command back to a known worker.
"""

from __future__ import annotations

import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from constellation.control.protocol import (
    DEFAULT_COMMAND_PORT,
    DEFAULT_HEARTBEAT_PORT,
    WORKER_OFFLINE_AFTER_S,
    CommandMessage,
    decode,
)


@dataclass
class WorkerInfo:
    worker_id: str
    ip: str
    last_seen: float
    animal: str = ""
    action: str = ""
    available_animals: list[str] = field(default_factory=list)
    available_actions_by_animal: dict[str, list[str]] = field(default_factory=dict)
    tier: str = "low"
    bpm: float = 0.0
    aubio_active: bool = False

    def is_online(self, now: float) -> bool:
        return (now - self.last_seen) < WORKER_OFFLINE_AFTER_S


class ControlChannel:
    def __init__(
        self,
        heartbeat_port: int = DEFAULT_HEARTBEAT_PORT,
        command_port: int = DEFAULT_COMMAND_PORT,
        bind_host: str = "0.0.0.0",
    ) -> None:
        self._heartbeat_port = heartbeat_port
        self._command_port = command_port
        self._bind_host = bind_host

        self._lock = threading.Lock()
        self._workers: dict[str, WorkerInfo] = {}

        self._sock: socket.socket | None = None
        self._send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------------------------------------------------------------- public
    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self._bind_host, self._heartbeat_port))
        self._sock.settimeout(0.5)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="ControlChannel", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._send_sock.close()

    def workers(self) -> list[WorkerInfo]:
        """Snapshot of all known workers (online + recently offline)."""
        with self._lock:
            return list(self._workers.values())

    def send_command(self, worker_id: str, name: str, value: Any) -> bool:
        """Send a command to a worker. Returns True if the worker is known."""
        with self._lock:
            info = self._workers.get(worker_id)
        if info is None:
            return False
        msg = CommandMessage(name=name, value=value).encode()
        try:
            self._send_sock.sendto(msg, (info.ip, self._command_port))
        except OSError:
            return False
        return True

    # --------------------------------------------------------------- internal
    def _loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            self._handle_packet(data, addr[0])

    def _handle_packet(self, data: bytes, src_ip: str) -> None:
        msg = decode(data)
        if msg is None or msg.get("type") != "heartbeat":
            return
        wid = msg.get("worker_id")
        if not isinstance(wid, str) or not wid:
            return
        with self._lock:
            info = self._workers.get(wid)
            if info is None:
                info = WorkerInfo(worker_id=wid, ip=src_ip, last_seen=time.monotonic())
                self._workers[wid] = info
            info.ip = src_ip
            info.last_seen = time.monotonic()
            info.animal = str(msg.get("animal", ""))
            info.action = str(msg.get("action", ""))
            avail_a = msg.get("available_animals", [])
            if isinstance(avail_a, list):
                info.available_animals = [str(x) for x in avail_a]
            avail_act = msg.get("available_actions_by_animal", {})
            if isinstance(avail_act, dict):
                info.available_actions_by_animal = {
                    str(k): [str(v) for v in vals]
                    for k, vals in avail_act.items()
                    if isinstance(vals, list)
                }
            info.tier = str(msg.get("tier", "low"))
            try:
                info.bpm = float(msg.get("bpm", 0.0))
            except (TypeError, ValueError):
                info.bpm = 0.0
            info.aubio_active = bool(msg.get("aubio_active", False))
