package com.hrbot.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class TapDragDetectorTest {
    private fun detector() = TapDragDetector(slopPx = 20f, maxTapDurationMs = 500L)

    @Test
    fun stationaryQuickReleaseIsTap() {
        val d = detector()
        d.onDown(100f, 100f, 0L)
        assertTrue(d.onUp(120L))
    }

    @Test
    fun jitterInsideSlopIsStillTapAndNotDrag() {
        val d = detector()
        d.onDown(100f, 100f, 0L)
        assertFalse(d.onMove(108f, 112f))
        assertTrue(d.onUp(200L))
    }

    @Test
    fun movementBeyondSlopIsDragNotTap() {
        val d = detector()
        d.onDown(100f, 100f, 0L)
        assertTrue(d.onMove(140f, 100f))
        assertFalse(d.onUp(100L))
    }

    @Test
    fun returningToStartAfterDragStaysDrag() {
        val d = detector()
        d.onDown(100f, 100f, 0L)
        d.onMove(200f, 100f)
        assertTrue(d.onMove(100f, 100f))
        assertFalse(d.onUp(100L))
    }

    @Test
    fun longPressWithoutMovementIsNotTap() {
        val d = detector()
        d.onDown(0f, 0f, 0L)
        assertFalse(d.onUp(501L))
    }

    @Test
    fun releaseAtExactTimeLimitIsTap() {
        val d = detector()
        d.onDown(0f, 0f, 1000L)
        assertTrue(d.onUp(1500L))
    }

    @Test
    fun cancelledGestureIsNotTapAndDetectorIsReusable() {
        val d = detector()
        d.onDown(0f, 0f, 0L)
        d.onCancel()
        assertFalse(d.onUp(10L))
        d.onDown(0f, 0f, 100L)
        assertTrue(d.onUp(150L))
    }

    @Test
    fun upWithoutDownIsNotTap() {
        assertFalse(detector().onUp(0L))
    }

    @Test
    fun dragStateResetsBetweenGestures() {
        val d = detector()
        d.onDown(0f, 0f, 0L)
        d.onMove(100f, 0f)
        d.onUp(50L)
        d.onDown(0f, 0f, 100L)
        assertFalse(d.isDragging)
        assertEquals(true, d.onUp(120L))
    }
}
