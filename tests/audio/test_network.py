"""Tests for BeatBroadcaster + BeatReceiver.

Network is loopback-only — these are still hardware-free unit tests.
Each test uses a unique high port to avoid colliding with siblings or
with whatever the dev host has open.
"""

from __future__ import annotations

import json
import socket
import threading
import time

from constellation.audio.network import BeatBroadcaster, BeatReceiver


class _FakeDetector:
    """Minimal stand-in for BeatDetector.

    Exposes the surface BeatBroadcaster reads — start/stop, beat_event,
    energy_tier, bpm, last_beat_audio_ms, last_beat_strength, plus the
    sync fields (beat_count, epoch, last_beat_monotonic_ms,
    predicted_next_beat_ms).
    """

    def __init__(self) -> None:
        self.beat_event = threading.Event()
        self.energy_tier = "low"
        self.bpm = 0.0
        self.last_beat_audio_ms = 0.0
        self.last_beat_strength = 0.0
        self.beat_count = -1
        self.epoch = 0
        self.last_beat_monotonic_ms = 0.0
        self.predicted_next_beat_ms = 0.0
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True


def _recv_one(sock: socket.socket, timeout: float = 1.0) -> dict:
    sock.settimeout(timeout)
    data, _ = sock.recvfrom(4096)
    return json.loads(data.decode("utf-8"))


def test_broadcaster_emits_beat_packet() -> None:
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    detector = _FakeDetector()
    detector.energy_tier = "high"
    detector.bpm = 128.0
    detector.last_beat_audio_ms = 1234.5
    detector.last_beat_strength = 4.2
    detector.beat_event.set()

    bc = BeatBroadcaster(detector, host="127.0.0.1", port=port, send_interval_s=0.01)
    bc.start()
    try:
        # Drain packets for up to ~300 ms looking for one with beat=True.
        beat_packet = None
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            try:
                msg = _recv_one(listener, timeout=0.1)
            except socket.timeout:
                continue
            if msg.get("beat"):
                beat_packet = msg
                break
        assert beat_packet is not None, "no beat packet received"
        assert beat_packet["tier"] == "high"
        assert beat_packet["bpm"] == 128.0
        assert beat_packet["strength"] == 4.2
        assert beat_packet["ts_ms"] == 1234.5
    finally:
        bc.stop()
        listener.close()
    assert detector.started
    assert detector.stopped


def test_receiver_sets_event_on_beat_packet() -> None:
    port = 49521
    rx = BeatReceiver(port=port, bind_host="127.0.0.1", recv_timeout_s=0.05)
    rx.start()
    try:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        payload = {
            "seq": 1,
            "ts_ms": 9876.5,
            "tier": "medium",
            "bpm": 110.0,
            "beat": True,
            "strength": 3.1,
        }
        sender.sendto(json.dumps(payload).encode("utf-8"), ("127.0.0.1", port))
        sender.close()

        # Give the listener thread a moment.
        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and not rx.beat_event.is_set():
            time.sleep(0.01)
        assert rx.beat_event.is_set()
        assert rx.energy_tier == "medium"
        assert rx.bpm == 110.0
        assert rx.last_beat_audio_ms == 9876.5
        assert rx.last_beat_strength == 3.1
    finally:
        rx.stop()


def test_receiver_ignores_malformed_packets() -> None:
    port = 49522
    rx = BeatReceiver(port=port, bind_host="127.0.0.1", recv_timeout_s=0.05)
    rx.start()
    try:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(b"not json at all", ("127.0.0.1", port))
        sender.sendto(b"\x00\x01\x02\x03", ("127.0.0.1", port))
        sender.sendto(json.dumps([1, 2, 3]).encode("utf-8"), ("127.0.0.1", port))
        sender.close()

        # Listener thread should still be alive — send a valid beat next.
        time.sleep(0.1)
        assert not rx.beat_event.is_set()

        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(
            json.dumps({"beat": True, "ts_ms": 1.0, "tier": "high", "bpm": 100.0}).encode(),
            ("127.0.0.1", port),
        )
        sender.close()

        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and not rx.beat_event.is_set():
            time.sleep(0.01)
        assert rx.beat_event.is_set()
    finally:
        rx.stop()


def test_broadcaster_emits_sync_fields() -> None:
    """Every packet carries brain_ms / beat_count / epoch for the scheduler."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]

    detector = _FakeDetector()
    detector.bpm = 120.0
    detector.beat_count = 7
    detector.epoch = 3
    detector.last_beat_monotonic_ms = 5000.0
    detector.predicted_next_beat_ms = 5500.0

    bc = BeatBroadcaster(detector, host="127.0.0.1", port=port, send_interval_s=0.01)
    bc.start()
    try:
        msg = _recv_one(listener, timeout=1.0)
        assert msg["beat_count"] == 7
        assert msg["epoch"] == 3
        assert msg["beat_brain_ms"] == 5000.0
        assert msg["next_beat_brain_ms"] == 5500.0
        assert msg["interval_ms"] == 500.0  # 60000 / 120 bpm
        assert "brain_ms" in msg
    finally:
        bc.stop()
        listener.close()


def test_receiver_tracks_authoritative_beat_count() -> None:
    """consume_frame_index() returns the brain's authoritative beat index."""
    port = 49524
    rx = BeatReceiver(port=port, bind_host="127.0.0.1", recv_timeout_s=0.05)
    rx.start()
    try:
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        payload = {
            "seq": 1,
            "beat": True,
            "tier": "high",
            "bpm": 120.0,
            "brain_ms": 1000.0,
            "beat_count": 4,
            "beat_brain_ms": 1000.0,
            "next_beat_brain_ms": 0.0,
            "interval_ms": 500.0,
            "epoch": 1,
        }
        sender.sendto(json.dumps(payload).encode("utf-8"), ("127.0.0.1", port))
        sender.close()

        deadline = time.monotonic() + 0.5
        while time.monotonic() < deadline and rx.beat_count < 0:
            time.sleep(0.01)
        assert rx.beat_count == 4
        assert rx.epoch == 1
        idx = rx.consume_frame_index()
        assert idx == 4
        # No new beat → nothing fresh to consume.
        assert rx.consume_frame_index() is None
    finally:
        rx.stop()


def test_receiver_exposes_beatdetector_surface() -> None:
    """BeatReceiver must duck-type as BeatDetector for AudioReactiveBehavior."""
    rx = BeatReceiver(
        # Accepts every BeatDetector kwarg without error.
        port=49523,
        sensitivity=1.5,
        cooldown_ms=200.0,
        history_size=43,
        sample_rate=22050,
        block_size=1024,
        noise_gate=0.01,
        energy_window_size=200,
        low_intensity_ratio=2.0,
        high_intensity_ratio=6.0,
        tempo_history_size=8,
        min_noise_floor=0.002,
        use_aubio=True,
    )
    for attr in (
        "beat_event",
        "energy_tier",
        "last_beat_audio_ms",
        "last_beat_strength",
        "bpm",
        "aubio_active",
        "last_beat_time",
    ):
        assert hasattr(rx, attr), f"BeatReceiver missing {attr}"
    assert isinstance(rx.beat_event, threading.Event)
    assert rx.energy_tier == "low"
    assert rx.aubio_active is False
