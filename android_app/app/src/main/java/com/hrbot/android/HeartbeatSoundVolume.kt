package com.hrbot.android

internal object HeartbeatSoundVolume {
    const val DEFAULT_PERCENT = 100

    fun clampPercent(percent: Int): Int = percent.coerceIn(0, 100)

    fun scalar(percent: Int): Float = clampPercent(percent) / 100.0f
}
