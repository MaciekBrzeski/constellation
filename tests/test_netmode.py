"""Tests for the network-mode helper (constellation.netmode).

subprocess.run is monkeypatched throughout so no nmcli / shell scripts run.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from constellation import netmode


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_scripts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend the scripts dir exists so _run gets past its guard."""
    monkeypatch.setattr(netmode, "_scripts_dir", lambda: Path("/fake/scripts/switch"))


def test_current_mode_parses_word(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_scripts(monkeypatch)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(0, "ap\n"))
    assert netmode.current_mode() == "ap"


def test_current_mode_takes_last_line(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_scripts(monkeypatch)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(0, "noise\nwifi\n"))
    assert netmode.current_mode() == "wifi"


def test_current_mode_unknown_on_garbage(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_scripts(monkeypatch)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(0, "weird"))
    assert netmode.current_mode() == "unknown"


def test_current_mode_unknown_when_scripts_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(netmode, "_scripts_dir", lambda: None)
    assert netmode.current_mode() == "unknown"


def test_switch_to_ap_success(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_scripts(monkeypatch)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(0, "AP up"))
    ok, output = netmode.switch_to_ap()
    assert ok is True
    assert "AP up" in output


def test_switch_to_wifi_failure_includes_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_scripts(monkeypatch)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FakeProc(1, "", "boom"))
    ok, output = netmode.switch_to_wifi()
    assert ok is False
    assert "boom" in output


def test_run_reports_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_scripts(monkeypatch)

    def _raise(*_a: object, **_k: object) -> None:
        raise subprocess.TimeoutExpired(cmd="mode-ap.sh", timeout=1.0)

    monkeypatch.setattr(subprocess, "run", _raise)
    ok, output = netmode.switch_to_ap()
    assert ok is False
    assert "timed out" in output


def test_switch_fails_when_scripts_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(netmode, "_scripts_dir", lambda: None)
    ok, output = netmode.switch_to_ap()
    assert ok is False
    assert "not found" in output


def test_scripts_available_in_repo_checkout() -> None:
    # The real scripts ship in scripts/switch/ of this repo.
    assert netmode.scripts_available() is True
