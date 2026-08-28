package com.hrbot.android

internal data class HeartbeatSoundEffect(
    val id: String,
    val label: String,
    val rawResourceName: String
)

internal object HeartbeatSoundCatalog {
    const val DEFAULT_ID = "heartbeat_clear_stethoscope"

    val effects: List<HeartbeatSoundEffect> = listOf(
        HeartbeatSoundEffect("heartbeat_lub_dub", "Natural Lub-Dub", "heartbeat_lub_dub"),
        HeartbeatSoundEffect("heartbeat_deep_chest", "Deep Chest", "heartbeat_deep_chest"),
        HeartbeatSoundEffect("heartbeat_soft_resting", "Soft Resting", "heartbeat_soft_resting"),
        HeartbeatSoundEffect(
            "heartbeat_clear_stethoscope",
            "Clear Stethoscope",
            "heartbeat_clear_stethoscope"
        ),
        HeartbeatSoundEffect("heartbeat_strong_pulse", "Strong Pulse", "heartbeat_strong_pulse")
    )

    fun findById(id: String): HeartbeatSoundEffect? = effects.firstOrNull { it.id == id }
}
