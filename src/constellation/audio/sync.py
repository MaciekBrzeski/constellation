"""Worker-side beat synchronisation.

A worker Pi must show the *same animation frame* as its sibling at the same
instant. :class:`BeatScheduler` makes that happen from the brain's broadcast
packets:

* **Authoritative counter** — every packet carries ``beat_count``, a monotonic
  index of the last beat. The worker *sets* its frame from that counter rather
  than incrementing locally, so a dropped UDP packet self-heals: the next
  packet carries the correct count and the worker jumps straight to it.

* **Clock offset** — each packet carries ``brain_ms`` (the brain's
  ``time.monotonic()`` at send). ``offset = brain_ms - local_recv_ms`` maps
  brain time to local time. A rolling *minimum* over recent packets is robust
  to network jitter — the smallest offset is the lowest-latency sample.

* **Predictive pre-roll** — ``next_beat_brain_ms`` predicts the next beat's
  brain time. Converted to local time (minus ``render_lead_ms`` to cover the
  render/projector lag) the worker flips the frame *exactly on the beat*
  instead of reacting when the packet lands.

The scheduler is fed from the receiver's network thread and polled from the
render loop's main thread, so all shared state is guarded by a lock.
"""

from __future__ import annotations

import collections
import threading


class BeatScheduler:
    """Turns brain broadcast packets into an authoritative local frame index."""

    def __init__(
        self,
        render_lead_ms: float = 16.0,
        offset_window_size: int = 64,
    ) -> None:
        self._render_lead_ms = render_lead_ms
        self._lock = threading.Lock()

        # Rolling window of brain→local clock offsets; _offset is its minimum.
        self._offsets: collections.deque[float] = collections.deque(
            maxlen=offset_window_size
        )
        self._offset = 0.0

        # _count: authoritative index of the beat currently displayed (-1 =
        # none yet). _next_local_ms: predicted local time of beat _count + 1
        # (0.0 = no prediction armed). _interval_ms: current beat period.
        self._count = -1
        self._next_local_ms = 0.0
        self._interval_ms = 0.0
        self._epoch = -1

    # ------------------------------------------------------------- properties
    @property
    def count(self) -> int:
        """Authoritative beat index currently scheduled (-1 before any beat)."""
        return self._count

    @property
    def epoch(self) -> int:
        """Sync epoch last seen from the brain."""
        return self._epoch

    @property
    def clock_offset_ms(self) -> float:
        """Estimated brain→local clock offset (``brain_ms - local_ms``)."""
        return self._offset

    # ------------------------------------------------------------- feed / poll
    def feed(self, packet: dict, recv_local_ms: float) -> None:
        """Ingest one broadcast packet, captured at ``recv_local_ms`` local time.

        Called from the receiver's network thread.
        """
        with self._lock:
            epoch = packet.get("epoch")
            if isinstance(epoch, int) and epoch != self._epoch:
                # Brain restarted or re-locked tempo — drop the stale schedule
                # and clock window, keep the monotonic beat counter.
                self._epoch = epoch
                self._offsets.clear()
                self._next_local_ms = 0.0

            brain_ms = packet.get("brain_ms")
            if isinstance(brain_ms, (int, float)):
                self._offsets.append(float(brain_ms) - recv_local_ms)
                self._offset = min(self._offsets)

            interval = packet.get("interval_ms")
            if isinstance(interval, (int, float)) and interval > 0:
                self._interval_ms = float(interval)

            beat_count = packet.get("beat_count")
            # Snap forward only — never rewind past a beat the predictor has
            # already advanced to (poll() may run ahead of confirmed packets).
            if (
                isinstance(beat_count, int)
                and beat_count >= 0
                and beat_count >= self._count
            ):
                self._count = beat_count
                next_brain = packet.get("next_beat_brain_ms")
                if (
                    isinstance(next_brain, (int, float))
                    and next_brain > 0
                    and self._offsets
                ):
                    self._next_local_ms = (
                        float(next_brain) - self._offset - self._render_lead_ms
                    )

    def poll(self, now_local_ms: float) -> int:
        """Return the beat index that should be displayed at ``now_local_ms``.

        Advances the index predictively when the scheduled local time for the
        next beat has arrived — so frames flip on the beat even between
        packets. Called every render-loop iteration from the main thread.
        """
        with self._lock:
            steps = 0
            while (
                self._next_local_ms > 0.0
                and now_local_ms >= self._next_local_ms
                and self._interval_ms > 0.0
                and steps < 8  # bound catch-up after a stall
            ):
                self._count += 1
                self._next_local_ms += self._interval_ms
                steps += 1
            return self._count
