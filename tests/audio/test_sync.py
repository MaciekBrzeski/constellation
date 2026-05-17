"""Tests for the worker-side BeatScheduler (authoritative + predictive sync)."""

from __future__ import annotations

from constellation.audio.sync import BeatScheduler


def _packet(epoch=1, brain_ms=0.0, beat_count=-1, next_brain=0.0, interval=0.0):
    return {
        "epoch": epoch,
        "brain_ms": brain_ms,
        "beat_count": beat_count,
        "next_beat_brain_ms": next_brain,
        "interval_ms": interval,
    }


def test_clock_offset_is_the_rolling_minimum() -> None:
    """offset = min(brain_ms - recv_local_ms) — jitter-robust."""
    sch = BeatScheduler(offset_window_size=16)
    for i in range(20):
        brain = 10000.0 + i * 20.0
        # Constant 5000 ms offset, plus 0/3 ms of one-way jitter.
        recv = brain - 5000.0 + (3.0 if i % 2 else 0.0)
        sch.feed(_packet(brain_ms=brain), recv)
    assert abs(sch.clock_offset_ms - 4997.0) < 1e-6


def test_packet_loss_self_heals() -> None:
    """A skipped beat_count is corrected by the next packet's absolute count."""
    sch = BeatScheduler()
    for bc in (0, 1, 3):  # beat 2's packet is lost
        sch.feed(_packet(brain_ms=1000.0 + bc, beat_count=bc, interval=500.0), bc)
    assert sch.count == 3  # jumped straight to 3, not stuck at 1


def test_count_never_rewinds() -> None:
    """A stale (lower) beat_count must not pull the index backwards."""
    sch = BeatScheduler()
    sch.feed(_packet(brain_ms=10.0, beat_count=5, interval=500.0), 0.0)
    sch.feed(_packet(brain_ms=11.0, beat_count=2, interval=500.0), 1.0)
    assert sch.count == 5


def test_predictive_preroll_advances_on_schedule() -> None:
    """The index flips at the predicted local time, not on packet arrival."""
    sch = BeatScheduler(render_lead_ms=0.0, offset_window_size=8)
    # offset = brain_ms - recv = 1000 - 0 = 1000.
    sch.feed(
        _packet(brain_ms=1000.0, beat_count=5, next_brain=1500.0, interval=500.0),
        0.0,
    )
    # next beat local time = 1500 - offset(1000) - lead(0) = 500.
    assert sch.poll(400.0) == 5   # before the scheduled flip
    assert sch.poll(500.0) == 6   # at the scheduled flip
    assert sch.poll(1000.0) == 7  # one interval later — predicted, no packet


def test_two_workers_converge_on_the_same_index() -> None:
    """The same packet stream yields the same count on workers with
    different network delay."""
    a = BeatScheduler(render_lead_ms=0.0)
    b = BeatScheduler(render_lead_ms=0.0)
    for bc in range(10):
        brain = 1000.0 + bc * 500.0
        pkt = _packet(
            brain_ms=brain, beat_count=bc, next_brain=brain + 500.0, interval=500.0
        )
        a.feed(pkt, brain - 100.0)  # worker A: 100 ms delay
        b.feed(pkt, brain - 130.0)  # worker B: 130 ms delay
    assert a.count == b.count == 9


def test_epoch_change_resets_clock_window_keeps_counter() -> None:
    """A new epoch drops the stale clock window but not the beat counter."""
    sch = BeatScheduler(offset_window_size=8)
    for i in range(8):
        sch.feed(_packet(epoch=1, brain_ms=1000.0 + i, beat_count=i), float(i))
    assert sch.count == 7
    sch.feed(_packet(epoch=2, brain_ms=99999.0, beat_count=7), 0.0)
    assert sch.epoch == 2
    assert sch.count == 7  # monotonic counter survives the epoch bump
    assert sch.clock_offset_ms == 99999.0  # window holds only the new packet


def test_poll_holds_at_minus_one_before_any_beat() -> None:
    sch = BeatScheduler()
    sch.feed(_packet(brain_ms=10.0, beat_count=-1), 0.0)
    assert sch.poll(1e9) == -1
