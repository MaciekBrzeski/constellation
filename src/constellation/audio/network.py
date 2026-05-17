"""UDP beat synchronisation for multi-projector deployments.

One brain Pi runs ``BeatBroadcaster`` (wraps a real ``BeatDetector`` +
microphone, emits state packets). Worker Pis run ``BeatReceiver``, a
drop-in replacement for ``BeatDetector`` that exposes the same surface
(``beat_event``, ``energy_tier``, ``last_beat_audio_ms``,
``last_beat_strength``, ``bpm``, ``aubio_active``) so
``AudioReactiveBehavior`` and ``cli/run.py`` work unchanged.

Wire format: JSON, ~50 Hz, ~200 bytes each, sent to a UDP broadcast
address. Every packet carries an authoritative ``beat_count`` and the
brain's ``brain_ms`` clock so workers can frame-lock and predict the next
beat — see :class:`~constellation.audio.sync.BeatScheduler`.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any

from constellation.audio.sync import BeatScheduler

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
            now_brain_ms = time.monotonic() * 1000.0
            bpm = float(self._detector.bpm)
            # New sync fields read via getattr so a detector predating them
            # (or a test fake) still broadcasts — receivers tolerate the
            # defaults. beat_count/brain_ms ride every packet so a worker that
            # missed the beat packet self-heals from the next state packet.
            payload = {
                "seq": self._seq,
                "ts_ms": float(self._detector.last_beat_audio_ms)
                if beat
                else now_brain_ms,
                "tier": self._detector.energy_tier,
                "bpm": bpm,
                "beat": beat,
                "strength": float(self._detector.last_beat_strength) if beat else 0.0,
                "brain_ms": now_brain_ms,
                "beat_count": int(getattr(self._detector, "beat_count", -1)),
                "beat_brain_ms": float(
                    getattr(self._detector, "last_beat_monotonic_ms", 0.0)
                ),
                "next_beat_brain_ms": float(
                    getattr(self._detector, "predicted_next_beat_ms", 0.0)
                ),
                "interval_ms": 60000.0 / bpm if bpm > 0 else 0.0,
                "epoch": int(getattr(self._detector, "epoch", 0)),
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
        render_lead_ms: float = 16.0,
        offset_window_size: int = 64,
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

        # Authoritative frame-index sync — see BeatScheduler.
        self._scheduler = BeatScheduler(
            render_lead_ms=render_lead_ms,
            offset_window_size=offset_window_size,
        )
        self._last_consumed_index = -1

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

    @property
    def beat_count(self) -> int:
        """Authoritative beat index from the scheduler (-1 before any beat)."""
        return self._scheduler.count

    @property
    def epoch(self) -> int:
        """Sync epoch last seen from the brain."""
        return self._scheduler.epoch

    @property
    def clock_offset_ms(self) -> float:
        """Estimated brain→local clock offset (for --debug-sync)."""
        return self._scheduler.clock_offset_ms

    def consume_frame_index(self) -> int | None:
        """Authoritative frame index when it advanced since the last call.

        Returns the new absolute beat index when the scheduler stepped
        forward (a beat boundary was crossed), else ``None``. Poll once per
        render frame from the main thread.
        """
        idx = self._scheduler.poll(time.monotonic() * 1000.0)
        if idx != self._last_consumed_index:
            self._last_consumed_index = idx
            return idx
        return None

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
        self._scheduler.feed(msg, time.monotonic() * 1000.0)
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
