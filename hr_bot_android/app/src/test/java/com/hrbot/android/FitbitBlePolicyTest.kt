package com.hrbot.android

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class FitbitBlePolicyTest {
    @Test
    fun unbondedDeviceStartsBonding() {
        assertEquals(
            FitbitBlePolicy.BondAction.START_BONDING,
            FitbitBlePolicy.bondAction(FitbitBlePolicy.BondState.NONE)
        )
    }

    @Test
    fun bondingDeviceWaitsAndBondedDeviceDiscovers() {
        assertEquals(
            FitbitBlePolicy.BondAction.WAIT_FOR_BOND,
            FitbitBlePolicy.bondAction(FitbitBlePolicy.BondState.BONDING)
        )
        assertEquals(
            FitbitBlePolicy.BondAction.DISCOVER_SERVICES,
            FitbitBlePolicy.bondAction(FitbitBlePolicy.BondState.BONDED)
        )
    }

    @Test
    fun allFitbitSecurityStatusesAreRecognized() {
        listOf(0x05, 0x08, 0x0C, 0x0F).forEach {
            assertTrue(
                "Expected status $it to be security-related",
                FitbitBlePolicy.isSecurityGattFailure(it)
            )
        }
        assertFalse(FitbitBlePolicy.isSecurityGattFailure(0x00))
        assertFalse(FitbitBlePolicy.isSecurityGattFailure(0x85))
    }

    @Test
    fun guidedRepairStartsOnSecondConsecutiveFailure() {
        assertFalse(FitbitBlePolicy.needsGuidedRepair(0))
        assertFalse(FitbitBlePolicy.needsGuidedRepair(1))
        assertTrue(FitbitBlePolicy.needsGuidedRepair(2))
        assertTrue(FitbitBlePolicy.needsGuidedRepair(3))
    }
}
