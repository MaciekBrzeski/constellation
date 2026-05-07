"""UDP beat synchronisation for multi-projector deployments.

One brain Pi runs ``BeatBroadcaster`` (wraps a real ``BeatDetector`` +
microphone, emits state packets). Worker Pis run ``BeatReceiver``, a
drop-in replacement for ``BeatDetector`` that exposes the same surface
(``beat_event``, ``energy_tier``, ``last_beat_audio_ms``,
``last_beat_strength``, ``bpm``, ``aubio_active``) so
``AudioReactiveBehavior`` and ``cli/run.py`` work unchanged.

Wire format: JSON, one packet per audio block, ~120 bytes each. Sent
to a UDP broadcast address. No clock-sync required — the broadcaster's
audio clock is the only timeline that matters; receivers just dedupe
on monotonic ``ts_ms``.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any

DEFAULT_PORT = 49500
DEFAULT_BROADCAST_HOST = "255.255.255.255"
SEND_INTERVAL_S = 0.02  # 50 Hz


class BeatBroadcaster:
    """Wrap a ``BeatDetector`` and publish its state over UDP.

    Polls the underlying detector at ~50 Hz. Whenever ``beat_event`` is
    set, emits one packet with ``beat=True``; in all other ticks emits
    a state-only packet so receivers track tier and BPM continuously.
    """

    def __init__(
        self,
        detector: Any,
        host: str = DEFAULT_BROADCAST_HOST,
        port: int = DEFAULT_PORT,
        send_interval_s: float = SEND_INTERVAL_S,
    ) -> None:
        self._detector = detector
        self._addr = (host, port)
        self._send_interval = send_interval_s
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._seq = 0

    def start(self) -> None:
        self._detector.start()
        self._thread = threading.Thread(
            target=self._loop, name="BeatBroadcaster", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            beat = self._detector.beat_event.is_set()
            if beat:
                self._detector.beat_event.clear()
            payload = {
                "seq": self._seq,
                "ts_ms": float(self._detector.last_beat_audio_ms)
                if beat
                else time.monotonic() * 1000.0,
                "tier": self._detector.energy_tier,
                "bpm": float(self._detector.bpm),
                "beat": beat,
                "strength": float(self._detector.last_beat_strength) if beat else 0.0,
            }
            self._seq += 1
            try:
                self._sock.sendto(json.dumps(payload).encode("utf-8"), self._addr)
            except OSError:
                pass
            time.sleep(self._send_interval)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        try:
            self._detector.stop()
        except Exception:
            pass
        self._sock.close()


class BeatReceiver:
    """UDP-driven drop-in replacement for ``BeatDetector``.

    Accepts (and ignores) every constructor kwarg ``BeatDetector`` takes
    so callers can swap one for the other without branching. Spawns a
    listener thread that updates internal state from inbound JSON
    packets and sets ``beat_event`` whenever a packet's ``beat`` flag
    is true.
    """

    def __init__(
        self,
        port: int = DEFAULT_PORT,
        bind_host: str = "0.0.0.0",
        recv_timeout_s: float = 0.5,
        **_unused: object,
    ) -> None:
        self._port = port
        self._bind_host = bind_host
        self._recv_timeout = recv_timeout_s

        self._beat_event = threading.Event()
        self._tier = "low"
        self._bpm = 0.0
        self._last_beat_audio_ms = 0.0
        self._last_beat_strength = 0.0
        self._last_packet_ts = 0.0

        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------- properties
    @property
    def beat_event(self) -> threading.Event:
        return self._beat_event

    @property
    def energy_tier(self) -> str:
        return self._tier

    @property
    def bpm(self) -> float:
        return self._bpm

    @property
    def last_beat_audio_ms(self) -> float:
        return self._last_beat_audio_ms

    @property
    def last_beat_strength(self) -> float:
        return self._last_beat_strength

    @property
    def last_beat_time(self) -> float:
        return self._last_packet_ts

    @property
    def aubio_active(self) -> bool:
        return False

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((self._bind_host, self._port))
        self._sock.settimeout(self._recv_timeout)
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="BeatReceiver", daemon=True
        )
        self._thread.start()

    def _loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data, _ = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            self._handle_packet(data)

    def _handle_packet(self, data: bytes) -> None:
        try:
            msg = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(msg, dict):
            return
        tier = msg.get("tier")
        if isinstance(tier, str):
            self._tier = tier
        bpm = msg.get("bpm")
        if isinstance(bpm, (int, float)):
            self._bpm = float(bpm)
        self._last_packet_ts = time.monotonic()
        if msg.get("beat"):
            ts_ms = msg.get("ts_ms")
            if isinstance(ts_ms, (int, float)):
                self._last_beat_audio_ms = float(ts_ms)
            strength = msg.get("strength")
            if isinstance(strength, (int, float)):
                self._last_beat_strength = float(strength)
            self._beat_event.set()

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
