"""Native Tkinter control panel for the constellation brain.

A five-tab desktop window wrapping the live ``BeatDetector`` + UDP
``BeatBroadcaster`` + ``ControlChannel``: monitor beat detection, pick the
microphone, tune the detector, run calibration, and remote-control worker Pis.

Threading: Tk's ``mainloop`` and every widget access stay on the main thread;
the detector/broadcaster/control plane keep their own background threads.
Live state is bridged by a 10 Hz ``root.after`` poll (plain attribute reads).
The single blocking job — calibration recording — runs on a worker thread and
hands its result back through a :class:`queue.Queue`.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from dataclasses import replace
from tkinter import messagebox, ttk

from constellation.audio.beat import BeatDetector, list_input_devices
from constellation.audio.calibration import (
    analyse,
    build_report,
    format_summary,
    record,
    suggest,
)
from constellation.audio.network import BeatBroadcaster
from constellation.config import AppConfig, save_audio_overrides
from constellation.control.master import ControlChannel

POLL_MS = 100
_TIER_COLORS = {"low": "#7aa9ff", "medium": "#ffd760", "high": "#ff6b6b"}
_DEFAULT_DEVICE_LABEL = "(system default)"

# (label, detector attribute, min, max) for the Tuning tab sliders.
_SLIDERS = (
    ("Sensitivity", "sensitivity", 0.1, 10.0),
    ("Noise gate", "noise_gate", 0.001, 0.1),
    ("Cooldown (ms)", "cooldown_ms", 50.0, 1000.0),
    ("Low intensity ratio", "low_intensity_ratio", 1.0, 10.0),
    ("High intensity ratio", "high_intensity_ratio", 2.0, 20.0),
)


class ControlApp:
    """The control-panel window. Construct with a Tk root, then ``run()``."""

    def __init__(
        self,
        root: tk.Tk,
        detector: BeatDetector,
        broadcaster: BeatBroadcaster,
        control: ControlChannel,
        config: AppConfig,
        audio_ok: bool,
    ) -> None:
        self.root = root
        self.detector = detector
        self.broadcaster = broadcaster
        self.control = control
        self.config = config
        self._audio_ok = audio_ok
        self._running = True

        self._device_name: str | None = config.audio.device
        self._device_map: dict[str, int | None] = {}
        self._last_beat_ms = detector.last_beat_audio_ms
        self._cal_queue: queue.Queue = queue.Queue()
        self._cal_suggestions: dict[str, float] = {}
        self._worker_ids: tuple[str, ...] = ()
        self._worker_rows: dict[str, dict] = {}

        root.title("Constellation Control")
        root.geometry("560x600")
        root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._notebook = ttk.Notebook(root)
        self._notebook.pack(fill="both", expand=True, padx=8, pady=8)
        self._build_status_tab()
        self._build_microphone_tab()
        self._build_tuning_tab()
        self._build_calibration_tab()
        self._build_workers_tab()

        self._refresh_status_banner()

    # ------------------------------------------------------------- run/shutdown
    def run(self) -> None:
        self.root.after(POLL_MS, self._poll)
        self.root.mainloop()

    def _on_close(self) -> None:
        self._running = False
        self.root.destroy()

    # ----------------------------------------------------------------- tab: status
    def _build_status_tab(self) -> None:
        frame = ttk.Frame(self._notebook, padding=12)
        self._notebook.add(frame, text="Status")

        self._banner = ttk.Label(frame, text="", wraplength=480)
        self._banner.grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))

        self._stat_vars: dict[str, tk.StringVar] = {}
        rows = [
            ("Energy tier", "tier"),
            ("BPM", "bpm"),
            ("Noise floor", "floor"),
            ("Tier intensity", "intensity"),
            ("aubio tempo tracker", "aubio"),
        ]
        for i, (label, key) in enumerate(rows, start=1):
            ttk.Label(frame, text=label).grid(row=i, column=0, sticky="w", pady=3)
            var = tk.StringVar(value="—")
            self._stat_vars[key] = var
            lbl = ttk.Label(frame, textvariable=var, font=("TkDefaultFont", 11, "bold"))
            lbl.grid(row=i, column=1, sticky="w", padx=12, pady=3)
            if key == "tier":
                self._tier_label = lbl

        ttk.Label(frame, text="Beat").grid(row=6, column=0, sticky="w", pady=3)
        self._beat_canvas = tk.Canvas(
            frame, width=28, height=28, highlightthickness=0
        )
        self._beat_canvas.grid(row=6, column=1, sticky="w", padx=12, pady=3)
        self._beat_dot = self._beat_canvas.create_oval(
            4, 4, 24, 24, fill="#2a2d38", outline=""
        )

    # ------------------------------------------------------------ tab: microphone
    def _build_microphone_tab(self) -> None:
        frame = ttk.Frame(self._notebook, padding=12)
        self._notebook.add(frame, text="Microphone")

        ttk.Label(frame, text="Input device").grid(row=0, column=0, sticky="w")
        self._device_combo = ttk.Combobox(frame, state="readonly", width=42)
        self._device_combo.grid(row=0, column=1, sticky="w", padx=8)
        self._device_combo.bind("<<ComboboxSelected>>", self._on_device_selected)

        ttk.Button(frame, text="Rescan", command=self._rescan_devices).grid(
            row=1, column=1, sticky="w", padx=8, pady=8
        )
        self._mic_status = ttk.Label(frame, text="", wraplength=480)
        self._mic_status.grid(row=2, column=0, columnspan=2, sticky="w", pady=8)
        self._rescan_devices()

    def _rescan_devices(self) -> None:
        devices = list_input_devices()
        self._device_map = {_DEFAULT_DEVICE_LABEL: None}
        labels = [_DEFAULT_DEVICE_LABEL]
        selected = _DEFAULT_DEVICE_LABEL
        for idx, name in devices:
            label = f"{idx}: {name}"
            self._device_map[label] = idx
            labels.append(label)
            if name == self._device_name:
                selected = label
        self._device_combo.configure(values=labels)
        self._device_combo.set(selected)
        if devices:
            self._mic_status.configure(text=f"{len(devices)} input device(s) found.")
        else:
            self._mic_status.configure(
                text="No input devices. Install libportaudio2 "
                "(offline/debs/libportaudio2_arm64.deb) and rescan."
            )

    def _on_device_selected(self, _event=None) -> None:
        label = self._device_combo.get()
        if label not in self._device_map:
            return
        index = self._device_map[label]
        name = None if index is None else label.split(": ", 1)[1]
        prev_device = self.detector.device
        prev_ok = self._audio_ok

        self._device_combo.configure(state="disabled")
        self._mic_status.configure(text="switching microphone…")
        self.root.update_idletasks()
        try:
            if self._audio_ok:
                self.detector.stop()
            self.detector.device = index
            self.detector.start()
            self._audio_ok = True
            self._device_name = name
            self._save_audio()
            self._mic_status.configure(text=f"now capturing from: {label}")
        except Exception as exc:  # noqa: BLE001 - surface any audio error
            self.detector.device = prev_device
            self._audio_ok = False
            if prev_ok:
                try:
                    self.detector.start()
                    self._audio_ok = True
                except Exception:  # noqa: BLE001
                    pass
            messagebox.showerror("Microphone", f"Could not open device:\n{exc}")
            self._mic_status.configure(text="device switch failed — reverted.")
        finally:
            self._device_combo.configure(state="readonly")
            self._refresh_status_banner()

    # ------------------------------------------------------------- tab: tuning
    def _build_tuning_tab(self) -> None:
        frame = ttk.Frame(self._notebook, padding=12)
        self._notebook.add(frame, text="Tuning")

        self._sliders: dict[str, ttk.Scale] = {}
        self._slider_vars: dict[str, tk.StringVar] = {}
        for i, (label, attr, lo, hi) in enumerate(_SLIDERS):
            ttk.Label(frame, text=label).grid(row=i, column=0, sticky="w", pady=6)
            value = float(getattr(self.detector, attr))
            # Register the var/scale before scale.set() — ttk.Scale fires its
            # command callback on set(), and _on_slider looks both up.
            var = tk.StringVar(value=self._fmt_slider(attr, value))
            self._slider_vars[attr] = var
            scale = ttk.Scale(
                frame, from_=lo, to=hi, length=300,
                command=lambda v, a=attr: self._on_slider(a, float(v)),  # type: ignore[misc]
            )
            self._sliders[attr] = scale
            scale.set(value)
            scale.grid(row=i, column=1, sticky="w", padx=8)
            scale.bind("<ButtonRelease-1>", lambda _e: self._save_audio())
            ttk.Label(frame, textvariable=var, width=8).grid(row=i, column=2, sticky="w")

    @staticmethod
    def _fmt_slider(attr: str, value: float) -> str:
        if attr == "cooldown_ms":
            return f"{value:.0f}"
        if attr == "noise_gate":
            return f"{value:.4f}"
        return f"{value:.2f}"

    def _on_slider(self, attr: str, value: float) -> None:
        setattr(self.detector, attr, value)
        self._slider_vars[attr].set(self._fmt_slider(attr, value))

    # --------------------------------------------------------- tab: calibration
    def _build_calibration_tab(self) -> None:
        frame = ttk.Frame(self._notebook, padding=12)
        self._notebook.add(frame, text="Calibration")

        ttk.Label(frame, text="Duration (s)").grid(row=0, column=0, sticky="w")
        self._cal_duration = tk.StringVar(value="15")
        ttk.Spinbox(
            frame, from_=3, to=120, width=6, textvariable=self._cal_duration
        ).grid(row=0, column=1, sticky="w", padx=8)
        self._cal_button = ttk.Button(
            frame, text="Record + analyse", command=self._start_calibration
        )
        self._cal_button.grid(row=0, column=2, sticky="w", padx=8)
        self._cal_apply = ttk.Button(
            frame, text="Apply suggested", command=self._apply_suggested,
            state="disabled",
        )
        self._cal_apply.grid(row=0, column=3, sticky="w", padx=8)

        self._cal_text = tk.Text(frame, width=64, height=24, state="disabled")
        self._cal_text.grid(row=1, column=0, columnspan=4, sticky="nsew", pady=10)
        self._set_cal_text("Play music near the mic, then record a sample.")

    def _set_cal_text(self, text: str) -> None:
        self._cal_text.configure(state="normal")
        self._cal_text.delete("1.0", "end")
        self._cal_text.insert("1.0", text)
        self._cal_text.configure(state="disabled")

    def _start_calibration(self) -> None:
        try:
            duration = float(self._cal_duration.get())
        except ValueError:
            messagebox.showerror("Calibration", "Duration must be a number.")
            return
        cfg_audio = self._current_audio()
        self._cal_button.configure(state="disabled")
        self._cal_apply.configure(state="disabled")
        self._device_combo.configure(state="disabled")
        self._set_cal_text(f"recording {duration:.0f}s — make some noise…")
        threading.Thread(
            target=self._calibration_worker,
            args=(duration, cfg_audio),
            daemon=True,
        ).start()

    def _calibration_worker(self, duration: float, cfg_audio) -> None:
        """Runs off the main thread; results go back via the queue."""
        was_running = self._audio_ok
        try:
            if was_running:
                self.detector.stop()
            audio = record(
                duration, cfg_audio.sample_rate,
                device=self.detector.device, countdown=False,
            )
            events = analyse(audio, cfg_audio.sample_rate, cfg_audio)
            report = build_report(
                events, duration_s=len(audio) / cfg_audio.sample_rate, cfg=cfg_audio
            )
            suggestions = suggest(report, cfg_audio)
            text = format_summary(report, cfg_audio, suggestions)
            self._cal_queue.put(("ok", suggestions, text))
        except Exception as exc:  # noqa: BLE001 - report any failure to the UI
            self._cal_queue.put(("error", {}, str(exc)))
        finally:
            if was_running:
                try:
                    self.detector.start()
                except Exception:  # noqa: BLE001
                    pass

    def _drain_cal_queue(self) -> None:
        try:
            while True:
                status, suggestions, payload = self._cal_queue.get_nowait()
                if status == "ok":
                    self._cal_suggestions = suggestions
                    self._set_cal_text(payload)
                    self._cal_apply.configure(
                        state="normal" if suggestions else "disabled"
                    )
                else:
                    self._set_cal_text(f"calibration failed:\n{payload}")
                self._cal_button.configure(state="normal")
                self._device_combo.configure(state="readonly")
        except queue.Empty:
            pass

    def _apply_suggested(self) -> None:
        for key, value in self._cal_suggestions.items():
            setattr(self.detector, key, value)
            if key in self._sliders:
                self._sliders[key].set(value)  # fires _on_slider → label sync
        self._save_audio()
        self._cal_apply.configure(state="disabled")

    # ------------------------------------------------------------- tab: workers
    def _build_workers_tab(self) -> None:
        frame = ttk.Frame(self._notebook, padding=12)
        self._notebook.add(frame, text="Workers")
        self._workers_frame = frame
        self._workers_empty = ttk.Label(
            frame, text="Waiting for worker heartbeats…"
        )
        self._workers_empty.grid(row=0, column=0, sticky="w")

    def _refresh_workers(self) -> None:
        workers = sorted(self.control.workers(), key=lambda w: w.worker_id)
        ids = tuple(w.worker_id for w in workers)
        if ids != self._worker_ids:
            self._rebuild_worker_rows(workers)
            self._worker_ids = ids
        import time as _t
        now = _t.monotonic()
        for w in workers:
            row = self._worker_rows.get(w.worker_id)
            if row is None:
                continue
            online = w.is_online(now)
            row["status"].configure(
                text="● online" if online else "○ offline",
                foreground="#5fd17a" if online else "#888",
            )
            row["meta"].configure(text=f"tier {w.tier}  ·  {w.bpm:.0f} BPM")

    def _rebuild_worker_rows(self, workers) -> None:
        for child in self._workers_frame.winfo_children():
            child.destroy()
        self._worker_rows = {}
        if not workers:
            ttk.Label(
                self._workers_frame, text="Waiting for worker heartbeats…"
            ).grid(row=0, column=0, sticky="w")
            return
        for i, w in enumerate(workers):
            box = ttk.LabelFrame(
                self._workers_frame, text=f"{w.worker_id}  ({w.ip})", padding=8
            )
            box.grid(row=i, column=0, sticky="ew", pady=4)
            status = ttk.Label(box, text="")
            status.grid(row=0, column=0, sticky="w")
            meta = ttk.Label(box, text="")
            meta.grid(row=0, column=1, sticky="w", padx=12)

            ttk.Label(box, text="Animal").grid(row=1, column=0, sticky="w", pady=4)
            animal = ttk.Combobox(
                box, state="readonly", width=18, values=w.available_animals
            )
            animal.set(w.animal)
            animal.grid(row=1, column=1, sticky="w", padx=8)

            ttk.Label(box, text="Action").grid(row=2, column=0, sticky="w", pady=4)
            actions = w.available_actions_by_animal.get(w.animal, [])
            action = ttk.Combobox(box, state="readonly", width=18, values=actions)
            action.set(w.action)
            action.grid(row=2, column=1, sticky="w", padx=8)

            wid = w.worker_id
            animal.bind(
                "<<ComboboxSelected>>",
                lambda _e, i=wid, c=animal:  # type: ignore[misc]
                self._send_worker(i, "set_animal", c.get()),
            )
            action.bind(
                "<<ComboboxSelected>>",
                lambda _e, i=wid, c=action:  # type: ignore[misc]
                self._send_worker(i, "set_action", c.get()),
            )
            self._worker_rows[wid] = {
                "status": status, "meta": meta,
                "animal": animal, "action": action,
            }

    def _send_worker(self, worker_id: str, name: str, value: str) -> None:
        if not self.control.send_command(worker_id, name, value):
            messagebox.showwarning(
                "Workers", f"Worker '{worker_id}' is no longer reachable."
            )

    # ----------------------------------------------------------------- helpers
    def _current_audio(self):
        """An ``AudioConfig`` snapshot reflecting the live detector state."""
        return replace(
            self.config.audio,
            sensitivity=float(self.detector.sensitivity),
            cooldown_ms=float(self.detector.cooldown_ms),
            noise_gate=float(self.detector.noise_gate),
            low_intensity_ratio=float(self.detector.low_intensity_ratio),
            high_intensity_ratio=float(self.detector.high_intensity_ratio),
            device=self._device_name,
        )

    def _save_audio(self) -> None:
        audio = self._current_audio()
        self.config = replace(self.config, audio=audio)
        try:
            save_audio_overrides(audio)
        except OSError as exc:
            messagebox.showwarning("Save", f"Could not save settings:\n{exc}")

    def _refresh_status_banner(self) -> None:
        if self._audio_ok:
            self._banner.configure(
                text="Microphone capturing — beat broadcast is live.",
                foreground="#5fd17a",
            )
        else:
            self._banner.configure(
                text="Audio unavailable. Install libportaudio2, then pick a "
                "device on the Microphone tab.",
                foreground="#ff6b6b",
            )

    def _flash_beat(self) -> None:
        self._beat_canvas.itemconfigure(self._beat_dot, fill="#ffd760")
        self.root.after(
            120, lambda: self._beat_canvas.itemconfigure(self._beat_dot, fill="#2a2d38")
        )

    # -------------------------------------------------------------------- poll
    def _poll(self) -> None:
        if not self._running:
            return
        self._drain_cal_queue()

        d = self.detector
        tier = d.energy_tier
        self._stat_vars["tier"].set(tier)
        self._tier_label.configure(foreground=_TIER_COLORS.get(tier, "#e6e8ee"))
        self._stat_vars["bpm"].set(f"{d.bpm:.1f}")
        self._stat_vars["floor"].set(f"{d.noise_floor:.4f}")
        self._stat_vars["intensity"].set(f"{d.tier_intensity:.2f}")
        self._stat_vars["aubio"].set("active" if d.aubio_active else "off (RMS)")

        beat_ms = d.last_beat_audio_ms
        if beat_ms > self._last_beat_ms:
            self._last_beat_ms = beat_ms
            self._flash_beat()

        self._refresh_workers()
        self.root.after(POLL_MS, self._poll)


def run(
    detector: BeatDetector,
    broadcaster: BeatBroadcaster,
    control: ControlChannel,
    config: AppConfig,
    audio_ok: bool,
) -> None:
    """Launch the control panel and block until the window is closed."""
    root = tk.Tk()
    app = ControlApp(root, detector, broadcaster, control, config, audio_ok)
    app.run()
