"""Monotonic pulse phase and one-shot beat scheduling helpers.

The classes in this module contain no UI or audio side effects.  Callers pass
monotonic timestamps explicitly, which keeps the visual and audio paths on the
same timeline and makes sleep/stall behaviour deterministic in tests.
"""

import math


TAU = math.tau
CREST_PHASE = math.pi / 2.0


class PulseClock:
    """A BPM oscillator anchored to a monotonic timestamp."""

    def __init__(self, bpm=0.0, phase=0.0, now=0.0):
        # Keeping the anchor in one immutable tuple gives background readers a
        # coherent snapshot while Tk replaces it on BPM/phase updates.
        self._state = (
            max(0.0, float(bpm)),
            float(phase),
            float(now),
            0,
        )

    @property
    def bpm(self):
        return self._state[0]

    @property
    def revision(self):
        return self._state[3]

    @property
    def period_seconds(self):
        bpm = self.bpm
        if bpm <= 0.0:
            return None
        return 60.0 / bpm

    def phase_at(self, now):
        bpm, anchor_phase, anchor_time, _revision = self._state
        elapsed = max(0.0, float(now) - anchor_time)
        if bpm <= 0.0:
            return anchor_phase
        return anchor_phase + elapsed * bpm / 60.0 * TAU

    def update(self, bpm, now, phase=None):
        """Set BPM and optionally correct phase without accumulating wall time.

        A BPM-only update samples the old oscillator first, preserving phase
        across tempo changes.  A supplied phase is an authoritative remote
        correction at ``now``.
        """

        now = float(now)
        current_phase = self.phase_at(now) if phase is None else float(phase)
        revision = self.revision + 1
        self._state = (max(0.0, float(bpm)), current_phase, now, revision)
        return current_phase

    def next_crest_after(self, now):
        """Return the first visual crest strictly after ``now``."""

        period = self.period_seconds
        if period is None:
            return None

        now = float(now)
        phase = self.phase_at(now)
        completed_crests = math.floor((phase - CREST_PHASE) / TAU)
        next_crest_phase = CREST_PHASE + (completed_crests + 1) * TAU
        phase_delta = next_crest_phase - phase

        # Floating-point roundoff at an exact crest must still select the next
        # cycle so enabling sound never fires immediately mid-toggle.
        if phase_delta <= 1e-12:
            phase_delta += TAU
        return now + phase_delta / TAU * period


class BeatGate:
    """Track crest deadlines and decide whether one audio onset is allowed."""

    def __init__(self, max_lateness_seconds=0.100):
        self.max_lateness_seconds = float(max_lateness_seconds)
        self.due_at = None
        self.last_started_at = None
        self._clock_revision = None

    @property
    def armed(self):
        return self.due_at is not None

    def disarm(self):
        self.due_at = None
        self._clock_revision = None

    def arm(self, clock, now):
        self._clock_revision = clock.revision
        self.due_at = self._guard_candidate(clock, clock.next_crest_after(now))
        return self.due_at

    def reschedule(self, clock, now):
        """Recompute the next crest after a BPM or phase correction."""

        now = float(now)
        next_crest = clock.next_crest_after(now)
        period = clock.period_seconds
        candidate = next_crest
        if self.armed and next_crest is not None and period is not None:
            recent_crest = next_crest - period
            if 0.0 <= now - recent_crest <= self.max_lateness_seconds + 1e-12:
                candidate = recent_crest
        self._clock_revision = clock.revision
        self.due_at = self._guard_candidate(clock, candidate)
        return self.due_at

    def seconds_until_due(self, now):
        if self.due_at is None:
            return None
        return max(0.0, self.due_at - float(now))

    def poll(self, clock, now):
        """Return True once for an on-time crest; skip late/missed crests."""

        now = float(now)
        if clock.period_seconds is None:
            self.disarm()
            return False
        if not self.armed:
            return False
        if self._clock_revision != clock.revision:
            self.reschedule(clock, now)
        if self.due_at is None or now + 1e-12 < self.due_at:
            return False

        scheduled_crest = self.due_at
        lateness = now - scheduled_crest
        duplicate = self._inside_duplicate_guard(clock, now)
        should_start = lateness <= self.max_lateness_seconds + 1e-12 and not duplicate

        if should_start:
            self.last_started_at = now

        # Always move to a future crest.  A delayed callback therefore emits
        # at most one sound and never replays all beats missed during sleep.
        self._clock_revision = clock.revision
        self.due_at = self._guard_candidate(clock, clock.next_crest_after(now))
        return should_start

    def _inside_duplicate_guard(self, clock, candidate):
        period = clock.period_seconds
        return (
            period is not None
            and self.last_started_at is not None
            and candidate - self.last_started_at < period / 2.0 - 1e-12
        )

    def _guard_candidate(self, clock, candidate):
        period = clock.period_seconds
        if candidate is None or period is None or self.last_started_at is None:
            return candidate
        while candidate - self.last_started_at < period / 2.0 - 1e-12:
            candidate += period
        return candidate
