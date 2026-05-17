"""Beat detection via microphone input.

Selectable onset backend (``backend`` kwarg):

* ``"multiband"`` — numpy spectral-flux detector (band-split, kick-locked
  tempo), see :mod:`constellation.audio.onset`. Genre-robust, no system deps.
* ``"aubio"`` — ``aubio.tempo`` phase-locked autocorrelation tracker, used
  when the library is installed.
* ``"rms"`` — wideband RMS-spike fallback, always available.
* ``"auto"`` — aubio if ``use_aubio`` and the lib imports, else multiband,
  else rms.

Energy-tier classification is always RMS-based regardless of backend, since
neither the aubio nor the multiband path exposes comparable loudness metrics.
"""

from __future__ import annotations

import collections
import threading
import time
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from constellation.audio.onset import MultibandSpectralFluxDetector


class BeatDetector:
    """Detect musical beats from microphone audio.

    Onset (preferred): ``aubio.tempo`` reports beats per block — phase-locked
    so subsequent beats land on-beat instead of after the kick. Falls back
    to RMS-spike detection when aubio isn't installed.

    RMS fallback: beats fire when block RMS exceeds
    ``rolling_mean * sensitivity`` past the audio-time cooldown.

    Energy tiering: classifies overall loudness against an **adaptive noise
    floor** (rolling 10th-percentile of recent RMS). ``low`` = near silence,
    ``medium`` = quiet music, ``high`` = loud music. Drives action selection
    in :class:`constellation.animation.behavior.AudioReactiveBehavior`.

    BPM: aubio's autocorrelation estimate when active, otherwise interval-
    based on **audio time** (block count × sample rate) so it stays accurate
    offline and under thread starvation.
    """

    def __init__(
        self,
        sensitivity: float = 1.5,
        cooldown_ms: float = 200.0,
        history_size: int = 43,
        sample_rate: int = 22050,
        block_size: int = 1024,
        noise_gate: float = 0.01,
        energy_window_size: int = 200,
        low_intensity_ratio: float = 2.0,
        high_intensity_ratio: float = 6.0,
        tempo_history_size: int = 8,
        min_noise_floor: float = 0.002,
        use_aubio: bool = True,
        device: int | str | None = None,
        backend: str = "auto",
        fft_size: int = 1024,
        flux_history_size: int = 43,
        bass_sensitivity: float = 1.6,
        mid_sensitivity: float = 1.8,
        high_sensitivity: float = 2.0,
        bass_max_hz: float = 250.0,
        high_min_hz: float = 4000.0,
        tempo_min_bpm: float = 50.0,
        tempo_max_bpm: float = 200.0,
    ) -> None:
        self.sensitivity = sensitivity
        self.cooldown_ms = cooldown_ms
        self.history_size = history_size
        self.sample_rate = sample_rate
        self.block_size = block_size
        self.noise_gate = noise_gate
        # Input device for the capture stream. ``None`` = system default;
        # may be a PortAudio device index or a (substring) name.
        self.device = device
        self._low_intensity_ratio = low_intensity_ratio
        self._high_intensity_ratio = high_intensity_ratio
        self._min_noise_floor = min_noise_floor
        self._tempo_history_size = tempo_history_size

        # Onset-backend selection + multiband tuning (used in start()).
        self._backend_choice = backend
        self._fft_size = fft_size
        self._flux_history_size = flux_history_size
        self._bass_sensitivity = bass_sensitivity
        self._mid_sensitivity = mid_sensitivity
        self._high_sensitivity = high_sensitivity
        self._bass_max_hz = bass_max_hz
        self._high_min_hz = high_min_hz
        self._tempo_min_bpm = tempo_min_bpm
        self._tempo_max_bpm = tempo_max_bpm
        self._onset_backend: MultibandSpectralFluxDetector | None = None
        self._backend_name = "rms"

        self._beat_event = threading.Event()
        self._energy_history: collections.deque[float] = collections.deque(
            maxlen=history_size,
        )
        self._energy_window: collections.deque[float] = collections.deque(
            maxlen=energy_window_size,
        )
        self._smoothed_energy: float = 0.0
        # Short EMA for tier classification — reacts in ~1 s so the deer
        # returns to idle quickly when music stops, while the 9 s mean keeps
        # the noise-floor estimate stable.
        self._tier_energy: float = 0.0
        self._tier_alpha: float = 0.05
        self._noise_floor: float = min_noise_floor
        self._energy_tier: str = "low"

        # Audio-clock bookkeeping
        self._block_duration_ms: float = block_size / sample_rate * 1000.0
        self._audio_time_ms: float = 0.0
        self._beat_audio_times_ms: collections.deque[float] = collections.deque(
            maxlen=tempo_history_size + 1,
        )
        self._last_beat_audio_ms: float = -1e12  # far in the past
        # Strength of the most recent beat = rms / rolling_mean at fire time.
        # Weak beats (hi-hats, filler) land near sensitivity; strong kicks sit
        # well above — used by AudioReactiveBehavior to pick run/jump on hits.
        self._last_beat_strength: float = 0.0

        # Wall-clock bookkeeping for silence detection (behavior.py compares
        # last_beat_time against time.monotonic()).
        self._last_beat_wall_s: float = 0.0

        self._bpm: float = 0.0
        self._stream = None

        # Authoritative beat counter + epoch for multi-projector sync.
        # _beat_count: monotonic index of the last fired beat (-1 = none yet).
        # _epoch: bumped on start() and on a tempo re-lock so worker Pis know
        # to discard a stale predicted-beat schedule.
        self._beat_count: int = -1
        self._epoch: int = 0

        # aubio tempo tracker — populated lazily in start() so unit tests and
        # offline replay can construct the detector without the lib.
        self._use_aubio: bool = use_aubio
        self._aubio_tempo = None

    # ------------------------------------------------------------- properties
    @property
    def beat_event(self) -> threading.Event:
        """Thread-safe event flag — set on each detected beat."""
        return self._beat_event

    @property
    def last_beat_time(self) -> float:
        """Wall-clock (``time.monotonic()``) seconds of the last detected beat.

        Compared against live ``time.monotonic()`` in the silence-detection
        path of ``AudioReactiveBehavior`` — keep this in wall-clock terms.
        """
        return self._last_beat_wall_s

    @property
    def energy_tier(self) -> str:
        """Current energy tier: ``'low'``, ``'medium'``, or ``'high'``."""
        return self._energy_tier

    @property
    def smoothed_energy(self) -> float:
        """Long-window mean RMS energy."""
        return self._smoothed_energy

    @property
    def noise_floor(self) -> float:
        """Adaptive noise floor (rolling p10 of recent RMS, clamped)."""
        return self._noise_floor

    @property
    def tier_energy(self) -> float:
        """Fast EMA of RMS used for tier classification."""
        return self._tier_energy

    @property
    def tier_intensity(self) -> float:
        """Current tier intensity: ``tier_energy / noise_floor``."""
        if self._noise_floor <= 0:
            return 0.0
        return self._tier_energy / self._noise_floor

    @property
    def bpm(self) -> float:
        """Estimated beats per minute (0.0 before enough beats are recorded)."""
        return self._bpm

    @property
    def last_beat_strength(self) -> float:
        """``rms / rolling_mean`` at the moment the last beat fired."""
        return self._last_beat_strength

    @property
    def last_beat_audio_ms(self) -> float:
        """Audio-time (milliseconds) of the last detected beat."""
        return self._last_beat_audio_ms

    @property
    def last_beat_monotonic_ms(self) -> float:
        """Wall-clock (``time.monotonic()``) milliseconds of the last beat.

        The common clock the broadcast packet uses to schedule frame flips
        on the worker Pis — keep it in the same domain as ``brain_ms``.
        """
        return self._last_beat_wall_s * 1000.0

    @property
    def beat_count(self) -> int:
        """Monotonic index of the last fired beat (-1 before the first)."""
        return self._beat_count

    @property
    def epoch(self) -> int:
        """Sync epoch — bumped on start() and on a tempo re-lock."""
        return self._epoch

    @property
    def predicted_next_beat_ms(self) -> float:
        """Predicted ``time.monotonic()`` ms of the next beat (0.0 if unknown)."""
        if self._bpm <= 0 or self._beat_count < 0:
            return 0.0
        return self._last_beat_wall_s * 1000.0 + 60000.0 / self._bpm

    @property
    def backend_name(self) -> str:
        """Resolved onset backend in use: ``'aubio'``, ``'multiband'`` or ``'rms'``."""
        return self._backend_name

    @property
    def low_intensity_ratio(self) -> float:
        """Intensity below this classifies the energy tier as ``'low'``."""
        return self._low_intensity_ratio

    @low_intensity_ratio.setter
    def low_intensity_ratio(self, value: float) -> None:
        self._low_intensity_ratio = value

    @property
    def high_intensity_ratio(self) -> float:
        """Intensity at or above this classifies the energy tier as ``'high'``."""
        return self._high_intensity_ratio

    @high_intensity_ratio.setter
    def high_intensity_ratio(self, value: float) -> None:
        self._high_intensity_ratio = value

    # ------------------------------------------------------------- processing
    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        """Called by sounddevice for each audio block."""
        block = indata[:, 0].astype(np.float32)
        rms = float(np.sqrt(np.mean(block ** 2)))

        # Run the active onset backend. aubio.tempo and the multiband detector
        # both expect exactly hop_size / block_size samples; sounddevice's
        # blocksize is set to match in start(). When the backend reports a
        # beat the RMS gate is bypassed — the backend has its own silence
        # handling and is more reliable on sustained music.
        ext_beat = False
        ext_bpm = 0.0
        if self._aubio_tempo is not None:
            try:
                is_beat = self._aubio_tempo(block)
                ext_beat = bool(is_beat[0])
                if ext_beat:
                    ext_bpm = float(self._aubio_tempo.get_bpm())
            except Exception:
                # aubio internal error → drop to RMS path for this block.
                ext_beat = False
                ext_bpm = 0.0
        elif self._onset_backend is not None:
            try:
                result = self._onset_backend.process_block(block)
                ext_beat = result.beat
                if ext_beat:
                    ext_bpm = result.bpm
            except Exception:
                ext_beat = False
                ext_bpm = 0.0

        self._process_energy(rms, ext_beat=ext_beat, ext_bpm=ext_bpm)

    def _process_energy(
        self,
        rms: float,
        ext_beat: bool = False,
        ext_bpm: float = 0.0,
    ) -> None:
        """Core onset + tier logic — testable without hardware.

        ``ext_beat=True`` forces a beat fire (bypassing the RMS gate); used
        when an onset backend (aubio or multiband) detected a beat.
        ``ext_bpm`` is the backend's tempo estimate at fire time and overrides
        the interval-based estimate when supplied.
        """
        # Advance audio time by one block.
        self._audio_time_ms += self._block_duration_ms

        self._energy_history.append(rms)
        self._energy_window.append(rms)
        if self._energy_window:
            self._smoothed_energy = sum(self._energy_window) / len(self._energy_window)
        # Fast EMA used by the tier classifier; warmup seeds with the first rms.
        if self._tier_energy == 0.0:
            self._tier_energy = rms
        else:
            self._tier_energy = (
                self._tier_alpha * rms + (1.0 - self._tier_alpha) * self._tier_energy
            )

        # Adaptive noise floor: p10 of the long window once it has samples.
        # Falls quickly when the room goes quiet, doesn't chase loud bursts.
        if len(self._energy_window) >= 10:
            self._noise_floor = max(
                self._min_noise_floor,
                float(np.percentile(list(self._energy_window), 10)),
            )

        if len(self._energy_history) < 2:
            return

        # Tier classification uses absolute intensity vs the noise floor —
        # updated even for quiet blocks below the hard noise_gate so silence
        # reliably reports "low". Uses the fast EMA so tier responds to
        # silence in ~1 s instead of lagging behind the 9 s mean.
        intensity = self._tier_energy / self._noise_floor if self._noise_floor > 0 else 0.0
        if intensity < self._low_intensity_ratio:
            self._energy_tier = "low"
        elif intensity >= self._high_intensity_ratio:
            self._energy_tier = "high"
        else:
            self._energy_tier = "medium"

        cooldown_elapsed = (
            self._audio_time_ms - self._last_beat_audio_ms
        ) >= self.cooldown_ms

        if ext_beat and cooldown_elapsed:
            self._fire_beat(rms, ext_bpm=ext_bpm)
            return

        # RMS fallback path — only runs when aubio either didn't fire or is
        # unavailable. Same gates as before: above the hard noise floor, then
        # above rolling_mean × sensitivity.
        if rms < self.noise_gate:
            return

        rolling_mean = sum(self._energy_history) / len(self._energy_history)
        threshold = rolling_mean * self.sensitivity

        if rms > threshold and cooldown_elapsed:
            self._fire_beat(rms)

    def _fire_beat(self, rms: float, ext_bpm: float = 0.0) -> None:
        """Record a beat at the current audio time and emit the event."""
        self._last_beat_audio_ms = self._audio_time_ms
        self._last_beat_wall_s = time.monotonic()
        # Strength measured against the adaptive noise floor rather than
        # the short rolling mean — during sustained music the rolling mean
        # tracks the beats themselves, collapsing every hit to ~1.5×.
        # noise_floor (p10 of the 9 s window) stays near the song's quiet
        # passages, giving a useful spread: hats ~2×, snares ~4×, kicks ~7×.
        self._last_beat_strength = (
            rms / self._noise_floor if self._noise_floor > 0 else 0.0
        )
        self._beat_event.set()
        self._beat_count += 1

        self._beat_audio_times_ms.append(self._audio_time_ms)
        # Prefer the backend's autocorrelation BPM when supplied (smoother and
        # phase-locked). Fall back to interval-based estimate.
        prev_bpm = self._bpm
        if ext_bpm > 0:
            self._bpm = ext_bpm
        elif len(self._beat_audio_times_ms) >= 2:
            intervals = [
                self._beat_audio_times_ms[i + 1] - self._beat_audio_times_ms[i]
                for i in range(len(self._beat_audio_times_ms) - 1)
            ]
            mean_interval_ms = sum(intervals) / len(intervals)
            if mean_interval_ms > 0:
                self._bpm = 60000.0 / mean_interval_ms

        # A large tempo change is a re-lock — bump the epoch so worker Pis
        # drop any predicted-beat schedule built on the old tempo.
        if (
            prev_bpm > 0
            and self._bpm > 0
            and abs(self._bpm - prev_bpm) / prev_bpm > 0.25
        ):
            self._epoch += 1

    # ------------------------------------------------------------- lifecycle
    def _build_backend(self) -> None:
        """Resolve ``backend`` and construct the onset backend.

        ``"auto"`` → aubio (if ``use_aubio``) else rms. When aubio is selected
        but not installed, ``"auto"`` falls through to multiband while an
        explicit ``"aubio"`` choice falls back to rms.
        """
        self._aubio_tempo = None
        self._onset_backend = None

        choice = self._backend_choice
        if choice == "auto":
            choice = "aubio" if self._use_aubio else "rms"

        if choice == "aubio":
            try:
                import aubio  # type: ignore[import-not-found]

                # win_s = 2 × hop_s is the standard aubio.tempo config; gives
                # the tracker enough buffer to estimate phase.
                self._aubio_tempo = aubio.tempo(
                    "default",
                    self.block_size * 2,
                    self.block_size,
                    self.sample_rate,
                )
                self._backend_name = "aubio"
                return
            except ImportError:
                choice = "multiband" if self._backend_choice == "auto" else "rms"

        if choice == "multiband":
            from constellation.audio.onset import MultibandSpectralFluxDetector

            self._onset_backend = MultibandSpectralFluxDetector(
                sample_rate=self.sample_rate,
                block_size=self.block_size,
                fft_size=self._fft_size,
                flux_history_size=self._flux_history_size,
                bass_sensitivity=self._bass_sensitivity,
                mid_sensitivity=self._mid_sensitivity,
                high_sensitivity=self._high_sensitivity,
                bass_max_hz=self._bass_max_hz,
                high_min_hz=self._high_min_hz,
                tempo_min_bpm=self._tempo_min_bpm,
                tempo_max_bpm=self._tempo_max_bpm,
                tempo_history_size=self._tempo_history_size,
            )
            self._backend_name = "multiband"
            return

        self._backend_name = "rms"

    def start(self) -> None:
        """Open the microphone stream and begin listening for beats.

        Constructs the onset backend (see :meth:`_build_backend`) and bumps
        the sync epoch so worker Pis re-establish their beat schedule.
        """
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError(
                "sounddevice is required for audio beat detection. "
                "Install it with: pip install 'constellation[audio]'"
            ) from exc
        except OSError as exc:
            # sounddevice imports but PortAudio's shared library is missing.
            raise RuntimeError(
                f"PortAudio runtime not found ({exc}). Install it with: "
                "sudo apt install libportaudio2"
            ) from exc

        self._build_backend()
        self._epoch += 1

        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                blocksize=self.block_size,
                channels=1,
                dtype="float32",
                callback=self._audio_callback,
                device=self.device,
            )
            self._stream.start()
        except sd.PortAudioError as exc:
            raise RuntimeError(
                f"Could not open microphone: {exc}. "
                "Check that a recording device is available."
            ) from exc

    def stop(self) -> None:
        """Stop and close the audio stream."""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self._aubio_tempo = None
        self._onset_backend = None

    @property
    def aubio_active(self) -> bool:
        """True when the aubio tempo tracker is in use for this run."""
        return self._aubio_tempo is not None


def list_input_devices() -> list[tuple[int, str]]:
    """Enumerate audio devices with input channels.

    Returns ``(index, name)`` pairs for every device that can capture
    audio. Never raises: if ``sounddevice`` isn't installed or PortAudio
    isn't available (``libportaudio2`` missing), returns an empty list so
    callers can show a friendly "no microphone" message instead of crashing.
    """
    try:
        import sounddevice as sd
    except (ImportError, OSError):
        return []
    try:
        devices = sd.query_devices()
    except Exception:
        return []
    result: list[tuple[int, str]] = []
    for idx, dev in enumerate(devices):
        if dev.get("max_input_channels", 0) > 0:
            result.append((idx, str(dev.get("name", f"device {idx}"))))
    return result
