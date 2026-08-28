package com.hrbot.android

/**
 * Small, Android-free decisions used by the Fitbit BLE session coordinator.
 * Keeping these rules separate makes the recovery thresholds testable without
 * constructing framework Bluetooth objects.
 */
internal object FitbitBlePolicy {
    private const val GATT_INSUFFICIENT_AUTHENTICATION = 0x05
    private const val GATT_INSUFFICIENT_AUTHORIZATION = 0x08

    // ATT error 0x0c. Android API 34 does not expose a BluetoothGatt constant
    // for this status, but devices can still return it through GATT callbacks.
    private const val GATT_INSUFFICIENT_ENCRYPTION_KEY_SIZE = 0x0C
    private const val GATT_INSUFFICIENT_ENCRYPTION = 0x0F

    const val FAILURES_BEFORE_GUIDED_REPAIR = 2

    enum class BondState {
        NONE,
        BONDING,
        BONDED
    }

    enum class BondAction {
        START_BONDING,
        WAIT_FOR_BOND,
        DISCOVER_SERVICES
    }

    fun bondAction(state: BondState): BondAction = when (state) {
        BondState.NONE -> BondAction.START_BONDING
        BondState.BONDING -> BondAction.WAIT_FOR_BOND
        BondState.BONDED -> BondAction.DISCOVER_SERVICES
    }

    fun isSecurityGattFailure(status: Int): Boolean = when (status) {
        GATT_INSUFFICIENT_AUTHENTICATION,
        GATT_INSUFFICIENT_AUTHORIZATION,
        GATT_INSUFFICIENT_ENCRYPTION_KEY_SIZE,
        GATT_INSUFFICIENT_ENCRYPTION -> true
        else -> false
    }

    fun needsGuidedRepair(consecutiveFailures: Int): Boolean =
        consecutiveFailures >= FAILURES_BEFORE_GUIDED_REPAIR
}
