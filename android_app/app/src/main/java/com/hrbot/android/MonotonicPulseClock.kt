package com.hrbot.android

import kotlin.math.PI
import kotlin.math.ceil

/**
 * A phase clock anchored to a monotonic timestamp.
 *
 * The clock is sampled by both the visual renderer and heartbeat scheduler, so
 * pausing visual callbacks never pauses (or later fast-forwards) the pulse.
 */
internal class MonotonicPulseClock {
    companion object {
        const val NANOS_PER_SECOND = 1_000_000_000L
        const val CREST_PHASE_RADIANS = PI / 2.0
        private const val TWO_PI = PI * 2.0
        private const val PHASE_EPSILON = 1e-9
    }

    private var bpm = 0
    private var anchorPhaseRadians = 0.0
    private var anchorTimeNanos = 0L

    fun phaseAt(nowNanos: Long): Double {
        if (bpm <= 0) return 0.0
        val elapsedNanos = (nowNanos - anchorTimeNanos).coerceAtLeast(0L)
        val rotations = elapsedNanos.toDouble() / NANOS_PER_SECOND * bpm / 60.0
        return normalize(anchorPhaseRadians + rotations * TWO_PI)
    }

    /**
     * Changes rate without changing the phase at [nowNanos]. A supplied remote
     * phase correction becomes the new anchor before the new rate takes effect.
     */
    fun synchronize(newBpm: Int, correctedPhaseRadians: Double?, nowNanos: Long) {
        require(newBpm >= 0) { "BPM cannot be negative" }
        val phase = correctedPhaseRadians?.let(::normalize) ?: phaseAt(nowNanos)
        bpm = newBpm
        anchorTimeNanos = nowNanos
        anchorPhaseRadians = if (newBpm > 0) phase else 0.0
    }

    fun periodNanos(): Long? {
        if (bpm <= 0) return null
        return (60.0 / bpm * NANOS_PER_SECOND).toLong().coerceAtLeast(1L)
    }

    /** Returns the first visual crest strictly after [nowNanos]. */
    fun nextCrestAfter(nowNanos: Long): Long? {
        if (bpm <= 0) return null
        val currentPhase = phaseAt(nowNanos)
        var phaseRemaining = normalize(CREST_PHASE_RADIANS - currentPhase)
        if (phaseRemaining < PHASE_EPSILON) phaseRemaining = TWO_PI

        val angularVelocity = TWO_PI * bpm / 60.0
        val delayNanos = ceil(phaseRemaining / angularVelocity * NANOS_PER_SECOND)
            .toLong()
            .coerceAtLeast(1L)
        return if (Long.MAX_VALUE - nowNanos < delayNanos) {
            Long.MAX_VALUE
        } else {
            nowNanos + delayNanos
        }
    }

    /** Returns the visual crest at or immediately before [nowNanos]. */
    fun crestAtOrBefore(nowNanos: Long): Long? {
        if (bpm <= 0) return null
        var phaseSinceCrest = normalize(phaseAt(nowNanos) - CREST_PHASE_RADIANS)
        if (phaseSinceCrest < PHASE_EPSILON) phaseSinceCrest = 0.0

        val angularVelocity = TWO_PI * bpm / 60.0
        val ageNanos = ceil(phaseSinceCrest / angularVelocity * NANOS_PER_SECOND)
            .toLong()
            .coerceAtLeast(0L)
        return if (nowNanos < Long.MIN_VALUE + ageNanos) {
            Long.MIN_VALUE
        } else {
            nowNanos - ageNanos
        }
    }

    private fun normalize(phaseRadians: Double): Double {
        var normalized = phaseRadians % TWO_PI
        if (normalized < 0.0) normalized += TWO_PI
        return normalized
    }
}

internal const val HEARTBEAT_MAX_LATENESS_NANOS = 100_000_000L

/** Selects a future crest for enable/load, or a still-timely recent crest after correction. */
internal class HeartbeatSchedulePolicy(
    private val maximumLatenessNanos: Long = HEARTBEAT_MAX_LATENESS_NANOS
) {
    fun deadline(
        clock: MonotonicPulseClock,
        nowNanos: Long,
        allowRecentCrest: Boolean
    ): Long? {
        if (allowRecentCrest) {
            val recentCrest = clock.crestAtOrBefore(nowNanos)
            if (
                recentCrest != null &&
                recentCrest <= nowNanos &&
                nowNanos - recentCrest <= maximumLatenessNanos
            ) {
                return recentCrest
            }
        }
        return clock.nextCrestAfter(nowNanos)
    }
}

/**
 * Suppresses duplicate crest callbacks and callbacks delayed far enough that
 * playing them would create an audible catch-up beat.
 */
internal class HeartbeatBeatGate(
    private val maximumLatenessNanos: Long = HEARTBEAT_MAX_LATENESS_NANOS
) {
    private var lastPlayedCrestNanos: Long? = null

    fun shouldPlay(
        scheduledCrestNanos: Long,
        observedAtNanos: Long,
        periodNanos: Long
    ): Boolean {
        if (periodNanos <= 0L || observedAtNanos < scheduledCrestNanos) return false
        if (observedAtNanos - scheduledCrestNanos > maximumLatenessNanos) return false

        val lastCrest = lastPlayedCrestNanos
        if (lastCrest != null && scheduledCrestNanos - lastCrest < periodNanos / 2L) {
            return false
        }

        lastPlayedCrestNanos = scheduledCrestNanos
        return true
    }
}
