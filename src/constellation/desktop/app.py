"""Native Tkinter control panel for the constellation brain.

A tabbed desktop window: monitor beat detection, pick the microphone, tune the
detector, run calibration, preview the render, and remote-control worker Pis.

The audio brain (``BeatDetector`` + ``BeatBroadcaster``) runs in a **child
process** — PortAudio's threads crash intermittently when sharing a process
with Tk/X11 on the L4T/Tegra box. The panel drives that child through a
:class:`~constellation.control.brain.BrainClient` over localhost UDP; the
(socket-only) worker ``ControlChannel`` stays in-process.

Threading: Tk's ``mainloop`` and every widget access stay on the main thread.
Live state is bridged by a 10 Hz ``root.after`` poll reading the BrainClient's
telemetry snapshot; calibration results arrive on ``brain.cal_queue``.
"""

from __future__ import annotations

import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from constellation import netmode
from constellation.config import AppConfig
from constellation.control.brain import BrainClient
from constellation.control.master import ControlChannel

POLL_MS = 100
_TIER_COLORS = {"low": "#7aa9ff", "medium": "#ffd760", "high": "#ff6b6b"}
_DEFAULT_DEVICE_LABEL = "(system default)"
# worker_id reported by the local pygame preview process.
_PREVIEW_ID = "preview"

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
        brain: BrainClient,
        control: ControlChannel,
        config: AppConfig,
    ) -> None:
        self.root = root
        self.brain = brain
        self.control = control
        self.config = config
        self._audio_ok = brain.audio_ok
        self._running = True

        self._device_name: str | None = config.audio.device
        # Device dropdown label → device name (None = system default).
        self._device_map: dict[str, str | None] = {}
        self._last_beat_ms = brain.last_beat_audio_ms
        self._cal_suggestions: dict[str, float] = {}
        self._worker_ids: tuple[str, ...] = ()
        self._worker_rows: dict[str, dict] = {}
        # Local pygame preview worker — a real ``constellation-run`` process
        # fed by this brain's own UDP beat broadcast (worker_id "preview").
        self._preview_proc: subprocess.Popen | None = None
        self._net_queue: queue.Queue = queue.Queue()
        self._poll_count = 0
        # Decaying per-band peak for auto-ranging the flux meters.
        self._flux_peak = {"bass": 1e-6, "mid": 1e-6, "high": 1e-6}

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
        self._build_preview_tab()
        self._has_network_tab = netmode.scripts_available()
        if self._has_network_tab:
            self._build_network_tab()

        self._refresh_status_banner()

    # ------------------------------------------------------------- run/shutdown
    def run(self) -> None:
        self.root.after(POLL_MS, self._poll)
        self.root.mainloop()

    def _on_close(self) -> None:
        self._running = False
        self._kill_preview()
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
            ("Onset backend", "backend"),
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

        # Multiband spectral-flux meters — auto-ranged, flat for aubio/rms.
        ttk.Label(frame, text="Spectral flux").grid(
            row=7, column=0, sticky="nw", pady=(10, 3)
        )
        flux_frame = ttk.Frame(frame)
        flux_frame.grid(row=7, column=1, sticky="w", padx=12, pady=(10, 3))
        self._flux_bars: dict[str, ttk.Progressbar] = {}
        for i, band in enumerate(("bass", "mid", "high")):
            ttk.Label(flux_frame, text=band, width=5).grid(
                row=i, column=0, sticky="w"
            )
            bar = ttk.Progressbar(flux_frame, length=200, maximum=100.0)
            bar.grid(row=i, column=1, sticky="w", pady=2)
            self._flux_bars[band] = bar

    # ------------------------------------------------------------ tab: microphone
    def _build_microphone_tab(self) -> None:
        frame = ttk.Frame(self._notebook, padding=12)
        self._notebook.add(frame, text="Microphone")

        ttk.Label(frame, text="Input device").grid(row=0, column=0, sticky="nw")
        self._device_combo = ttk.Combobox(frame, state="readonly", width=58)
        self._device_combo.grid(row=0, column=1, sticky="w", padx=8)
        self._device_combo.bind("<<ComboboxSelected>>", self._on_device_selected)

        ttk.Button(frame, text="Rescan", command=self._rescan_devices).grid(
            row=1, column=1, sticky="w", padx=8, pady=8
        )

        # Live "what mic am I actually capturing from?" — invaluable when
        # the dropdown is on (system default) or when a USB mic is hot-plugged.
        ttk.Label(frame, text="Capturing from").grid(row=2, column=0, sticky="w")
        self._mic_active = ttk.Label(
            frame, text="—", wraplength=420,
            font=("TkDefaultFont", 10, "bold"),
        )
        self._mic_active.grid(row=2, column=1, sticky="w", padx=8, pady=4)

        self._mic_status = ttk.Label(frame, text="", wraplength=480)
        self._mic_status.grid(row=3, column=0, columnspan=2, sticky="w", pady=8)
        self._populate_devices()

    @staticmethod
    def _device_sort_key(name: str) -> tuple[int, str]:
        """Float real USB mics / system routers above generic tegra subdevices.

        The Switch's Tegra audio card exposes 10 indistinguishable input-capable
        subdevices that swamp the dropdown — push them to the bottom so the
        likely-correct entries surface first.
        """
        lower = name.lower()
        for keyword in ("usb", "torch", "streaming", "mic", "headset", "webcam"):
            if keyword in lower:
                return (0, lower)
        if lower in {"pipewire", "default", "pulse", "jack"}:
            return (1, lower)
        return (2, lower)

    def _populate_devices(self) -> None:
        """Fill the device dropdown from the brain child's reported list."""
        devices = sorted(self.brain.devices, key=self._device_sort_key)
        self._device_map = {_DEFAULT_DEVICE_LABEL: None}
        labels = [_DEFAULT_DEVICE_LABEL]
        selected = _DEFAULT_DEVICE_LABEL
        for name in devices:
            self._device_map[name] = name
            labels.append(name)
            if name == self._device_name:
                selected = name
        self._device_combo.configure(values=labels)
        self._device_combo.set(selected)
        if devices:
            self._mic_status.configure(text=f"{len(devices)} input device(s) found.")
        else:
            self._mic_status.configure(
                text="No input devices. Install libportaudio2 "
                "(offline/debs/libportaudio2_arm64.deb) and rescan."
            )

    def _rescan_devices(self) -> None:
        """Ask the child to re-enumerate, then repopulate once it replies."""
        self._mic_status.configure(text="rescanning…")
        self.brain.rescan()
        self.root.after(600, self._populate_devices)

    def _on_device_selected(self, _event=None) -> None:
        label = self._device_combo.get()
        if label not in self._device_map:
            return
        name = self._device_map[label]
        self._device_name = name
        # set_device persists the choice and respawns the brain so the device
        # opens via the stable startup path; telemetry updates the banner.
        self.brain.set_device(name)
        self._mic_status.configure(
            text=f"switching to: {label}… (restarting audio, ~2 s)"
        )

    # ------------------------------------------------------------- tab: tuning
    def _build_tuning_tab(self) -> None:
        frame = ttk.Frame(self._notebook, padding=12)
        self._notebook.add(frame, text="Tuning")

        self._sliders: dict[str, ttk.Scale] = {}
        self._slider_vars: dict[str, tk.StringVar] = {}
        for i, (label, attr, lo, hi) in enumerate(_SLIDERS):
            ttk.Label(frame, text=label).grid(row=i, column=0, sticky="w", pady=6)
            value = self.brain.param(attr)
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
        self.brain.set_param(attr, value)
        self._slider_vars[attr].set(self._fmt_slider(attr, value))

    # --------------------------------------------------------- tab: calibration
    def _build_calibration_tab(self) -> None:
        frame = ttk.Frame(self._notebook, padding=12)
        self._notebook.add(frame, text="Calibration")

        # Step 1 — room tone. Record with NO music so the gate is set above
        # genuine background noise, not the quiet passages of a song.
        ttk.Label(frame, text="1. Background (silence)").grid(
            row=0, column=0, sticky="w"
        )
        self._baseline_duration = tk.StringVar(value="5")
        ttk.Spinbox(
            frame, from_=3, to=60, width=6, textvariable=self._baseline_duration
        ).grid(row=0, column=1, sticky="w", padx=8)
        self._baseline_button = ttk.Button(
            frame, text="Measure background", command=self._start_baseline
        )
        self._baseline_button.grid(row=0, column=2, sticky="w", padx=8)

        # Step 2 — music. Tunes sensitivity; leaves the gate to step 1.
        ttk.Label(frame, text="2. Music").grid(row=1, column=0, sticky="w")
        self._cal_duration = tk.StringVar(value="15")
        ttk.Spinbox(
            frame, from_=3, to=120, width=6, textvariable=self._cal_duration
        ).grid(row=1, column=1, sticky="w", padx=8)
        self._cal_button = ttk.Button(
            frame, text="Record + analyse", command=self._start_calibration
        )
        self._cal_button.grid(row=1, column=2, sticky="w", padx=8)

        self._cal_apply = ttk.Button(
            frame, text="Apply suggested", command=self._apply_suggested,
            state="disabled",
        )
        self._cal_apply.grid(row=2, column=2, sticky="w", padx=8, pady=(6, 0))

        self._cal_text = tk.Text(frame, width=64, height=22, state="disabled")
        self._cal_text.grid(row=3, column=0, columnspan=3, sticky="nsew", pady=10)
        self._set_cal_text(
            "Step 1: silence the room, click Measure background, Apply.\n"
            "Step 2: play music, click Record + analyse, Apply."
        )

    def _set_cal_text(self, text: str) -> None:
        self._cal_text.configure(state="normal")
        self._cal_text.delete("1.0", "end")
        self._cal_text.insert("1.0", text)
        self._cal_text.configure(state="disabled")

    def _start_baseline(self) -> None:
        try:
            duration = float(self._baseline_duration.get())
        except ValueError:
            messagebox.showerror("Calibration", "Duration must be a number.")
            return
        self._cal_busy(True)
        self._set_cal_text(f"recording {duration:.0f}s of room tone — stay quiet…")
        # The brain child records + analyses; the result arrives on cal_queue.
        self.brain.calibrate("baseline", duration)

    def _start_calibration(self) -> None:
        try:
            duration = float(self._cal_duration.get())
        except ValueError:
            messagebox.showerror("Calibration", "Duration must be a number.")
            return
        self._cal_busy(True)
        self._set_cal_text(f"recording {duration:.0f}s — make some noise…")
        self.brain.calibrate("music", duration)

    def _cal_busy(self, busy: bool) -> None:
        """Toggle the calibration controls while a recording pass runs."""
        state = "disabled" if busy else "normal"
        self._cal_button.configure(state=state)
        self._baseline_button.configure(state=state)
        self._device_combo.configure(state="disabled" if busy else "readonly")
        if busy:
            self._cal_apply.configure(state="disabled")

    def _drain_cal_queue(self) -> None:
        """Pump calibration results sent back by the brain child."""
        try:
            while True:
                _kind, ok, suggestions, text = self.brain.cal_queue.get_nowait()
                if ok:
                    self._cal_suggestions = suggestions
                    self._set_cal_text(text)
                    self._cal_apply.configure(
                        state="normal" if suggestions else "disabled"
                    )
                else:
                    self._set_cal_text(f"calibration failed:\n{text}")
                self._cal_busy(False)
        except queue.Empty:
            pass

    def _apply_suggested(self) -> None:
        for key, value in self._cal_suggestions.items():
            self.brain.set_param(key, value)
            if key in self._sliders:
                self._sliders[key].set(value)  # fires _on_slider → label sync
        self.brain.save_overrides()
        self._cal_apply.configure(state="disabled")

    # ------------------------------------------------------------- tab: workers
    def _build_workers_tab(self) -> None:
        frame = ttk.Frame(self._notebook, padding=12)
        self._notebook.add(frame, text="Workers")
        self._workers_summary = tk.StringVar(value="Waiting for worker heartbeats…")
        ttk.Label(
            frame,
            textvariable=self._workers_summary,
            font=("TkDefaultFont", 10, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 8))
        # Worker cards are rebuilt inside this sub-frame so the summary above
        # survives a rebuild.
        self._workers_frame = ttk.Frame(frame)
        self._workers_frame.grid(row=1, column=0, sticky="nsew")

    def _refresh_workers(self) -> None:
        workers = sorted(self.control.workers(), key=lambda w: w.worker_id)
        ids = tuple(w.worker_id for w in workers)
        if ids != self._worker_ids:
            self._rebuild_worker_rows(workers)
            self._worker_ids = ids
        import time as _t
        now = _t.monotonic()
        online_count = 0
        for w in workers:
            is_online = w.is_online(now)
            if is_online:
                online_count += 1
            row = self._worker_rows.get(w.worker_id)
            if row is None:
                continue
            ago = now - w.last_seen
            row["status"].configure(
                text="● online" if is_online else "○ offline",
                foreground="#5fd17a" if is_online else "#888",
            )
            row["meta"].configure(
                text=f"tier {w.tier}  ·  {w.bpm:.0f} BPM  ·  seen {ago:.0f}s ago"
            )
        if workers:
            self._workers_summary.set(
                f"{online_count}/{len(workers)} worker(s) online"
            )
        else:
            self._workers_summary.set("Waiting for worker heartbeats…")

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

    # ------------------------------------------------------------- tab: preview
    def _build_preview_tab(self) -> None:
        frame = ttk.Frame(self._notebook, padding=12)
        self._notebook.add(frame, text="Preview")

        ttk.Label(
            frame,
            text="Launch a local pygame window that renders the constellation "
            "exactly as a worker Pi would — driven by this brain's own beat "
            "broadcast. Lets you test the animation with no Pi hardware.",
            wraplength=480,
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 12))

        ttk.Label(frame, text="Animal").grid(row=1, column=0, sticky="w")
        self._preview_animal = tk.StringVar(value="deer_male")
        ttk.Radiobutton(
            frame, text="Male deer", value="deer_male",
            variable=self._preview_animal, command=self._on_preview_animal,
        ).grid(row=1, column=1, sticky="w", padx=8)
        ttk.Radiobutton(
            frame, text="Female deer", value="deer_female",
            variable=self._preview_animal, command=self._on_preview_animal,
        ).grid(row=1, column=2, sticky="w", padx=8)

        self._preview_launch = ttk.Button(
            frame, text="Launch preview", command=self._start_preview
        )
        self._preview_launch.grid(row=2, column=1, sticky="w", padx=8, pady=10)
        self._preview_stop = ttk.Button(
            frame, text="Stop preview", command=self._stop_preview, state="disabled"
        )
        self._preview_stop.grid(row=2, column=2, sticky="w", padx=8, pady=10)

        self._preview_status = ttk.Label(
            frame, text="preview not running.", wraplength=480
        )
        self._preview_status.grid(row=3, column=0, columnspan=3, sticky="w", pady=4)

    def _preview_running(self) -> bool:
        return self._preview_proc is not None and self._preview_proc.poll() is None

    def _set_preview_ui(self, *, running: bool) -> None:
        self._preview_launch.configure(state="disabled" if running else "normal")
        self._preview_stop.configure(state="normal" if running else "disabled")

    def _start_preview(self) -> None:
        if self._preview_running():
            return
        animal = self._preview_animal.get()
        port = self.config.network.port
        cmd = [
            sys.executable, "-m", "constellation",
            "--mode", "windowed",
            "--remote-beat", f"0.0.0.0:{port}",
            "--animal", animal,
            "--worker-id", _PREVIEW_ID,
            "--master-host", "127.0.0.1",
        ]
        try:
            self._preview_proc = subprocess.Popen(cmd)
        except OSError as exc:
            messagebox.showerror("Preview", f"Could not launch preview:\n{exc}")
            return
        self._set_preview_ui(running=True)
        self._preview_status.configure(
            text=f"preview running — {animal} (pid {self._preview_proc.pid})"
        )

    def _stop_preview(self) -> None:
        self._kill_preview()
        self._set_preview_ui(running=False)
        self._preview_status.configure(text="preview stopped.")

    def _kill_preview(self) -> None:
        """Terminate the preview process if alive (no UI access — close-safe)."""
        proc = self._preview_proc
        self._preview_proc = None
        if proc is None or proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()

    def _on_preview_animal(self) -> None:
        """Radio toggled — switch the live preview's animal via the control plane."""
        animal = self._preview_animal.get()
        if not self._preview_running():
            return
        if self.control.send_command(_PREVIEW_ID, "set_animal", animal):
            self._preview_status.configure(text=f"preview running — {animal}")
        else:
            self._preview_status.configure(
                text=f"{animal} selected — applies once the preview registers…"
            )

    def _refresh_preview(self) -> None:
        """Detect the preview window being closed by the user."""
        proc = self._preview_proc
        if proc is not None and proc.poll() is not None:
            self._preview_proc = None
            self._set_preview_ui(running=False)
            self._preview_status.configure(text="preview window closed.")

    # ------------------------------------------------------------- tab: network
    _NET_COLORS = {"ap": "#5fd17a", "wifi": "#7aa9ff", "unknown": "#888"}

    def _build_network_tab(self) -> None:
        frame = ttk.Frame(self._notebook, padding=12)
        self._notebook.add(frame, text="Network")

        ttk.Label(frame, text="Wi-Fi mode").grid(row=0, column=0, sticky="w")
        self._net_mode_var = tk.StringVar(value="checking…")
        self._net_mode_label = ttk.Label(
            frame,
            textvariable=self._net_mode_var,
            font=("TkDefaultFont", 11, "bold"),
        )
        self._net_mode_label.grid(row=0, column=1, sticky="w", padx=12)

        self._net_ap_btn = ttk.Button(
            frame, text="Switch to AP", command=lambda: self._start_net_switch("ap")
        )
        self._net_ap_btn.grid(row=1, column=0, sticky="w", pady=10)
        self._net_wifi_btn = ttk.Button(
            frame,
            text="Switch to Wi-Fi",
            command=lambda: self._start_net_switch("wifi"),
        )
        self._net_wifi_btn.grid(row=1, column=1, sticky="w", pady=10, padx=8)

        ttk.Label(
            frame,
            text="Switching to AP turns this Switch into the worker Pis' "
            "access point — it cuts the Switch's own internet until you "
            "switch back to Wi-Fi.",
            foreground="#ffd760",
            wraplength=480,
        ).grid(row=2, column=0, columnspan=3, sticky="w")

        self._net_text = tk.Text(frame, width=64, height=14, state="disabled")
        self._net_text.grid(row=3, column=0, columnspan=3, sticky="nsew", pady=10)
        self._set_net_text("Querying current network mode…")
        self._refresh_net_mode()

    def _set_net_text(self, text: str) -> None:
        self._net_text.configure(state="normal")
        self._net_text.delete("1.0", "end")
        self._net_text.insert("1.0", text)
        self._net_text.configure(state="disabled")

    def _apply_net_mode(self, mode: str) -> None:
        self._net_mode_var.set(mode.upper())
        self._net_mode_label.configure(foreground=self._NET_COLORS.get(mode, "#888"))

    def _start_net_switch(self, target: str) -> None:
        self._net_ap_btn.configure(state="disabled")
        self._net_wifi_btn.configure(state="disabled")
        label = "AP" if target == "ap" else "Wi-Fi"
        self._set_net_text(f"switching to {label} — this can take a few seconds…")
        threading.Thread(
            target=self._net_switch_worker, args=(target,), daemon=True
        ).start()

    def _net_switch_worker(self, target: str) -> None:
        """Runs off the main thread; result goes back via the queue."""
        if target == "ap":
            ok, output = netmode.switch_to_ap()
        else:
            ok, output = netmode.switch_to_wifi()
        mode = netmode.current_mode()
        self._net_queue.put(("switch", ok, output, mode))

    def _refresh_net_mode(self) -> None:
        """Query the current mode off the main thread (subprocess call)."""
        threading.Thread(target=self._net_mode_worker, daemon=True).start()

    def _net_mode_worker(self) -> None:
        self._net_queue.put(("mode", True, "", netmode.current_mode()))

    def _drain_net_queue(self) -> None:
        try:
            while True:
                kind, ok, output, mode = self._net_queue.get_nowait()
                self._apply_net_mode(mode)
                if kind == "switch":
                    self._net_ap_btn.configure(state="normal")
                    self._net_wifi_btn.configure(state="normal")
                    status = "OK" if ok else "FAILED"
                    self._set_net_text(f"[{status}]\n{output}")
        except queue.Empty:
            pass

    # ----------------------------------------------------------------- helpers
    def _save_audio(self) -> None:
        """Persist the live audio settings — the brain child owns the file."""
        self.brain.save_overrides()

    def _refresh_status_banner(self) -> None:
        if not self.brain.child_alive():
            self._banner.configure(
                text="Brain process exited — restart the control panel.",
                foreground="#ff6b6b",
            )
        elif self._audio_ok:
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

        d = self.brain
        self._audio_ok = d.audio_ok
        self._refresh_status_banner()
        active = d.device_active or ("—" if not self._audio_ok else "(system default)")
        if active != self._mic_active.cget("text"):
            self._mic_active.configure(text=active)
        tier = d.energy_tier
        self._stat_vars["tier"].set(tier)
        self._tier_label.configure(foreground=_TIER_COLORS.get(tier, "#e6e8ee"))
        self._stat_vars["bpm"].set(f"{d.bpm:.1f}")
        self._stat_vars["floor"].set(f"{d.noise_floor:.4f}")
        self._stat_vars["intensity"].set(f"{d.tier_intensity:.2f}")
        self._stat_vars["backend"].set(d.backend_name)

        flux = d.band_flux
        for band, bar in self._flux_bars.items():
            val = flux.get(band, 0.0)
            # Decay the peak so the meter re-ranges when levels drop.
            peak = max(self._flux_peak[band] * 0.995, val, 1e-6)
            self._flux_peak[band] = peak
            bar["value"] = min(100.0, val / peak * 100.0)

        beat_ms = d.last_beat_audio_ms
        if beat_ms > self._last_beat_ms:
            self._last_beat_ms = beat_ms
            self._flash_beat()

        self._refresh_workers()
        self._refresh_preview()

        if self._has_network_tab:
            self._drain_net_queue()
            self._poll_count += 1
            if self._poll_count % 50 == 0:  # ~5 s — catch external mode changes
                self._refresh_net_mode()

        self.root.after(POLL_MS, self._poll)


def run(
    brain: BrainClient,
    control: ControlChannel,
    config: AppConfig,
) -> None:
    """Launch the control panel and block until the window is closed."""
    root = tk.Tk()
    app = ControlApp(root, brain, control, config)
    app.run()
