package com.hrbot.android

/** Pure rules for the floating heart-rate widget, kept free of Android types so they unit-test. */
object FloatingWidgetPolicy {
    const val PREFS_NAME = "hr_bot_prefs"
    const val PREF_ENABLED = "floating_enabled"
    const val PREF_PERMISSION_PROMPTED = "floating_permission_prompted"

    /** New installs get the widget; a saved choice (on or off) always wins over this. */
    const val DEFAULT_ENABLED = true

    /**
     * Whether to (re)show the widget after the overlay permission may have been granted
     * (e.g. when the user returns from the system settings screen).
     */
    fun shouldShowAfterPermissionChange(
        savedEnabled: Boolean,
        currentlyShown: Boolean,
        canDrawOverlays: Boolean
    ): Boolean = savedEnabled && !currentlyShown && canDrawOverlays

    /** Whether to ask the user once for the overlay permission the default-on widget needs. */
    fun shouldPromptForPermission(
        savedEnabled: Boolean,
        canDrawOverlays: Boolean,
        alreadyPrompted: Boolean
    ): Boolean = savedEnabled && !canDrawOverlays && !alreadyPrompted
}
