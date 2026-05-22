"""Tests for the out-of-process audio brain (BrainServer / BrainClient).

The brain runs in a child process so PortAudio never shares the Tk panel's
process. These tests exercise the UDP protocol with a **fake detector** — real
PortAudio open/close is flaky on the Tegra target and irrelevant to the wire
protocol, so we patch it out.
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from constellation.config import load_config
from constellation.control.brain import (
    BRAIN_CMD_PORT,
    BRAIN_TELEM_PORT,
    BrainClient,
    BrainServer,
)


class _FakeDetector:
    """Stand-in for BeatDetector — no audio, just the attributes the brain reads."""

    def __init__(self, **kwargs: object) -> None:
        self.sensitivity = 1.5
        self.cooldown_ms = 200.0
        self.noise_gate = 0.01
        self.low_intensity_ratio = 2.0
        self.high_intensity_ratio = 6.0
        self.min_noise_floor = 0.002
        self.device = kwargs.get("device")
        self.beat_event = threading.Event()
        self.energy_tier = "low"
        self.bpm = 0.0
        self.noise_floor = 0.002
        self.tier_intensity = 0.0
        self.backend_name = "multiband"
        self.band_flux: dict[str, float] = {}
        self.last_beat_audio_ms = 0.0

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass


@pytest.fixture
def fake_audio(monkeypatch):
    """Patch the brain's audio + persistence so no PortAudio or file I/O runs."""
    monkeypatch.setattr(
        "constellation.audio.beat.BeatDetector", _FakeDetector, raising=True
    )
    monkeypatch.setattr(
        "constellation.audio.beat.list_input_devices",
        lambda: [(0, "Fake Mic A"), (1, "Fake Mic B")],
        raising=True,
    )
    monkeypatch.setattr(
        "constellation.config.save_audio_overrides",
        lambda audio: None,
        raising=True,
    )
    # Avoid the real sounddevice query in _resolve_active_device_name.
    monkeypatch.setattr(
        BrainServer, "_resolve_active_device_name",
        lambda self: self._device_name, raising=True,
    )


def test_brain_client_seeds_snapshot_from_config() -> None:
    """The client has usable values before the first telemetry packet."""
    cfg = load_config(None)
    client = BrainClient(cfg, None, host="255.255.255.255", port=49500)
    assert client.backend_name == cfg.audio.backend
    assert client.param("sensitivity") == cfg.audio.sensitivity
    assert client.energy_tier == "low"
    assert client.audio_ok is False
    assert client.child_alive() is False


def _recv_telemetry(sock: socket.socket, timeout_s: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout_s
    sock.settimeout(0.3)
    while time.monotonic() < deadline:
        try:
            data, _ = sock.recvfrom(8192)
        except socket.timeout:
            continue
        msg = json.loads(data.decode("utf-8"))
        if msg.get("type") == "telemetry":
            return msg
    raise AssertionError("no telemetry packet received")


def _run_server(cfg) -> tuple[BrainServer, threading.Thread, socket.socket]:
    server = BrainServer(cfg, host="255.255.255.255", port=49500)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    panel = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    panel.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    panel.bind(("127.0.0.1", BRAIN_TELEM_PORT))
    return server, thread, panel


def test_brain_server_telemetry_and_set_param(fake_audio) -> None:
    """Server emits telemetry and applies a set_param command over UDP."""
    cfg = load_config(None)
    server, thread, panel = _run_server(cfg)
    try:
        first = _recv_telemetry(panel)
        assert first["backend"] == cfg.audio.backend
        assert "params" in first and "devices" in first and "device_active" in first

        panel.sendto(
            json.dumps({
                "type": "cmd", "name": "set_param",
                "param": "sensitivity", "value": 3.75,
            }).encode("utf-8"),
            ("127.0.0.1", BRAIN_CMD_PORT),
        )
        for _ in range(20):
            telem = _recv_telemetry(panel)
            if abs(telem["params"]["sensitivity"] - 3.75) < 1e-6:
                break
        assert abs(telem["params"]["sensitivity"] - 3.75) < 1e-6
    finally:
        server._stop.set()
        thread.join(timeout=2.0)
        panel.close()


def test_brain_server_set_device_persists(fake_audio) -> None:
    """set_device records the chosen device (reported back in telemetry)."""
    cfg = load_config(None)
    server, thread, panel = _run_server(cfg)
    try:
        _recv_telemetry(panel)
        panel.sendto(
            json.dumps({
                "type": "cmd", "name": "set_device", "value": "Fake Mic B",
            }).encode("utf-8"),
            ("127.0.0.1", BRAIN_CMD_PORT),
        )
        for _ in range(20):
            telem = _recv_telemetry(panel)
            if telem["device"] == "Fake Mic B":
                break
        assert telem["device"] == "Fake Mic B"
    finally:
        server._stop.set()
        thread.join(timeout=2.0)
        panel.close()


def test_brain_server_ignores_unknown_param(fake_audio) -> None:
    """A set_param for a non-tunable attribute is silently dropped."""
    cfg = load_config(None)
    server, thread, panel = _run_server(cfg)
    try:
        _recv_telemetry(panel)
        panel.sendto(
            json.dumps({
                "type": "cmd", "name": "set_param",
                "param": "sample_rate", "value": 999,
            }).encode("utf-8"),
            ("127.0.0.1", BRAIN_CMD_PORT),
        )
        time.sleep(0.3)
        telem = _recv_telemetry(panel)
        assert "sample_rate" not in telem["params"]
    finally:
        server._stop.set()
        thread.join(timeout=2.0)
        panel.close()
