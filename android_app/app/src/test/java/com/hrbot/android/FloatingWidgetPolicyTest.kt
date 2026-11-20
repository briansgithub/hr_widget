package com.hrbot.android

import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class FloatingWidgetPolicyTest {
    @Test
    fun widgetIsOnByDefault() {
        assertTrue(FloatingWidgetPolicy.DEFAULT_ENABLED)
    }

    @Test
    fun showsWhenPermissionArrivesAndWidgetIsWantedButHidden() {
        assertTrue(FloatingWidgetPolicy.shouldShowAfterPermissionChange(true, false, true))
    }

    @Test
    fun doesNotShowWhenUserSavedOff() {
        assertFalse(FloatingWidgetPolicy.shouldShowAfterPermissionChange(false, false, true))
    }

    @Test
    fun doesNotShowWithoutPermissionOrWhenAlreadyShown() {
        assertFalse(FloatingWidgetPolicy.shouldShowAfterPermissionChange(true, false, false))
        assertFalse(FloatingWidgetPolicy.shouldShowAfterPermissionChange(true, true, true))
    }

    @Test
    fun promptsOnceWhenWantedAndPermissionMissing() {
        assertTrue(FloatingWidgetPolicy.shouldPromptForPermission(true, false, false))
        assertFalse(FloatingWidgetPolicy.shouldPromptForPermission(true, false, true))
    }

    @Test
    fun neverPromptsWhenOffOrAlreadyPermitted() {
        assertFalse(FloatingWidgetPolicy.shouldPromptForPermission(false, false, false))
        assertFalse(FloatingWidgetPolicy.shouldPromptForPermission(true, true, false))
    }
}
