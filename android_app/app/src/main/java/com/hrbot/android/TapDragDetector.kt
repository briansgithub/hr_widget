package com.hrbot.android

import kotlin.math.hypot

/**
 * Tells a tap from a drag for the floating widget's touch stream. Pure Kotlin so it can be unit-tested.
 * A gesture is a tap if the finger stays within [slopPx] of where it went down and lifts within
 * [maxTapDurationMs]; once it leaves the slop radius it is a drag for the rest of the gesture.
 */
class TapDragDetector(
    private val slopPx: Float,
    private val maxTapDurationMs: Long
) {
    private var downX = 0f
    private var downY = 0f
    private var downTimeMs = 0L
    private var active = false

    var isDragging = false
        private set

    fun onDown(x: Float, y: Float, timeMs: Long) {
        downX = x
        downY = y
        downTimeMs = timeMs
        active = true
        isDragging = false
    }

    /** Returns true while the gesture counts as a drag (the caller should move the widget). */
    fun onMove(x: Float, y: Float): Boolean {
        if (!active) return false
        if (!isDragging && hypot(x - downX, y - downY) > slopPx) isDragging = true
        return isDragging
    }

    /** Returns true if the finished gesture was a tap. */
    fun onUp(timeMs: Long): Boolean {
        val tap = active && !isDragging && (timeMs - downTimeMs) <= maxTapDurationMs
        reset()
        return tap
    }

    fun onCancel() = reset()

    private fun reset() {
        active = false
        isDragging = false
    }
}
