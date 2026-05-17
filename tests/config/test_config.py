"""Tests for config overlay persistence (save_audio_overrides + load_config)."""

from __future__ import annotations

from dataclasses import replace

from constellation import config as cfgmod
from constellation.config import (
    AudioConfig,
    NetworkConfig,
    load_config,
    save_audio_overrides,
)


def _use_overlay(tmp_path, monkeypatch):
    overlay = tmp_path / "config.toml"
    monkeypatch.setattr(cfgmod, "user_config_path", lambda: overlay)
    return overlay


def test_save_load_round_trip(tmp_path, monkeypatch) -> None:
    _use_overlay(tmp_path, monkeypatch)
    save_audio_overrides(replace(AudioConfig(), sensitivity=2.7, device="USB Mic"))
    loaded = load_config()
    assert loaded.audio.sensitivity == 2.7
    assert loaded.audio.device == "USB Mic"


def test_overlay_keys_win_untouched_fields_keep_defaults(
    tmp_path, monkeypatch
) -> None:
    overlay = _use_overlay(tmp_path, monkeypatch)
    overlay.write_text("[audio]\nsensitivity = 3.3\n")
    loaded = load_config()
    assert loaded.audio.sensitivity == 3.3  # overlay wins
    assert loaded.audio.history_size == AudioConfig().history_size  # default


def test_save_preserves_other_overlay_sections(tmp_path, monkeypatch) -> None:
    overlay = _use_overlay(tmp_path, monkeypatch)
    overlay.write_text("[network]\nport = 50000\n")
    save_audio_overrides(replace(AudioConfig(), sensitivity=2.0))
    text = overlay.read_text()
    assert "[network]" in text and "port = 50000" in text
    assert "[audio]" in text


def test_save_omits_none_device(tmp_path, monkeypatch) -> None:
    overlay = _use_overlay(tmp_path, monkeypatch)
    save_audio_overrides(AudioConfig())  # device defaults to None
    assert "device" not in overlay.read_text()


def test_no_overlay_uses_base_config(tmp_path, monkeypatch) -> None:
    _use_overlay(tmp_path, monkeypatch)  # points at a non-existent file
    loaded = load_config()
    assert loaded.audio.device is None  # nothing persisted


def test_multiband_audio_fields_load_from_overlay(tmp_path, monkeypatch) -> None:
    overlay = _use_overlay(tmp_path, monkeypatch)
    overlay.write_text('[audio]\nbackend = "multiband"\nbass_sensitivity = 2.5\n')
    loaded = load_config()
    assert loaded.audio.backend == "multiband"
    assert loaded.audio.bass_sensitivity == 2.5


def test_save_round_trips_new_audio_fields(tmp_path, monkeypatch) -> None:
    _use_overlay(tmp_path, monkeypatch)
    save_audio_overrides(replace(AudioConfig(), backend="aubio", fft_size=2048))
    loaded = load_config()
    assert loaded.audio.backend == "aubio"
    assert loaded.audio.fft_size == 2048


def test_network_config_defaults() -> None:
    net = NetworkConfig()
    assert net.predictive_sync is True
    assert net.ap_ssid == "constellation"
    assert net.render_lead_ms == 16.0


def test_network_sync_fields_load_from_overlay(tmp_path, monkeypatch) -> None:
    overlay = _use_overlay(tmp_path, monkeypatch)
    overlay.write_text(
        "[network]\nrender_lead_ms = 24.0\npredictive_sync = false\n"
    )
    loaded = load_config()
    assert loaded.network.render_lead_ms == 24.0
    assert loaded.network.predictive_sync is False
