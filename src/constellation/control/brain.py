"""Out-of-process audio brain for the desktop control panel.

PortAudio's audio threads race fatally with Tk/X11 inside one process on the
L4T/Tegra box (intermittent SIGILL/SIGSEGV in ``libportaudio.so.2``). The
headless brain is rock-stable, so the Tkinter panel runs the brain in a child
process and drives it over localhost UDP:

* :class:`BrainServer` — runs in the child. Owns the ``BeatDetector`` +
  ``BeatBroadcaster`` exactly as the headless master does. Listens for
  commands, emits 10 Hz telemetry, runs calibration locally.
* :class:`BrainClient` — runs in the Tk panel. Spawns the child, sends
  commands, caches the latest telemetry, and exposes a detector-like surface
  so the GUI code barely changes.

Wire format: JSON over UDP, localhost only, single instance.
  client → server on ``BRAIN_CMD_PORT``     {"type": "cmd", ...}
  server → client on ``BRAIN_TELEM_PORT``   {"type": "telemetry"|"calibration"}
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

BRAIN_CMD_PORT = 49503  # client → server
BRAIN_TELEM_PORT = 49504  # server → client
TELEMETRY_INTERVAL_S = 0.1
_LOCALHOST = "127.0.0.1"

# Detector attributes the Tuning tab / calibration may set live.
_TUNABLE = (
    "sensitivity",
    "cooldown_ms",
    "noise_gate",
    "low_intensity_ratio",
    "high_intensity_ratio",
    "min_noise_floor",
)


# --------------------------------------------------------------------------
# Server — runs in the child process
# --------------------------------------------------------------------------
class BrainServer:
    """Owns the live ``BeatDetector`` + ``BeatBroadcaster`` in a child process."""

    def __init__(self, config: Any, host: str, port: int) -> None:
        self._config = config
        self._host = host
        self._port = port
        self._detector: Any = None
        self._broadcaster: Any = None
        self._audio_ok = False
        self._calibrating = False
        self._have_baseline = False
        self._device_name: str | None = config.audio.device
        self._devices: list[str] = []
        self._stop = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._client_addr = (_LOCALHOST, BRAIN_TELEM_PORT)

    # ---------------------------------------------------------- detector setup
    def _build_detector(self) -> Any:
        from constellation.audio.beat import BeatDetector, list_input_devices

        self._devices = [name for _, name in list_input_devices()]
        cfg = self._config.audio
        device = None
        for idx, name in list_input_devices():
            if name == self._device_name:
                device = idx
                break
        return BeatDetector(
            sensitivity=cfg.sensitivity,
            cooldown_ms=cfg.cooldown_ms,
            history_size=cfg.history_size,
            sample_rate=cfg.sample_rate,
            block_size=cfg.block_size,
            noise_gate=cfg.noise_gate,
            energy_window_size=cfg.energy_window_size,
            low_intensity_ratio=cfg.low_intensity_ratio,
            high_intensity_ratio=cfg.high_intensity_ratio,
            tempo_history_size=cfg.tempo_history_size,
            min_noise_floor=cfg.min_noise_floor,
            use_aubio=cfg.use_aubio,
            device=device,
            backend=cfg.backend,
            fft_size=cfg.fft_size,
            flux_history_size=cfg.flux_history_size,
            bass_sensitivity=cfg.bass_sensitivity,
            mid_sensitivity=cfg.mid_sensitivity,
            high_sensitivity=cfg.high_sensitivity,
            bass_max_hz=cfg.bass_max_hz,
            high_min_hz=cfg.high_min_hz,
            tempo_min_bpm=cfg.tempo_min_bpm,
            tempo_max_bpm=cfg.tempo_max_bpm,
        )

    # ---------------------------------------------------------------- run loop
    def run(self) -> None:
        from constellation.audio.network import BeatBroadcaster

        self._detector = self._build_detector()
        self._broadcaster = BeatBroadcaster(
            self._detector, host=self._host, port=self._port
        )
        try:
            self._broadcaster.start()  # also starts the detector
            self._audio_ok = True
        except Exception as exc:  # noqa: BLE001 - no mic: keep serving anyway
            print(f"[brain] audio unavailable: {exc}", file=sys.stderr)
            self._audio_ok = False

        self._sock.bind((_LOCALHOST, BRAIN_CMD_PORT))
        self._sock.settimeout(TELEMETRY_INTERVAL_S)
        print(f"[brain] serving on udp/{BRAIN_CMD_PORT}, broadcasting beats", flush=True)

        last_telem = 0.0
        while not self._stop.is_set():
            # Exit if the panel that spawned us is gone (reparented to init):
            # avoids an orphaned brain holding the mic + UDP ports.
            if os.getppid() == 1:
                break
            try:
                data, _ = self._sock.recvfrom(8192)
                self._handle_command(data)
            except socket.timeout:
                pass
            except OSError:
                break
            now = time.monotonic()
            if now - last_telem >= TELEMETRY_INTERVAL_S:
                self._send_telemetry()
                last_telem = now

        try:
            self._broadcaster.stop()
        except Exception:  # noqa: BLE001
            pass
        self._sock.close()

    # --------------------------------------------------------------- commands
    def _handle_command(self, data: bytes) -> None:
        try:
            msg = json.loads(data.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(msg, dict) or msg.get("type") != "cmd":
            return
        name = msg.get("name")
        value = msg.get("value")
        if name == "set_param":
            self._set_param(msg.get("param"), value)
        elif name == "set_device":
            self._set_device(value)
        elif name == "rescan":
            from constellation.audio.beat import list_input_devices

            self._devices = [n for _, n in list_input_devices()]
        elif name == "save_overrides":
            self._save_overrides()
        elif name == "calibrate":
            threading.Thread(
                target=self._run_calibration,
                args=(msg.get("kind", "music"), float(msg.get("duration", 15.0))),
                daemon=True,
            ).start()
        elif name == "shutdown":
            self._stop.set()

    def _set_param(self, param: Any, value: Any) -> None:
        if param in _TUNABLE and isinstance(value, (int, float)):
            setattr(self._detector, param, float(value))

    def _set_device(self, name: Any) -> None:
        # Persist only — the client respawns us so the chosen device opens via
        # the stable startup path. Re-opening a PortAudio stream in place is
        # crash-prone on the Tegra box (SIGSEGV/SIGILL on stop+start), and
        # re-enumerating mid-stream returns a flaky device list.
        self._device_name = name if isinstance(name, str) else None
        self._save_overrides()

    def _save_overrides(self) -> None:
        from dataclasses import replace

        from constellation.config import save_audio_overrides

        d = self._detector
        audio = replace(
            self._config.audio,
            sensitivity=float(d.sensitivity),
            cooldown_ms=float(d.cooldown_ms),
            noise_gate=float(d.noise_gate),
            min_noise_floor=float(d.min_noise_floor),
            low_intensity_ratio=float(d.low_intensity_ratio),
            high_intensity_ratio=float(d.high_intensity_ratio),
            device=self._device_name,
        )
        self._config = replace(self._config, audio=audio)
        try:
            save_audio_overrides(audio)
        except OSError as exc:
            print(f"[brain] could not save overrides: {exc}", file=sys.stderr)

    # ------------------------------------------------------------ calibration
    def _run_calibration(self, kind: str, duration: float) -> None:
        from dataclasses import replace

        from constellation.audio import calibration as cal

        self._calibrating = True
        was_ok = self._audio_ok
        try:
            if was_ok:
                self._detector.stop()
            cfg = replace(
                self._config.audio,
                sensitivity=float(self._detector.sensitivity),
                cooldown_ms=float(self._detector.cooldown_ms),
                noise_gate=float(self._detector.noise_gate),
                min_noise_floor=float(self._detector.min_noise_floor),
                low_intensity_ratio=float(self._detector.low_intensity_ratio),
                high_intensity_ratio=float(self._detector.high_intensity_ratio),
            )
            audio = cal.record(
                duration, cfg.sample_rate, device=self._detector.device,
                countdown=False,
            )
            if kind == "baseline":
                report = cal.measure_baseline(audio, cfg.sample_rate, cfg)
                suggestions = cal.suggest_from_baseline(report)
                text = cal.format_baseline_summary(report, cfg, suggestions)
                self._have_baseline = True
            else:
                events = cal.analyse(audio, cfg.sample_rate, cfg)
                report2 = cal.build_report(
                    events, duration_s=len(audio) / cfg.sample_rate, cfg=cfg
                )
                suggestions = cal.suggest(
                    report2, cfg, have_baseline=self._have_baseline
                )
                text = cal.format_summary(report2, cfg, suggestions)
            self._reply({"type": "calibration", "kind": kind, "ok": True,
                         "suggestions": suggestions, "text": text})
        except Exception as exc:  # noqa: BLE001 - report any failure to the panel
            self._reply({"type": "calibration", "kind": kind, "ok": False,
                         "suggestions": {}, "text": str(exc)})
        finally:
            if was_ok:
                try:
                    self._detector.start()
                except Exception:  # noqa: BLE001
                    self._audio_ok = False
            self._calibrating = False

    # -------------------------------------------------------------- telemetry
    def _send_telemetry(self) -> None:
        d = self._detector
        packet = {
            "type": "telemetry",
            "audio_ok": self._audio_ok,
            "calibrating": self._calibrating,
            "tier": d.energy_tier,
            "bpm": float(d.bpm),
            "noise_floor": float(d.noise_floor),
            "tier_intensity": float(d.tier_intensity),
            "backend": d.backend_name,
            "band_flux": dict(getattr(d, "band_flux", {})),
            "last_beat_audio_ms": float(d.last_beat_audio_ms),
            "device": self._device_name,
            "device_active": self._resolve_active_device_name(),
            "devices": list(self._devices),
            "params": {p: float(getattr(d, p)) for p in _TUNABLE},
        }
        self._reply(packet)

    def _resolve_active_device_name(self) -> str | None:
        """Name of the mic PortAudio is actually capturing from, or None.

        With ``device=None`` ``sd.InputStream`` picks the system default, so the
        ``self._device_name`` field doesn't tell the panel what the user is
        truly hearing — ask PortAudio.
        """
        if not self._audio_ok or self._detector is None:
            return None
        try:
            import sounddevice as sd  # type: ignore[import-untyped]

            info = sd.query_devices(self._detector.device, "input")
            return str(info.get("name", "")) or None
        except Exception:  # noqa: BLE001 - telemetry must never raise
            return self._device_name

    def _reply(self, packet: dict[str, Any]) -> None:
        try:
            self._sock.sendto(
                json.dumps(packet).encode("utf-8"), self._client_addr
            )
        except OSError:
            pass


# --------------------------------------------------------------------------
# Client — runs in the Tkinter panel process
# --------------------------------------------------------------------------
class BrainClient:
    """Spawns and drives a :class:`BrainServer` child; mimics ``BeatDetector``.

    Read properties return the latest telemetry snapshot (seeded from config
    so the GUI has values before the first packet arrives). Tunable attributes
    are settable — a write sends a ``set_param`` command and is echoed locally
    so reads are immediate. Calibration results arrive on :attr:`cal_queue`.
    """

    def __init__(self, config: Any, config_path: Path | None,
                 host: str, port: int) -> None:
        self._config = config
        self._config_path = config_path
        self._host = host
        self._port = port
        self._proc: subprocess.Popen[bytes] | None = None
        self._sock: socket.socket | None = None
        self._listener: threading.Thread | None = None
        self._stop = threading.Event()
        # Set on the first telemetry packet — proves the child is bound and
        # ready to receive commands (UDP to an unbound port is dropped).
        self._ready = threading.Event()
        # Set while deliberately respawning the child (device switch) so the
        # panel doesn't mistake the gap for a crash.
        self._restarting = threading.Event()

        import queue

        self.cal_queue: queue.Queue = queue.Queue()
        # Snapshot seeded from config; telemetry refines it within ~100 ms.
        cfg = config.audio
        self._snap: dict[str, Any] = {
            "audio_ok": False,
            "calibrating": False,
            "tier": "low",
            "bpm": 0.0,
            "noise_floor": float(cfg.min_noise_floor),
            "tier_intensity": 0.0,
            "backend": cfg.backend,
            "band_flux": {},
            "last_beat_audio_ms": 0.0,
            "device": cfg.device,
            "device_active": None,
            "devices": [],
            "params": {p: float(getattr(cfg, p, 0.0)) for p in _TUNABLE},
        }
        self._lock = threading.Lock()

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Bind the telemetry socket and spawn the brain child process."""
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((_LOCALHOST, BRAIN_TELEM_PORT))
        self._sock.settimeout(0.5)
        self._listener = threading.Thread(
            target=self._listen, name="BrainClient", daemon=True
        )
        self._listener.start()
        self._spawn_child()

    def _spawn_child(self) -> None:
        """Spawn the brain child and block until it emits telemetry."""
        cmd = [sys.executable, "-m", "constellation.control.brain",
               "--host", self._host, "--port", str(self._port)]
        if self._config_path is not None:
            cmd += ["--config", str(self._config_path)]
        self._ready.clear()
        self._proc = subprocess.Popen(cmd)
        # Block until the child is bound and emitting telemetry, so the first
        # command the panel sends isn't dropped onto an unbound port.
        self._ready.wait(timeout=6.0)

    def stop(self) -> None:
        self._stop.set()
        self._send({"type": "cmd", "name": "shutdown"})
        proc = self._proc
        self._proc = None
        if proc is not None and proc.poll() is None:
            try:
                proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                proc.terminate()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass

    def child_alive(self) -> bool:
        if self._restarting.is_set():
            return True  # deliberate respawn in progress — not a crash
        return self._proc is not None and self._proc.poll() is None

    # --------------------------------------------------------------- listener
    def _listen(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                data, _ = self._sock.recvfrom(8192)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "telemetry":
                with self._lock:
                    for key in self._snap:
                        if key in msg:
                            self._snap[key] = msg[key]
                self._ready.set()
            elif msg.get("type") == "calibration":
                self.cal_queue.put(
                    (msg.get("kind", "music"), bool(msg.get("ok")),
                     dict(msg.get("suggestions", {})), str(msg.get("text", "")))
                )

    def _send(self, msg: dict[str, Any]) -> None:
        if self._sock is None:
            return
        try:
            self._sock.sendto(
                json.dumps(msg).encode("utf-8"), (_LOCALHOST, BRAIN_CMD_PORT)
            )
        except OSError:
            pass

    def _get(self, key: str) -> Any:
        with self._lock:
            return self._snap[key]

    # ------------------------------------------------------- commands (panel)
    def set_param(self, name: str, value: float) -> None:
        """Set a tunable detector parameter on the child, echoed locally."""
        with self._lock:
            self._snap["params"][name] = float(value)
        self._send({"type": "cmd", "name": "set_param",
                    "param": name, "value": float(value)})

    def set_device(self, name: str | None) -> None:
        """Switch the capture device by persisting it + respawning the child.

        Live re-open of the PortAudio stream is unreliable on the Tegra box, so
        instead the current child saves the choice and a fresh child opens it at
        startup. Runs on a worker thread so the GUI never blocks on the respawn.
        """
        with self._lock:
            self._snap["device"] = name
            self._snap["audio_ok"] = False
        threading.Thread(
            target=self._switch_device, args=(name,), daemon=True
        ).start()

    def _switch_device(self, name: str | None) -> None:
        self._restarting.set()
        try:
            self._send({"type": "cmd", "name": "set_device", "value": name})
            time.sleep(0.4)  # let the child persist the override
            proc = self._proc
            if proc is not None and proc.poll() is None:
                self._send({"type": "cmd", "name": "shutdown"})
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.terminate()
            self._spawn_child()
        finally:
            self._restarting.clear()

    def rescan(self) -> None:
        self._send({"type": "cmd", "name": "rescan"})

    def save_overrides(self) -> None:
        self._send({"type": "cmd", "name": "save_overrides"})

    def calibrate(self, kind: str, duration: float) -> None:
        self._send({"type": "cmd", "name": "calibrate",
                    "kind": kind, "duration": float(duration)})

    # ---------------------------------------------- detector-like read surface
    @property
    def audio_ok(self) -> bool:
        return bool(self._get("audio_ok"))

    @property
    def calibrating(self) -> bool:
        return bool(self._get("calibrating"))

    @property
    def energy_tier(self) -> str:
        return str(self._get("tier"))

    @property
    def bpm(self) -> float:
        return float(self._get("bpm"))

    @property
    def noise_floor(self) -> float:
        return float(self._get("noise_floor"))

    @property
    def tier_intensity(self) -> float:
        return float(self._get("tier_intensity"))

    @property
    def backend_name(self) -> str:
        return str(self._get("backend"))

    @property
    def band_flux(self) -> dict[str, float]:
        return dict(self._get("band_flux"))

    @property
    def last_beat_audio_ms(self) -> float:
        return float(self._get("last_beat_audio_ms"))

    @property
    def device(self) -> str | None:
        dev = self._get("device")
        return dev if isinstance(dev, str) else None

    @property
    def device_active(self) -> str | None:
        """Name of the mic PortAudio is actually capturing from."""
        dev = self._get("device_active")
        return dev if isinstance(dev, str) else None

    @property
    def devices(self) -> list[str]:
        return list(self._get("devices"))

    def param(self, name: str) -> float:
        with self._lock:
            return float(self._snap["params"].get(name, 0.0))


# --------------------------------------------------------------------------
# Child entry point — `python -m constellation.control.brain`
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> None:
    from constellation.config import load_config

    parser = argparse.ArgumentParser(prog="constellation-brain")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--host", default="255.255.255.255")
    parser.add_argument("--port", type=int, default=49500)
    args = parser.parse_args(argv)

    config = load_config(args.config)
    server = BrainServer(config, host=args.host, port=args.port)
    try:
        server.run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
