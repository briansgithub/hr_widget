package com.hrbot.android

import kotlin.math.PI
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

class MonotonicPulseClockTest {
    @Test
    fun visualCrestAndBeatScheduleAlignAtSupportedRates() {
        listOf(60, 120, 200, 250).forEach { bpm ->
            val clock = MonotonicPulseClock()
            clock.synchronize(bpm, 0.0, 0L)
            val periodNanos = (60.0 / bpm * MonotonicPulseClock.NANOS_PER_SECOND).toLong()
            val firstCrest = checkNotNull(clock.nextCrestAfter(0L))

            assertWithinOneNanosecond(periodNanos / 4L, firstCrest)
            // Integer-nanosecond scheduling can round the ideal crest up by 1 ns.
            assertEquals(PI / 2.0, clock.phaseAt(firstCrest), 3e-8)
            assertWithinOneNanosecond(
                firstCrest + periodNanos,
                checkNotNull(clock.nextCrestAfter(firstCrest))
            )
        }
    }

    @Test
    fun enablingMidCycleWaitsForTheNextCrest() {
        val clock = MonotonicPulseClock()
        clock.synchronize(60, 0.0, 0L)

        assertWithinOneNanosecond(
            250_000_000L,
            checkNotNull(clock.nextCrestAfter(100_000_000L))
        )
    }

    @Test
    fun enableOrLoadAtACrestStillWaitsForTheNextCycle() {
        val clock = MonotonicPulseClock()
        val policy = HeartbeatSchedulePolicy()
        clock.synchronize(60, PI / 2.0, 1_000_000_000L)

        assertWithinOneNanosecond(
            2_000_000_000L,
            checkNotNull(
                policy.deadline(
                    clock,
                    1_000_000_000L,
                    allowRecentCrest = false
                )
            )
        )
    }

    @Test
    fun timingCorrectionCanRecoverARecentCrestWithinTheGate() {
        val clock = MonotonicPulseClock()
        val policy = HeartbeatSchedulePolicy()
        val gate = HeartbeatBeatGate()
        val nowNanos = 1_000_000_000L
        val fiftyMillisecondsOfPhase = 2.0 * PI * 0.050
        clock.synchronize(
            60,
            PI / 2.0 + fiftyMillisecondsOfPhase,
            nowNanos
        )

        val deadline = checkNotNull(
            policy.deadline(clock, nowNanos, allowRecentCrest = true)
        )
        assertWithinOneNanosecond(950_000_000L, deadline)
        assertTrue(gate.shouldPlay(deadline, nowNanos, checkNotNull(clock.periodNanos())))
    }

    @Test
    fun timingCorrectionSkipsACrestOutsideTheLatenessGate() {
        val clock = MonotonicPulseClock()
        val policy = HeartbeatSchedulePolicy()
        val nowNanos = 1_000_000_000L
        val oneHundredOneMillisecondsOfPhase = 2.0 * PI * 0.101
        clock.synchronize(
            60,
            PI / 2.0 + oneHundredOneMillisecondsOfPhase,
            nowNanos
        )

        val deadline = checkNotNull(
            policy.deadline(clock, nowNanos, allowRecentCrest = true)
        )
        assertTrue(deadline > nowNanos)
        assertWithinOneNanosecond(
            checkNotNull(clock.nextCrestAfter(nowNanos)),
            deadline
        )
    }

    @Test
    fun bpmChangesPreserveCurrentPhase() {
        val clock = MonotonicPulseClock()
        clock.synchronize(60, 0.0, 0L)
        clock.synchronize(120, null, 250_000_000L)

        assertEquals(PI / 2.0, clock.phaseAt(250_000_000L), 1e-9)
        assertEquals(PI, clock.phaseAt(375_000_000L), 1e-9)
    }

    @Test
    fun remoteCorrectionReanchorsVisualAndAudioPhase() {
        val clock = MonotonicPulseClock()
        clock.synchronize(120, 0.0, 0L)
        clock.synchronize(120, PI, 1_000_000_000L)

        assertEquals(PI, clock.phaseAt(1_000_000_000L), 1e-9)
        assertWithinOneNanosecond(
            1_375_000_000L,
            checkNotNull(clock.nextCrestAfter(1_000_000_000L))
        )
    }

    @Test
    fun beatGateSuppressesHalfCycleDuplicates() {
        listOf(60, 120, 200, 250).forEach { bpm ->
            val gate = HeartbeatBeatGate()
            val periodNanos = (60.0 / bpm * MonotonicPulseClock.NANOS_PER_SECOND).toLong()
            val firstCrest = periodNanos / 4L

            assertTrue(gate.shouldPlay(firstCrest, firstCrest + 10_000_000L, periodNanos))
            assertFalse(
                gate.shouldPlay(
                    firstCrest + periodNanos / 4L,
                    firstCrest + periodNanos / 4L + 10_000_000L,
                    periodNanos
                )
            )
            assertTrue(
                gate.shouldPlay(
                    firstCrest + periodNanos / 2L,
                    firstCrest + periodNanos / 2L + 10_000_000L,
                    periodNanos
                )
            )
        }
    }

    @Test
    fun beatGateSkipsLateCallbacksWithoutCatchUp() {
        val gate = HeartbeatBeatGate()
        val periodNanos = 1_000_000_000L

        assertFalse(gate.shouldPlay(250_000_000L, 350_000_001L, periodNanos))
        assertTrue(gate.shouldPlay(1_250_000_000L, 1_260_000_000L, periodNanos))
    }

    @Test
    fun zeroBpmStopsTheClock() {
        val clock = MonotonicPulseClock()
        clock.synchronize(120, 0.0, 0L)
        clock.synchronize(0, null, 100_000_000L)

        assertEquals(0.0, clock.phaseAt(10_000_000_000L), 0.0)
        assertNull(clock.periodNanos())
        assertNull(clock.nextCrestAfter(10_000_000_000L))
    }

    private fun assertWithinOneNanosecond(expected: Long, actual: Long) {
        assertTrue("Expected $expected ns, got $actual ns", kotlin.math.abs(expected - actual) <= 1L)
    }
}
