"""Beat detection via microphone input using energy-based onset detection."""

from __future__ import annotations

import collections
import threading
import time

import numpy as np


class BeatDetector:
    """Detect musical beats from microphone audio using RMS energy spikes.

    Onset: beats fire when the current block's RMS exceeds
    ``rolling_mean * sensitivity`` and the audio-time cooldown has elapsed.

    Energy tiering: classifies overall loudness against an **adaptive noise
    floor** (rolling 10th-percentile of recent RMS). ``low`` = near silence,
    ``medium`` = quiet music, ``high`` = loud music. Drives action selection
    in :class:`constellation.animation.behavior.AudioReactiveBehavior`.

    BPM: estimated from inter-beat intervals measured in **audio time**
    (derived from the block count and sample rate), not wall clock — so the
    detector is reproducible offline and robust to thread starvation live.
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
    ) -> None:
        self.sensitivity = sensitivity
        self.cooldown_ms = cooldown_ms
        self.history_size = history_size
        self.sample_rate = sample_rate
        self.block_size = block_size
        self.noise_gate = noise_gate
        self._low_intensity_ratio = low_intensity_ratio
        self._high_intensity_ratio = high_intensity_ratio
        self._min_noise_floor = min_noise_floor

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

    # ------------------------------------------------------------- processing
    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        """Called by sounddevice for each audio block."""
        rms = float(np.sqrt(np.mean(indata[:, 0] ** 2)))
        self._process_energy(rms)

    def _process_energy(self, rms: float) -> None:
        """Core onset + tier logic — testable without hardware."""
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

        # Onset check — require RMS above the hard noise gate as a sanity
        # filter, then exceed the rolling mean × sensitivity.
        if rms < self.noise_gate:
            return

        rolling_mean = sum(self._energy_history) / len(self._energy_history)
        threshold = rolling_mean * self.sensitivity

        cooldown_elapsed = (
            self._audio_time_ms - self._last_beat_audio_ms
        ) >= self.cooldown_ms

        if rms > threshold and cooldown_elapsed:
            self._last_beat_audio_ms = self._audio_time_ms
            self._last_beat_wall_s = time.monotonic()
            # Strength measured against the adaptive noise floor rather than
            # the short rolling mean — during sustained music the rolling
            # mean tracks the beats themselves, collapsing every hit to ~1.5×.
            # noise_floor (p10 of the 9 s window) stays near the song's quiet
            # passages, giving a useful spread: hats ~2×, snares ~4×, kicks ~7×.
            self._last_beat_strength = (
                rms / self._noise_floor if self._noise_floor > 0 else 0.0
            )
            self._beat_event.set()

            self._beat_audio_times_ms.append(self._audio_time_ms)
            if len(self._beat_audio_times_ms) >= 2:
                intervals = [
                    self._beat_audio_times_ms[i + 1] - self._beat_audio_times_ms[i]
                    for i in range(len(self._beat_audio_times_ms) - 1)
                ]
                mean_interval_ms = sum(intervals) / len(intervals)
                if mean_interval_ms > 0:
                    self._bpm = 60000.0 / mean_interval_ms

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        """Open the microphone stream and begin listening for beats."""
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise RuntimeError(
                "sounddevice is required for audio beat detection. "
                "Install it with: pip install 'constellation[audio]'"
            ) from exc

        try:
            self._stream = sd.InputStream(
                samplerate=self.sample_rate,
                blocksize=self.block_size,
                channels=1,
                dtype="float32",
                callback=self._audio_callback,
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
