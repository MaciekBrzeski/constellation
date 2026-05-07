"""FastAPI dashboard endpoints."""

from __future__ import annotations

import time

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from constellation.control.master import ControlChannel, WorkerInfo  # noqa: E402
from constellation.web.app import create_app  # noqa: E402


class _FakeDetector:
    energy_tier = "high"
    bpm = 128.0
    last_beat_strength = 4.2
    last_beat_audio_ms = 12345.6
    sensitivity = 1.5
    noise_floor = 0.003
    tier_intensity = 8.0
    aubio_active = True


class _FakeChannel(ControlChannel):
    """Subclass that skips the UDP socket bind so tests don't need ports."""

    def __init__(self) -> None:
        # Skip ControlChannel.__init__ networking; we only exercise the
        # in-memory registry + send_command stub.
        self._lock = __import__("threading").Lock()
        self._workers: dict[str, WorkerInfo] = {}
        self._command_port = 49502
        self.sent: list[tuple[str, str, object]] = []

    def start(self) -> None:  # pragma: no cover
        pass

    def stop(self) -> None:  # pragma: no cover
        pass

    def add_worker(self, info: WorkerInfo) -> None:
        self._workers[info.worker_id] = info

    def send_command(self, worker_id: str, name: str, value: object) -> bool:
        if worker_id not in self._workers:
            return False
        self.sent.append((worker_id, name, value))
        return True


def _make_client() -> tuple[TestClient, _FakeChannel]:
    channel = _FakeChannel()
    channel.add_worker(
        WorkerInfo(
            worker_id="alpha",
            ip="127.0.0.1",
            last_seen=time.monotonic(),
            animal="deer_male",
            action="walk",
            available_animals=["deer_male", "wolf"],
            available_actions_by_animal={"deer_male": ["idle", "walk", "run"]},
            tier="high",
            bpm=128.0,
            aubio_active=True,
        )
    )
    app = create_app(_FakeDetector(), channel)
    return TestClient(app), channel


def test_state_endpoint_returns_master_and_workers() -> None:
    client, _ = _make_client()
    r = client.get("/api/state")
    assert r.status_code == 200
    data = r.json()
    assert data["master"]["tier"] == "high"
    assert data["master"]["bpm"] == 128.0
    assert data["master"]["sensitivity"] == 1.5
    assert len(data["workers"]) == 1
    assert data["workers"][0]["worker_id"] == "alpha"
    assert data["workers"][0]["online"] is True


def test_set_sensitivity_updates_detector() -> None:
    client, _ = _make_client()
    r = client.post("/api/master/sensitivity", json={"value": 2.4})
    assert r.status_code == 200
    assert r.json()["sensitivity"] == 2.4
    state = client.get("/api/state").json()
    assert state["master"]["sensitivity"] == 2.4


def test_set_sensitivity_validates_range() -> None:
    client, _ = _make_client()
    assert client.post("/api/master/sensitivity", json={"value": 0.0}).status_code == 400
    assert client.post("/api/master/sensitivity", json={"value": 100.0}).status_code == 400


def test_set_animal_routes_to_channel() -> None:
    client, channel = _make_client()
    r = client.post("/api/worker/alpha/animal", json={"value": "wolf"})
    assert r.status_code == 200
    assert channel.sent == [("alpha", "set_animal", "wolf")]


def test_set_animal_unknown_worker_returns_404() -> None:
    client, _ = _make_client()
    r = client.post("/api/worker/ghost/animal", json={"value": "wolf"})
    assert r.status_code == 404


def test_index_html_served() -> None:
    client, _ = _make_client()
    r = client.get("/")
    assert r.status_code == 200
    assert "constellation" in r.text.lower()
