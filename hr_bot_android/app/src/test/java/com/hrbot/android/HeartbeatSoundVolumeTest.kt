package com.hrbot.android

import org.junit.Assert.assertEquals
import org.junit.Test

class HeartbeatSoundVolumeTest {
    @Test
    fun defaultVolumeIsFullScale() {
        assertEquals(100, HeartbeatSoundVolume.DEFAULT_PERCENT)
        assertEquals(1.0f, HeartbeatSoundVolume.scalar(HeartbeatSoundVolume.DEFAULT_PERCENT), 0.0f)
    }

    @Test
    fun percentIsClampedBeforeConversionToScalar() {
        assertEquals(0, HeartbeatSoundVolume.clampPercent(-20))
        assertEquals(0.0f, HeartbeatSoundVolume.scalar(-20), 0.0f)
        assertEquals(0.37f, HeartbeatSoundVolume.scalar(37), 0.0001f)
        assertEquals(100, HeartbeatSoundVolume.clampPercent(140))
        assertEquals(1.0f, HeartbeatSoundVolume.scalar(140), 0.0f)
    }
}
