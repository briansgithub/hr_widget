package com.hrbot.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Test

class HeartbeatSoundCatalogTest {
    @Test
    fun catalogHasExpectedOrderedPresets() {
        assertEquals(
            listOf(
                Triple("heartbeat_lub_dub", "Natural Lub-Dub", "heartbeat_lub_dub"),
                Triple("heartbeat_deep_chest", "Deep Chest", "heartbeat_deep_chest"),
                Triple("heartbeat_soft_resting", "Soft Resting", "heartbeat_soft_resting"),
                Triple(
                    "heartbeat_clear_stethoscope",
                    "Clear Stethoscope",
                    "heartbeat_clear_stethoscope"
                ),
                Triple("heartbeat_strong_pulse", "Strong Pulse", "heartbeat_strong_pulse")
            ),
            HeartbeatSoundCatalog.effects.map { Triple(it.id, it.label, it.rawResourceName) }
        )
    }

    @Test
    fun clearStethoscopeIsDefault() {
        assertEquals("heartbeat_clear_stethoscope", HeartbeatSoundCatalog.DEFAULT_ID)
        assertSame(
            HeartbeatSoundCatalog.effects[3],
            HeartbeatSoundCatalog.findById(HeartbeatSoundCatalog.DEFAULT_ID)
        )
    }

    @Test
    fun selectionValidationRejectsUnknownId() {
        assertNull(HeartbeatSoundCatalog.findById("not_a_heartbeat_effect"))
    }
}
